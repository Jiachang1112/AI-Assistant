import os
import logging
import datetime
import json
from flask import Flask, request, abort, redirect, url_for, session
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

import google.generativeai as genai
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# --- Firebase 相關 ---
import firebase_admin
from firebase_admin import credentials, firestore

# 啟用 log
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS") # 新增這行

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "random_secret_string") 
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# 檢查變數
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, FIREBASE_CREDENTIALS_JSON]):
    logging.error("環境變數未設定完全，請檢查 Render 設定 (含 FIREBASE_CREDENTIALS)。")

# --- 初始化 LINE & Gemini ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)

# --- 初始化 Firebase ---
try:
    # 讀取環境變數中的 JSON 字串並轉為字典
    cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logging.info("Firebase 初始化成功！")
except Exception as e:
    logging.error(f"Firebase 初始化失敗: {e}")

# Google 權限
SCOPES = [
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid'
]

# --- 資料庫操作函式 (取代原本的記憶體字典) ---

def save_user_credentials(user_id, creds_data):
    """將使用者的憑證存入 Firestore"""
    try:
        # 在 'users' 集合中，以 user_id 為檔名儲存
        doc_ref = db.collection('users').document(user_id)
        doc_ref.set(creds_data)
        logging.info(f"使用者 {user_id} 資料已儲存至 Firebase")
    except Exception as e:
        logging.error(f"儲存 Firebase 失敗: {e}")

def get_user_credentials(user_id):
    """從 Firestore 讀取使用者憑證"""
    try:
        doc_ref = db.collection('users').document(user_id)
        doc = doc_ref.get()
        if doc.exists:
            return doc.to_dict()
        else:
            return None
    except Exception as e:
        logging.error(f"讀取 Firebase 失敗: {e}")
        return None

# --- 工具函式 (Tools) ---
def create_calendar_event(title: str, start_time: str, end_time: str, description: str = ""):
    return "Event creation request received."

def get_calendar_events(time_min: str = None):
    return "Calendar list request received."

tools_list = [create_calendar_event, get_calendar_events]

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

# --- 路由 ---
@app.route("/")
def home():
    return "OK - Firebase Enabled Bot", 200

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
        return "錯誤：Session 過期。"

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
    
    # 準備要存入 Firebase 的資料
    creds_data = {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    }

    # --- 寫入 Firebase ---
    save_user_credentials(line_user_id, creds_data)

    try:
        line_bot_api.push_message(line_user_id, TextSendMessage(text="🎉 綁定成功！資料已安全儲存。"))
    except Exception as e:
        logging.error(f"Push message failed: {e}")

    return "綁定成功！請關閉視窗。"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# --- 執行 Calendar API ---
def execute_calendar_api(user_id, function_name, args):
    # --- 從 Firebase 讀取 ---
    creds_info = get_user_credentials(user_id)
    
    if not creds_info:
        return "錯誤：使用者尚未登入，無法執行日曆操作。請輸入「登入」。"

    creds = Credentials.from_authorized_user_info(creds_info)
    
    try:
        service = build('calendar', 'v3', credentials=creds)
        
        if function_name == "create_calendar_event":
            summary = args.get('title', '未命名行程')
            start_time = args.get('start_time')
            end_time = args.get('end_time')
            
            if not end_time and start_time:
                try:
                    dt = datetime.datetime.fromisoformat(start_time)
                    end_time = (dt + datetime.timedelta(hours=1)).isoformat()
                except:
                    pass

            event = {
                'summary': summary,
                'description': args.get('description', ''),
                'start': {'dateTime': start_time, 'timeZone': 'Asia/Taipei'},
                'end': {'dateTime': end_time, 'timeZone': 'Asia/Taipei'},
            }
            created_event = service.events().insert(calendarId='primary', body=event).execute()
            return f"成功建立行程：{created_event.get('htmlLink')}"
            
        elif function_name == "get_calendar_events":
            now = datetime.datetime.utcnow().isoformat() + 'Z'
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
                summary = event.get('summary', '無標題')
                result_text += f"- {start}: {summary}\n"
            return result_text

    except Exception as e:
        logging.error(f"Google API Error: {e}")
        return f"執行日曆操作時發生錯誤：{str(e)}"
    
    return "未知的操作。"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    user_id = event.source.user_id

    if user_msg in ["登入", "綁定", "連結Google"]:
        login_url = url_for('login', userid=user_id, _external=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"請點擊連結進行綁定：\n{login_url}"))
        return

    try:
        model = genai.GenerativeModel("gemini-2.0-flash", tools=tools_list, system_instruction=get_system_instruction())
        chat = model.start_chat(enable_automatic_function_calling=False)
        response = chat.send_message(user_msg)
        
        if response.parts and response.parts[0].function_call:
            fc = response.parts[0].function_call
            func_name = fc.name
            func_args = dict(fc.args)
            
            # 這裡的 execute_calendar_api 內部已經改為讀取 Firebase
            api_result = execute_calendar_api(user_id, func_name, func_args)
            
            response_part = {
                "function_response": {
                    "name": func_name,
                    "response": {"result": api_result}
                }
            }
            final_response = chat.send_message(response_part)
            reply_text = final_response.text
        else:
            reply_text = response.text

    except Exception as e:
        logging.exception("Gemini Error")
        reply_text = f"系統發生錯誤，請稍後再試。(錯誤: {str(e)})"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
