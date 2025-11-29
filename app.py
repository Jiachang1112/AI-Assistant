import os
import logging
import datetime
import json
from flask import Flask, request, abort, redirect, url_for, session
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
# --- 修改：加入 QuickReply 相關元件 ---
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, 
    FlexSendMessage, BubbleContainer, BoxComponent, 
    TextComponent, ButtonComponent, URIAction,
    QuickReply, QuickReplyButton, MessageAction
)

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
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS")

# --- 關鍵修正：解決 LINE 瀏覽器 MismatchingStateError 問題 ---
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "random_secret_string")
app.config['SESSION_COOKIE_SECURE'] = True  # 確保透過 HTTPS 傳輸 Cookie
app.config['SESSION_COOKIE_SAMESITE'] = 'None' # 允許跨站傳輸
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
    if not firebase_admin._apps:
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

# --- 資料庫操作函式 ---
def save_user_credentials(user_id, creds_data):
    try:
        doc_ref = db.collection('users').document(user_id)
        doc_ref.set(creds_data)
        logging.info(f"使用者 {user_id} 資料已儲存至 Firebase")
    except Exception as e:
        logging.error(f"儲存 Firebase 失敗: {e}")

def get_user_credentials(user_id):
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

# 【新增】刪除使用者資料 (登出用)
def delete_user_credentials(user_id):
    try:
        db.collection('users').document(user_id).delete()
        logging.info(f"使用者 {user_id} 已從資料庫刪除 (登出)")
        return True
    except Exception as e:
        logging.error(f"刪除 Firebase 失敗: {e}")
        return False

# --- 工具函式 (Tools) ---
def create_calendar_event(title: str, start_time: str, end_time: str, description: str = ""):
    return "Event creation request received."

def get_calendar_events(time_min: str = None):
    return "Calendar list request received."

tools_list = [create_calendar_event, get_calendar_events]

def get_system_instruction():
    # 取得 UTC 時間，然後手動加 8 小時變成台灣時間
    utc_now = datetime.datetime.utcnow()
    taipei_time = utc_now + datetime.timedelta(hours=8)
    
    # 轉成字串
    now = taipei_time.strftime("%Y-%m-%d %H:%M:%S")
    
    return f"""
    你是一個專業的 Google 日曆助理。現在台灣時間是 {now} (週{taipei_time.isoweekday()})。
    
    1. 當使用者想「查詢」或「新增」行程時，請務必呼叫對應的 function tool。
    2. 使用者說的時間如果是相對時間（如「明天下午三點」），請根據現在時間轉換成 ISO 8601 格式 (YYYY-MM-DDTHH:MM:SS)。
    3. 如果使用者沒有指定結束時間，預設行程長度為 1 小時。
    4. 若使用者尚未登入或綁定，請引導他們輸入「登入」。
    5. 回應時請使用繁體中文 (Traditional Chinese)。
    """

# --- 【修改】動態產生 Quick Reply 按鈕 ---
def get_quick_reply(user_id):
    # 先去資料庫檢查這個人是否已登入
    creds = get_user_credentials(user_id)
    is_logged_in = creds is not None

    items = [
        QuickReplyButton(action=MessageAction(label="🔍 查詢行程", text="查詢接下來的行程")),
        QuickReplyButton(action=MessageAction(label="➕ 新增範例", text="幫我新增明天早上9點開會")),
    ]

    # 根據登入狀態切換按鈕
    if is_logged_in:
        # 已登入 -> 顯示「登出」
        items.append(QuickReplyButton(action=MessageAction(label="👋 登出", text="登出")))
    else:
        # 未登入 -> 顯示「登入」
        items.append(QuickReplyButton(action=MessageAction(label="🔗 綁定 Google", text="登入")))

    items.append(QuickReplyButton(action=MessageAction(label="❓ 你能做什麼", text="請問你可以幫我做什麼？")))

    return QuickReply(items=items)

# --- 新增：製作漂亮行程卡片的函式 ---
def create_event_flex_message(event_data):
    summary = event_data.get('summary', '無標題')
    html_link = event_data.get('htmlLink')
    
    # 處理時間顯示
    start = event_data['start']
    if 'dateTime' in start:
        time_str = start['dateTime'].replace('T', ' ')[:16] # 取到分就好
    else:
        time_str = f"{start['date']} (全天)"

    bubble = BubbleContainer(
        body=BoxComponent(
            layout='vertical',
            contents=[
                TextComponent(text='📅 行程已建立', weight='bold', color='#1DB446', size='sm'),
                TextComponent(text=summary, weight='bold', size='xl', margin='md', wrap=True),
                BoxComponent(
                    layout='vertical',
                    margin='lg',
                    spacing='sm',
                    contents=[
                        BoxComponent(
                            layout='baseline',
                            spacing='sm',
                            contents=[
                                TextComponent(text='時間', color='#aaaaaa', size='sm', flex=1),
                                TextComponent(text=time_str, wrap=True, color='#666666', size='sm', flex=5)
                            ],
                        ),
                    ],
                )
            ],
        ),
        footer=BoxComponent(
            layout='vertical',
            spacing='sm',
            contents=[
                # 編輯按鈕
                ButtonComponent(
                    style='link',
                    height='sm',
                    action=URIAction(label='編輯 / 查看行程', uri=html_link)
                )
            ],
            flex=0
        )
    )
    return FlexSendMessage(alt_text=f"已建立行程：{summary}", contents=bubble)

# --- 路由 ---
@app.route("/")
def home():
    return "OK - Bot with Dynamic Buttons", 200

@app.route("/login")
def login():
    line_user_id = request.args.get('userid')
    if not line_user_id:
        return "錯誤：無效的使用者 ID"
    
    session.permanent = True  # 設定 Session 持久化
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
    
    # 強制 prompt='consent' 以取得 refresh_token
    authorization_url, state = flow.authorization_url(
        access_type='offline', 
        include_granted_scopes='true',
        prompt='consent' 
    )
    
    session['state'] = state
    return redirect(authorization_url)

@app.route("/oauth2callback")
def oauth2callback():
    # 檢查 state 是否存在
    if 'state' not in session:
        return "錯誤：瀏覽器 Session 失效。請嘗試「複製連結」並在 Chrome/Safari 瀏覽器中開啟以完成登入。"
        
    state = session['state']
    line_user_id = session.get('line_user_id')
    
    if not line_user_id:
        return "錯誤：無法識別使用者，請重新登入。"

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
    
    try:
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        
        if not creds.refresh_token:
            logging.warning("警告：Google 未回傳 refresh_token")
        
        creds_data = {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': creds.token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': creds.scopes
        }

        save_user_credentials(line_user_id, creds_data)

        try:
            # 綁定成功後，推播訊息並更新按鈕為「登出」
            line_bot_api.push_message(
                line_user_id, 
                TextSendMessage(
                    text="🎉 綁定成功！我現在有永久記憶了，請試著叫我新增行程。",
                    quick_reply=get_quick_reply(line_user_id) # 這裡會自動變成登出按鈕
                )
            )
        except:
            pass
            
        return "綁定成功！請關閉視窗回到 LINE。"
        
    except Exception as e:
        logging.error(f"OAuth callback error: {e}")
        return f"綁定失敗，請重試。錯誤訊息：{e}"

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
    creds_info = get_user_credentials(user_id)
    if not creds_info or not creds_info.get('refresh_token'):
        return "錯誤：授權已過期或不完整。請輸入「登入」重新綁定 Google 帳號。"

    creds = Credentials.from_authorized_user_info(creds_info)
    
    try:
        # 自動 refresh token
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            creds_data = {
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret,
                'scopes': creds.scopes
            }
            save_user_credentials(user_id, creds_data)

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
            # 回傳 event 物件以製作卡片
            return created_event
            
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
        return f"執行錯誤：{str(e)}"
    
    return "未知的操作。"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    user_id = event.source.user_id

    # --- 1. 處理登出指令 ---
    if user_msg == "登出":
        delete_user_credentials(user_id)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="已成功登出！若要繼續使用日曆功能，請重新登入。",
                quick_reply=get_quick_reply(user_id) # 登出後，這裡會變回「登入」按鈕
            )
        )
        return

    # --- 2. 處理登入綁定 ---
    if user_msg in ["登入", "綁定", "連結Google"]:
        login_url = url_for('login', userid=user_id, _external=True)
        line_bot_api.reply_message(
            event.reply_token, 
            TextSendMessage(
                text=f"請點擊連結進行綁定 (若失敗請複製連結到 Chrome 開啟)：\n{login_url}",
                quick_reply=get_quick_reply(user_id)
            )
        )
        return

    # --- 3. Gemini 對話 ---
    try:
        model = genai.GenerativeModel("gemini-2.0-flash", tools=tools_list, system_instruction=get_system_instruction())
        chat = model.start_chat(enable_automatic_function_calling=False)
        response = chat.send_message(user_msg)
        
        # --- 處理 Function Call ---
        if response.parts and response.parts[0].function_call:
            fc = response.parts[0].function_call
            func_name = fc.name
            func_args = dict(fc.args)
            
            api_result = execute_calendar_api(user_id, func_name, func_args)
            
            # (A) 如果是建立行程成功 (回傳字典)
            if isinstance(api_result, dict) and 'htmlLink' in api_result:
                flex_msg = create_event_flex_message(api_result)
                flex_msg.quick_reply = get_quick_reply(user_id) # 加入按鈕
                
                line_bot_api.reply_message(event.reply_token, flex_msg)
                
                # 安靜回報給 Gemini
                response_part = {
                    "function_response": {
                        "name": func_name,
                        "response": {"result": "Event created successfully."}
                    }
                }
                chat.send_message(response_part)
                
            # (B) 如果是查詢或其他結果 (回傳文字)
            else:
                response_part = {
                    "function_response": {
                        "name": func_name,
                        "response": {"result": api_result}
                    }
                }
                final_response = chat.send_message(response_part)
                line_bot_api.reply_message(
                    event.reply_token, 
                    TextSendMessage(
                        text=final_response.text,
                        quick_reply=get_quick_reply(user_id) # 加入按鈕
                    )
                )
        # --- 處理一般對話 ---
        else:
            line_bot_api.reply_message(
                event.reply_token, 
                TextSendMessage(
                    text=response.text,
                    quick_reply=get_quick_reply(user_id) # 加入按鈕
                )
            )

    except Exception as e:
        logging.exception("Gemini Error")
        line_bot_api.reply_message(
            event.reply_token, 
            TextSendMessage(
                text="系統忙碌中，請稍後再試。",
                quick_reply=get_quick_reply(user_id) # 加入按鈕
            )
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
