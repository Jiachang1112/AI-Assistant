import os
import logging
import datetime
from flask import Flask, request, abort, redirect, url_for, session
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

import google.generativeai as genai
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.api_core import protobuf_helpers

# 啟用 log
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "random_secret_string") 

# 本地測試或 Render 開發環境允許 HTTP
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# 檢查變數
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET]):
    logging.error("環境變數未設定完全，請檢查 Render 設定。")

# --- 初始化 ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)

# 暫存使用者憑證 (重啟會消失，建議正式版改用資料庫)
user_credentials = {}

# Google 權限範圍
SCOPES = [
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid'
]

# --- 定義給 Gemini 用的工具函式 (Tools) ---

def create_calendar_event(title: str, start_time: str, end_time: str, description: str = ""):
    """
    在 Google 日曆建立行程。
    Args:
        title: 行程標題
        start_time: 開始時間 (ISO 8601 格式, 例如 2025-11-29T15:00:00)
        end_time: 結束時間 (ISO 8601 格式)
        description: 行程描述 (選填)
    """
    # 這個函式只是定義介面，實際執行邏輯在 handle_message 裡面透過 user_id 執行
    return "Event creation request received."

def get_calendar_events(time_min: str = None):
    """
    查詢接下來的日曆行程。
    Args:
        time_min: 查詢起始時間 (ISO 8601 格式)，若未提供則預設為現在。
    """
    return "Calendar list request received."

# 將工具包裝起來
tools_list = [create_calendar_event, get_calendar_events]

# 設定系統指令，包含目前時間，讓 AI 知道「明天」是幾號
def get_system_instruction():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""
    你是一個專業的 Google 日曆助理。現在時間是 {now}。
    
    1. 當使用者想「查詢」或「新增」行程時，請務必呼叫對應的 function tool。
    2. 使用者說的時間如果是相對時間（如「明天下午三點」），請根據現在時間轉換成 ISO 8601 格式 (YYYY-MM-DDTHH:MM:SS)。
    3. 如果使用者沒有指定結束時間，預設行程長度為 1 小時。
    4. 若使用者尚未登入或綁定，請引導他們輸入「登入」。
    5. 回應時請使用繁體中文 (Traditional Chinese)。
    """

# --- 路由與 OAuth ---

@app.route("/")
def home():
    return "OK - Calendar Bot is running", 200

@app.route("/login")
def login():
    line_user_id = request.args.get('userid')
    if not line_user_id:
        return "錯誤：無效的使用者 ID"
    session['line_user_id'] = line_user_id

    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    redirect_uri = url_for('oauth2callback', _external=True)
    flow = Flow.from_client_config(client_config=client_config, scopes=SCOPES, redirect_uri=redirect_uri)
    authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true')
    session['state'] = state
    return redirect(authorization_url)

@app.route("/oauth2callback")
def oauth2callback():
    state = session.get('state')
    line_user_id = session.get('line_user_id')
    
    if not line_user_id:
        return "錯誤：Session 過期，請重新從 LINE 點擊登入。"

    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    redirect_uri = url_for('oauth2callback', _external=True)
    flow = Flow.from_client_config(client_config=client_config, scopes=SCOPES, state=state, redirect_uri=redirect_uri)
    flow.fetch_token(authorization_response=request.url)
    
    creds = flow.credentials
    
    # 儲存憑證
    user_credentials[line_user_id] = {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    }

    try:
        line_bot_api.push_message(line_user_id, TextSendMessage(text="🎉 Google 帳號綁定成功！現在你可以直接叫我「幫我明天下午三點安排會議」或是「看看我有什麼行程」。"))
    except Exception as e:
        logging.error(f"Push message failed: {e}")

    return "綁定成功！請關閉視窗回到 LINE。"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# --- 實際執行 Google Calendar API 的函式 ---
def execute_calendar_api(user_id, function_name, args):
    if user_id not in user_credentials:
        return "錯誤：使用者尚未登入，無法執行日曆操作。請輸入「登入」。"

    # 重建憑證物件
    creds_info = user_credentials[user_id]
    creds = Credentials.from_authorized_user_info(creds_info)
    
    try:
        service = build('calendar', 'v3', credentials=creds)
        
        if function_name == "create_calendar_event":
            event = {
                'summary': args.get('title'),
                'description': args.get('description', ''),
                'start': {'dateTime': args.get('start_time'), 'timeZone': 'Asia/Taipei'},
                'end': {'dateTime': args.get('end_time'), 'timeZone': 'Asia/Taipei'},
            }
            created_event = service.events().insert(calendarId='primary', body=event).execute()
            return f"成功建立行程：{created_event.get('htmlLink')}"
            
        elif function_name == "get_calendar_events":
            now = datetime.datetime.utcnow().isoformat() + 'Z'  # 'Z' indicates UTC time
            time_min = args.get('time_min', now)
            
            events_result = service.events().list(
                calendarId='primary', timeMin=time_min,
                maxResults=10, singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])

            if not events:
                return "接下來沒有行程。"
            
            result_text = "接下來的行程：\n"
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                result_text += f"- {start}: {event['summary']}\n"
            return result_text

    except Exception as e:
        logging.error(f"Google API Error: {e}")
        return f"執行日曆操作時發生錯誤：{str(e)}"
    
    return "未知的操作。"

# --- LINE 訊息處理 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    user_id = event.source.user_id

    # 1. 處理登入指令
    if user_msg in ["登入", "綁定", "連結Google"]:
        login_url = url_for('login', userid=user_id, _external=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"請點擊連結進行綁定：\n{login_url}"))
        return

    # 2. 呼叫 Gemini (帶有 Tools)
    # 檢查使用者是否已登入，以決定是否啟用工具
    # 注意：即便已登入，這裡我們每次都把工具給 Gemini，讓它決定是否呼叫
    
    try:
        # 建立模型 (每次都重新建立以更新系統時間指令)
        model = genai.GenerativeModel(
            "gemini-2.0-flash",
            tools=tools_list,
            system_instruction=get_system_instruction()
        )
        
        # 啟動對話
        chat = model.start_chat(enable_automatic_function_calling=False) # 我們手動處理 Function Call 以便注入 user_id
        
        response = chat.send_message(user_msg)
        
        # 檢查是否有 Function Call
        if response.parts[0].function_call:
            fc = response.parts[0].function_call
            func_name = fc.name
            func_args = dict(fc.args)
            
            logging.info(f"Gemini 請求執行函式: {func_name}, 參數: {func_args}")
            
            # 執行 Google Calendar API
            api_result = execute_calendar_api(user_id, func_name, func_args)
            
            # 把結果回傳給 Gemini，讓它生成最終人類語言的回覆
            # 注意：Gemini 2.0 在手動 function calling 的流程需要把結果送回
            final_response = chat.send_message(
                genai.prototypes.Part(
                    function_response=genai.prototypes.FunctionResponse(
                        name=func_name,
                        response={'result': api_result}
                    )
                )
            )
            reply_text = final_response.text
        else:
            # 純文字回覆
            reply_text = response.text

    except Exception as e:
        logging.exception("Gemini Error")
        reply_text = "抱歉，我現在有點錯亂，請稍後再試。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
