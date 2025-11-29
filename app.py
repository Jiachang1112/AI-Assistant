import os
import logging
import datetime
import json
import requests # 用來強制發送動畫請求
from flask import Flask, request, abort, redirect, url_for, session
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, 
    FlexSendMessage, BubbleContainer, BoxComponent, 
    TextComponent, ButtonComponent, URIAction,
    QuickReply, QuickReplyButton, MessageAction,
    CarouselContainer, ImageComponent
)

import google.generativeai as genai
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

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

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "random_secret_string")
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'None'
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, FIREBASE_CREDENTIALS_JSON]):
    logging.error("環境變數未設定完全，請檢查 Render 設定。")

# --- 初始化 ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)

try:
    if not firebase_admin._apps:
        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    logging.info("Firebase 初始化成功！")
except Exception as e:
    logging.error(f"Firebase 初始化失敗: {e}")

SCOPES = [
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid'
]

# --- 資料庫操作 ---
def save_user_credentials(user_id, creds_data):
    try:
        doc_ref = db.collection('users').document(user_id)
        doc_ref.set(creds_data, merge=True)
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

def delete_user_credentials(user_id):
    try:
        db.collection('users').document(user_id).delete()
        return True
    except Exception as e:
        logging.error(f"刪除 Firebase 失敗: {e}")
        return False

def save_user_style(user_id, style):
    try:
        doc_ref = db.collection('users').document(user_id)
        doc_ref.set({'reply_style': style}, merge=True)
    except Exception as e:
        logging.error(f"儲存風格失敗: {e}")

def get_chat_history(user_id):
    try:
        doc = db.collection('users').document(user_id).get()
        if doc.exists:
            data = doc.to_dict()
            raw_history = data.get('chat_history', [])
            gemini_history = []
            for h in raw_history:
                gemini_history.append({"role": h['role'], "parts": [h['text']]})
            return gemini_history
        return []
    except Exception as e:
        logging.error(f"讀取對話紀錄失敗: {e}")
        return []

def save_chat_history(user_id, user_text, model_text):
    try:
        doc_ref = db.collection('users').document(user_id)
        doc = doc_ref.get()
        current_history = doc.to_dict().get('chat_history', []) if doc.exists else []
        
        current_history.append({"role": "user", "text": user_text})
        current_history.append({"role": "model", "text": model_text})
        
        if len(current_history) > 20:
            current_history = current_history[-20:]
            
        doc_ref.set({'chat_history': current_history}, merge=True)
    except Exception as e:
        logging.error(f"儲存對話紀錄失敗: {e}")

def clear_chat_history(user_id):
    try:
        doc_ref = db.collection('users').document(user_id)
        doc_ref.set({'chat_history': []}, merge=True)
        return True
    except Exception as e:
        logging.error(f"清空對話失敗: {e}")
        return False

# --- 核心邏輯 ---
def get_default_ledger_id(user_id):
    try:
        ledgers_ref = db.collection('users').document(user_id).collection('ledgers')
        q_default = ledgers_ref.where('isDefault', '==', True).limit(1)
        snap_default = q_default.get()
        if snap_default: return snap_default[0].id
        q_first = ledgers_ref.order_by('createdAt').limit(1)
        snap_first = q_first.get()
        if snap_first: return snap_first[0].id
        
        new_ledger = {
            'name': '預設帳本', 'currency': 'TWD', 'isDefault': True,
            'createdAt': firestore.SERVER_TIMESTAMP, 'updatedAt': firestore.SERVER_TIMESTAMP
        }
        update_time, ref = ledgers_ref.add(new_ledger)
        return ref.id
    except Exception as e:
        logging.error(f"取得帳本失敗: {e}")
        return None

# --- Tools 定義 ---
def create_calendar_event(title: str, start_time: str, end_time: str = None, description: str = ""):
    """
    在 Google 日曆建立行程。
    """
    return "Event creation request received."

def get_calendar_events(time_min: str = None):
    return "Calendar list request received."

def add_accounting_entry(item: str, amount: float, category: str = "其他", type: str = "expense", note: str = ""):
    return "Accounting request received."

tools_list = [create_calendar_event, get_calendar_events, add_accounting_entry]

def get_system_instruction(style=None):
    utc_now = datetime.datetime.utcnow()
    taipei_time = utc_now + datetime.timedelta(hours=8)
    now = taipei_time.strftime("%Y-%m-%d %H:%M:%S")
    
    base_instruction = f"""
    你是一個專業的 Google 日曆助理與生活記帳助手。現在台灣時間是 {now} (週{taipei_time.isoweekday()})。
    
    【最高指導原則 - 直接執行】
    1. 當使用者提到「新增」、「記」、「約」、「安排」行程時，請直接呼叫 `Calendar` 工具。
       - 如果使用者說「開會」，title 參數就填「開會」。
       - 如果使用者沒說結束時間，請不用問，直接不用填。
    
    2. 當使用者輸入金額、品項，請呼叫 `add_accounting_entry`。

    3. 若使用者尚未登入或綁定，請引導他們輸入「登入」。
    4. 回應時請使用繁體中文 (Traditional Chinese)。
    """
    if style:
        base_instruction += f"\n\n【語氣風格】請依照「{style}」的風格回應：\n{style}"
    return base_instruction

def get_quick_reply(user_id):
    creds = get_user_credentials(user_id)
    is_logged_in = creds and creds.get('refresh_token')
    items = [
        QuickReplyButton(action=MessageAction(label="🔍 查詢行程", text="查詢接下來的行程")),
        QuickReplyButton(action=MessageAction(label="➕ 新增範例", text="幫我新增明天早上9點開會")),
        QuickReplyButton(action=MessageAction(label="💰 記帳/風格", text="開啟記帳模式")),
        QuickReplyButton(action=MessageAction(label="🗑️ 清空對話", text="清空對話")),
        QuickReplyButton(action=MessageAction(label="❓ 你能做什麼", text="請問你可以幫我做什麼？")),
    ]
    if is_logged_in:
        items.append(QuickReplyButton(action=MessageAction(label="👋 登出", text="登出")))
    else:
        items.append(QuickReplyButton(action=MessageAction(label="🔗 綁定 Google", text="登入")))
    return QuickReply(items=items)

# --- 修改：日曆 Flex Message (縮小圖示、保留右側文字) ---
def create_event_bubble(event_data):
    summary = event_data.get('summary') or event_data.get('title') or '未命名行程'
    html_link = event_data.get('htmlLink')
    start = event_data['start']
    
    time_str = ""
    if 'dateTime' in start:
        dt = datetime.datetime.fromisoformat(start['dateTime'])
        time_str = dt.strftime('%Y-%m-%d %H:%M')
    else:
        time_str = f"{start['date']} (全天)"

    return BubbleContainer(
        header=BoxComponent(
            layout='horizontal',
            backgroundColor='#1DB446',
            paddingAll='15px',
            contents=[
                # 圖示縮小為 3xl
                TextComponent(text='📅', size='3xl', flex=0, align='center', gravity='center'),
                # 標題靠左對齊圖示，顯示「行程已建立」
                TextComponent(
                    text='行程已建立', 
                    weight='bold', 
                    color='#ffffff', 
                    size='lg', 
                    align='start', 
                    gravity='center', 
                    margin='md',
                    flex=1
                )
            ]
        ),
        body=BoxComponent(
            layout='vertical',
            contents=[
                TextComponent(text=summary, weight='bold', size='3xl', margin='md', wrap=True, color='#111111'),
                BoxComponent(
                    layout='vertical', margin='lg', spacing='sm',
                    contents=[
                        BoxComponent(
                            layout='baseline', spacing='sm',
                            contents=[
                                TextComponent(text='時間', color='#aaaaaa', size='sm', flex=1),
                                TextComponent(text=time_str, wrap=True, color='#666666', size='md', flex=4, weight="bold")
                            ],
                        ),
                    ],
                )
            ],
        ),
        footer=BoxComponent(
            layout='vertical', spacing='sm',
            contents=[
                ButtonComponent(
                    style='secondary', height='sm',
                    action=URIAction(label='✏️ 編輯 / 查看', uri=html_link),
                    color='#1DB446'
                )
            ],
            paddingAll='16px'
        )
    )

# --- 修改：記帳 Flex Message (縮小圖示、保留右側文字) ---
def create_accounting_bubble(data):
    is_income = data.get('type') == 'income'
    theme_color = '#10b981' if is_income else '#ef4444'
    sign = '+' if is_income else '-'
    icon = '💰' if is_income else '💸'
    title_text = '收入入帳' if is_income else '支出記帳'
    
    return BubbleContainer(
        header=BoxComponent(
            layout='horizontal',
            backgroundColor=theme_color,
            paddingAll='15px',
            contents=[
                # 圖示縮小為 3xl
                TextComponent(text=icon, size='3xl', flex=0, align='center', gravity='center'),
                # 標題靠左對齊圖示
                TextComponent(
                    text=title_text, 
                    weight='bold', 
                    color='#ffffff', 
                    size='lg', 
                    align='start', 
                    gravity='center', 
                    margin='md',
                    flex=1
                )
            ]
        ),
        body=BoxComponent(
            layout='vertical',
            contents=[
                TextComponent(text=data.get('item', '未命名'), weight='bold', size='xl', margin='md', color='#333333'),
                TextComponent(text=f"{sign} ${data.get('amount')}", size='4xl', weight='bold', color=theme_color, margin='sm'),
                BoxComponent(
                    layout='vertical', margin='lg', spacing='sm',
                    contents=[
                        BoxComponent(
                            layout='baseline', spacing='sm',
                            contents=[
                                TextComponent(text='分類', color='#aaaaaa', size='sm', flex=1),
                                TextComponent(text=data.get('category'), color='#666666', size='sm', flex=4, weight="bold")
                            ],
                        ),
                        BoxComponent(
                            layout='baseline', spacing='sm',
                            contents=[
                                TextComponent(text='日期', color='#aaaaaa', size='sm', flex=1),
                                TextComponent(text=data.get('date'), color='#666666', size='sm', flex=4)
                            ],
                        ),
                    ],
                )
            ],
        )
    )

# --- Routes ---
@app.route("/")
def home():
    return "OK - Bot Running", 200

@app.route("/login")
def login():
    line_user_id = request.args.get('userid')
    if not line_user_id: return "錯誤：無效的使用者 ID"
    session.permanent = True
    session['line_user_id'] = line_user_id
    client_config = {"web": {"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}
    redirect_uri = url_for('oauth2callback', _external=True)
    flow = Flow.from_client_config(client_config=client_config, scopes=SCOPES, redirect_uri=redirect_uri)
    authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    session['state'] = state
    return redirect(authorization_url)

@app.route("/oauth2callback")
def oauth2callback():
    if 'state' not in session: return "錯誤：Session 失效。"
    state = session['state']
    line_user_id = session.get('line_user_id')
    if not line_user_id: return "錯誤：無法識別使用者。"
    client_config = {"web": {"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}
    redirect_uri = url_for('oauth2callback', _external=True)
    flow = Flow.from_client_config(client_config=client_config, scopes=SCOPES, state=state, redirect_uri=redirect_uri)
    try:
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        try:
            user_info_service = build('oauth2', 'v2', credentials=creds)
            user_info = user_info_service.userinfo().get().execute()
            user_email = user_info.get('email')
        except: user_email = "unknown"
        
        creds_data = {'google_email': user_email, 'token': creds.token, 'refresh_token': creds.refresh_token, 'token_uri': creds.token_uri, 'client_id': creds.client_id, 'client_secret': creds.client_secret, 'scopes': creds.scopes}
        save_user_credentials(line_user_id, creds_data)
        try:
            line_bot_api.push_message(line_user_id, TextSendMessage(text=f"🎉 綁定成功！帳號：{user_email}", quick_reply=get_quick_reply(line_user_id)))
        except: pass
        return f"綁定成功！帳號：{user_email}。請關閉視窗。"
    except Exception as e: return f"綁定失敗：{e}"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return "OK"

def execute_api_logic(user_id, function_name, args):
    creds_info = get_user_credentials(user_id)
    if not creds_info or not creds_info.get('refresh_token'):
        return "錯誤：請先登入。"

    # 記帳
    if function_name == "add_accounting_entry":
        try:
            ledger_id = get_default_ledger_id(user_id)
            if not ledger_id: return "錯誤：無法取得帳本。"
            now_iso = datetime.datetime.now().strftime("%Y-%m-%d")
            entry_data = {'type': args.get('type', 'expense'), 'amount': float(args.get('amount', 0)), 'categoryId': args.get('category', '其他'), 'note': args.get('item', '') + ' ' + args.get('note', ''), 'date': now_iso, 'createdAt': firestore.SERVER_TIMESTAMP, 'updatedAt': firestore.SERVER_TIMESTAMP, 'source': 'line-bot'}
            db.collection('users').document(user_id).collection('ledgers').document(ledger_id).collection('entries').add(entry_data)
            return {'status': 'success', 'action': 'accounting', 'data': {'item': args.get('item', ''), 'amount': entry_data['amount'], 'category': entry_data['categoryId'], 'type': entry_data['type'], 'date': entry_data['date']}}
        except Exception as e:
            logging.error(f"記帳失敗: {e}")
            return f"記帳錯誤: {e}"

    # 日曆
    creds = Credentials.from_authorized_user_info(creds_info)
    try:
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            creds_data = {'google_email': creds_info.get('google_email'), 'token': creds.token, 'refresh_token': creds.refresh_token, 'token_uri': creds.token_uri, 'client_id': creds.client_id, 'client_secret': creds.client_secret, 'scopes': creds.scopes}
            save_user_credentials(user_id, creds_data)
        
        service = build('calendar', 'v3', credentials=creds)
        
        if function_name == "create_calendar_event":
            summary = args.get('title') or args.get('summary') or args.get('event_name') or list(args.values())[0]
            start_time = args.get('start_time')
            end_time = args.get('end_time')
            if not end_time and start_time:
                try:
                    dt = datetime.datetime.fromisoformat(start_time)
                    end_time = (dt + datetime.timedelta(hours=1)).isoformat()
                except: pass
            event = {'summary': summary, 'description': args.get('description', ''), 'start': {'dateTime': start_time, 'timeZone': 'Asia/Taipei'}, 'end': {'dateTime': end_time, 'timeZone': 'Asia/Taipei'}}
            created_event = service.events().insert(calendarId='primary', body=event).execute()
            created_event['action'] = 'calendar_create'
            return created_event
            
        elif function_name == "get_calendar_events":
            now = datetime.datetime.utcnow().isoformat() + 'Z'
            time_min = args.get('time_min', now)
            events_result = service.events().list(calendarId='primary', timeMin=time_min, maxResults=10, singleEvents=True, orderBy='startTime').execute()
            events = events_result.get('items', [])
            if not events: return "接下來沒有行程。"
            result_text = "接下來的行程：\n"
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                summary = event.get('summary', '無標題')
                result_text += f"- {start}: {summary}\n"
            return result_text
    except Exception as e: return f"執行錯誤：{str(e)}"
    return "未知的操作。"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    user_id = event.source.user_id

    try:
        url = "https://api.line.me/v2/bot/chat/loading/start"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
        data = {"chatId": user_id, "loadingSeconds": 20}
        requests.post(url, headers=headers, json=data)
    except Exception as e:
        logging.warning(f"Failed to send loading animation: {e}")

    if user_msg == "開啟記帳模式":
        msg = """📝 歡迎使用記帳模式！
您可以直接輸入「午餐 100元」、「飲料 50」來記帳。

💡 您也可以調整我的回覆風格：
請輸入以下指令：
- 設定風格：毒舌管家
- 設定風格：溫柔秘書
- 設定風格：嚴格會計
(也可以自訂，例如「設定風格：傲嬌妹妹」)"""
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg, quick_reply=get_quick_reply(user_id)))
        return

    if user_msg.startswith("設定風格："):
        new_style = user_msg.replace("設定風格：", "").strip()
        save_user_style(user_id, new_style)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 風格已設定為「{new_style}」！", quick_reply=get_quick_reply(user_id)))
        return

    if user_msg == "清空對話":
        clear_chat_history(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🧹 對話記憶已清空！", quick_reply=get_quick_reply(user_id)))
        return

    if user_msg.lower() in ["id", "uid"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ID: {user_id}", quick_reply=get_quick_reply(user_id)))
        return

    if user_msg == "登出":
        delete_user_credentials(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="已登出！", quick_reply=get_quick_reply(user_id)))
        return

    if user_msg in ["登入", "綁定", "連結Google"]:
        login_url = url_for('login', userid=user_id, _external=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"請點擊連結進行綁定：\n{login_url}", quick_reply=get_quick_reply(user_id)))
        return

    try:
        doc = db.collection('users').document(user_id).get()
        history = []
        user_style = None
        if doc.exists:
            data = doc.to_dict()
            user_style = data.get('reply_style')
            for h in data.get('chat_history', []): history.append({"role": h['role'], "parts": [h['text']]})

        current_instruction = get_system_instruction(user_style)
        model = genai.GenerativeModel("gemini-2.0-flash", tools=tools_list, system_instruction=current_instruction)
        chat = model.start_chat(history=history, enable_automatic_function_calling=False)
        response = chat.send_message(user_msg)
        
        flex_bubbles = []
        text_responses = []

        if response.parts:
            for part in response.parts:
                if part.function_call:
                    fc = part.function_call
                    fname = fc.name
                    fargs = dict(fc.args)
                    api_result = execute_api_logic(user_id, fname, fargs)
                    if isinstance(api_result, dict):
                        if api_result.get('action') == 'accounting':
                            flex_bubbles.append(create_accounting_bubble(api_result['data']))
                            save_chat_history(user_id, user_msg, f"已記帳：{api_result['data']['item']}")
                        elif api_result.get('action') == 'calendar_create':
                            flex_bubbles.append(create_event_bubble(api_result))
                            save_chat_history(user_id, user_msg, f"已建立行程：{api_result.get('summary')}")
                        else:
                            text_responses.append(str(api_result))
                    else:
                        text_responses.append(str(api_result))
                    chat.send_message({"function_response": {"name": fname, "response": {"result": "Success" if isinstance(api_result, dict) else str(api_result)}}})
                elif part.text:
                    text_responses.append(part.text)
                    save_chat_history(user_id, user_msg, part.text)

        reply_messages = []
        if flex_bubbles:
            if len(flex_bubbles) > 1:
                container = CarouselContainer(contents=flex_bubbles)
                reply_messages.append(FlexSendMessage(alt_text="處理結果", contents=container))
            else:
                reply_messages.append(FlexSendMessage(alt_text="處理結果", contents=flex_bubbles[0]))
        
        if text_responses:
            combined_text = "\n".join(text_responses).strip()
            if combined_text:
                reply_messages.append(TextSendMessage(text=combined_text))

        if reply_messages:
            reply_messages[-1].quick_reply = get_quick_reply(user_id)
            line_bot_api.reply_message(event.reply_token, reply_messages)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="處理完成", quick_reply=get_quick_reply(user_id)))

    except Exception as e:
        logging.exception("Error")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="系統忙碌中", quick_reply=get_quick_reply(user_id)))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
