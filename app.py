import os
import logging
import json # 新增：處理 JSON
from flask import Flask, request, abort, redirect, url_for, session
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

import google.generativeai as genai
# 新增：Google OAuth 相關套件
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials

# 啟用 log
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Google 相關變數
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
# Flask Session 需要一個密鑰
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "random_secret_string") 

# 允許 http 傳輸 (僅限本地開發測試用，Render 上線時建議拿掉或設為 0，但在 Render 預設是 HTTPS 所以通常沒問題)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET or not GEMINI_API_KEY:
    raise RuntimeError("環境變數沒有設好，請在 Render Environment 確認 LINE 和 Gemini 設定")

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    logging.warning("警告：Google Client ID/Secret 尚未設定，登入功能將無法使用")

# --- 初始化 ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)

# --- 暫存使用者資料庫 (注意：Render 免費版重啟後會消失，正式版建議用 Firebase 或資料庫) ---
# 結構: { 'LINE_USER_ID': CredentialsObject }
user_credentials = {}

# --- Google OAuth 設定 ---
# 這裡設定你要存取的權限，例如日曆、Email
SCOPES = [
    'https://www.googleapis.com/auth/calendar.events', # 讀寫日曆事件
    'https://www.googleapis.com/auth/userinfo.email',
    'openid'
]

# --- 系統指令 ---
sys_instruction = """
你是一個有用的 AI 助手。
請根據使用者輸入的語言來決定回應的語言（例如使用者用英文，你就回英文）。
但在使用中文時，請務必遵守以下最高指導原則：
「所有中文回應都必須使用繁體中文 (Traditional Chinese)，絕對禁止使用簡體中文。」
"""

model = genai.GenerativeModel(
    "gemini-2.0-flash",
    system_instruction=sys_instruction
)

# --- 路由與功能 ---

@app.route("/")
def home():
    return "OK - AI Assistant is running", 200

# 1. 登入路由：使用者點擊連結後會來到這裡
@app.route("/login")
def login():
    # 取得網址列傳來的 user_id (來自 LINE)
    line_user_id = request.args.get('userid')
    if not line_user_id:
        return "錯誤：無效的使用者 ID"
    
    # 把 LINE ID 存入 session，等下 Google 回來時才知道是誰
    session['line_user_id'] = line_user_id

    # 建立 Google 登入設定
    # 為了方便 Render 部署，我們直接用字典建立 config，不讀取 client_secret.json 檔案
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    # 動態偵測目前的網域 (Render 的網址)
    redirect_uri = url_for('oauth2callback', _external=True)

    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )
    
    session['state'] = state
    return redirect(authorization_url)

# 2. 回呼路由：Google 驗證完會跳轉回這裡
@app.route("/oauth2callback")
def oauth2callback():
    state = session.get('state')
    
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    redirect_uri = url_for('oauth2callback', _external=True)

    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        state=state,
        redirect_uri=redirect_uri
    )

    # 用回傳的 code 換取 token
    flow.fetch_token(authorization_response=request.url)

    # 取得憑證
    credentials = flow.credentials
    
    # 從 session 取回是哪位 LINE 使用者
    line_user_id = session.get('line_user_id')

    if line_user_id:
        # 儲存憑證 (目前存記憶體，重啟會消失)
        # TODO: 未來請改成存入 Firebase 或資料庫
        user_credentials[line_user_id] = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes
        }
        
        # 嘗試推播訊息給使用者，通知綁定成功
        try:
            line_bot_api.push_message(
                line_user_id,
                TextSendMessage(text="Google 帳號綁定成功！我可以開始幫你處理日曆了。")
            )
        except Exception as e:
            logging.error(f"推播失敗: {e}")

        return "綁定成功！你可以關閉這個視窗回到 LINE 了。"
    else:
        return "錯誤：找不到對應的 LINE 使用者，請重新操作。"

# LINE Webhook 入口
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    logging.info(f"收到 LINE webhook：{body}")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logging.exception("Invalid signature")
        abort(400)

    return "OK"

# 處理文字訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip() # 去除前後空白
    user_id = event.source.user_id
    logging.info(f"使用者({user_id})說：{user_msg}")

    # --- 特殊指令：綁定 Google ---
    if user_msg == "綁定" or user_msg == "登入" or user_msg == "連結Google":
        # 產生登入連結
        # 這裡的 url_for 會產生類似 https://your-app.onrender.com/login 的網址
        login_url = url_for('login', userid=user_id, _external=True)
        
        reply_text = f"請點擊以下連結來綁定 Google 帳號：\n{login_url}"
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
        return

    # --- 一般對話 (Gemini) ---
    try:
        # 檢查使用者是否已登入 (目前只做檢查，還沒做日曆功能)
        is_logged_in = user_id in user_credentials
        
        # 可以在 prompt 讓 Gemini 知道使用者狀態
        context_prompt = ""
        if is_logged_in:
            context_prompt = "(使用者已綁定 Google 帳號)"
        
        response = model.generate_content(user_msg + context_prompt)
        reply_text = response.text
        logging.info(f"Gemini 回覆：{reply_text}")
    except Exception as e:
        logging.exception("呼叫 Gemini 發生錯誤")
        reply_text = "系統有點忙碌，請稍後再試。"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
