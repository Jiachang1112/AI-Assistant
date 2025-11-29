import os
import logging
import datetime
import json
import requests # 用來強制發送動畫請求
from flask import Flask, request, abort, redirect, url_for, session
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
# --- 加入 CarouselContainer ---
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, 
    FlexSendMessage, BubbleContainer, BoxComponent, 
    TextComponent, ButtonComponent, URIAction,
    QuickReply, QuickReplyButton, MessageAction,
    CarouselContainer
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
        logging.info(f"使用者 {user_id} 風格已設定為: {style}")
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

def create_calendar_event(title: str, start_time: str, end_time: str, description: str = ""):
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
    
    1. 當使用者想「查詢」或「新增」行程時，請呼叫對應的 calendar function tool。
    2. 當使用者輸入金額、品項（例如：午餐 100、喝飲料 50、領薪水 30000），請務必呼叫 `add_accounting_entry` tool。
       - 若是花錢，type 為 'expense'；若是賺錢，type 為 'income'。
       - 請自動推斷 category (如: 餐飲, 交通, 娛樂, 收入)。
    3. 若使用者尚未登入或綁定，請引導他們輸入「登入」。
    4. 回應時請使用繁體中文 (Traditional Chinese)。
    5. 完成記帳後，請給予簡短的確認與評語。
    """
    if style:
        base_instruction += f"\n\n【重要指令】請務必依照以下「{style}」的風格與語氣來回應(包含記帳評語)：\n{style}"
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

# --- 卡片樣式 ---
def create_event_bubble(event_data):
    summary = event_data.get('summary', '無標題')
    html_link = event_data.get('htmlLink')
    start = event_data['start']
    if 'dateTime' in start:
        time_str = start['dateTime'].replace('T', ' ')[:16]
    else:
        time_str = f"{start['date']} (全天)"

    return BubbleContainer(
        body=BoxComponent(
            layout='vertical',
            contents=[
                TextComponent(text='📅 行程已建立', weight='bold', color='#1DB446', size='sm'),
                TextComponent(text=summary, weight='bold', size='xl', margin='md', wrap=True),
                BoxComponent(
                    layout='vertical', margin='lg', spacing='sm',
                    contents=[
                        BoxComponent(
                            layout='baseline', spacing='sm',
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
            layout='vertical', spacing='sm',
            contents=[
                ButtonComponent(
                    style='link', height='sm',
                    action=URIAction(label='編輯 / 查看行程', uri=html_link)
                )
            ],
            flex=0
        )
    )

def create_accounting_bubble(data):
    is_income = data.get('type') == 'income'
    color = '#10b981' if is_income else '#ef4444'
    sign = '+' if is_income else '-'
    
    return BubbleContainer(
        body=BoxComponent(
            layout='vertical',
            contents=[
                TextComponent(text='💰 記帳成功', weight='bold', color=color, size='sm'),
                TextComponent(text=data.get('item', '未命名'), weight='bold', size='xl', margin='md'),
                TextComponent(text=f"{sign} ${data.get('amount')}", size='3xl', weight='bold', color=color, margin='md'),
                BoxComponent(
                    layout='vertical', margin='lg', spacing='sm',
                    contents=[
                        BoxComponent(
                            layout='baseline', spacing='sm',
                            contents=[
                                TextComponent(text='分類', color='#aaaaaa', size='sm', flex=1),
                                TextComponent(text=data.get('category'), color='#666666', size='sm', flex=5)
                            ],
                        ),
                        BoxComponent(
                            layout='baseline', spacing='sm',
                            contents=[
                                TextComponent(text='日期', color='#aaaaaa', size='sm', flex=1),
                                TextComponent(text=data.get('date'), color='#666666', size='sm', flex=5)
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

    if function_name == "add_accounting_entry":
        try:
            ledger_id = get_default_ledger_id(user_id)
            if not ledger_id: return "錯誤：無法取得帳本。"
            
            now_iso = datetime.datetime.now().strftime("%Y-%m-%d")
            entry_data = {
                'type': args.get('type', 'expense'),
                'amount': float(args.get('amount', 0)),
                'categoryId': args.get('category', '其他'),
                'note': args.get('item', '') + ' ' + args.get('note', ''),
                'date': now_iso,
                'createdAt': firestore.SERVER_TIMESTAMP,
                'updatedAt': firestore.SERVER_TIMESTAMP,
                'source': 'line-bot'
            }
            
            db.collection('users').document(user_id).collection('ledgers').document(ledger_id).collection('entries').add(entry_data)
              
            return {
                'status': 'success',
                'action': 'accounting',
                'data': {
                    'item': args.get('item', ''),
                    'amount': entry_data['amount'],
                    'category': entry_data['categoryId'],
                    'type': entry_data['type'],
                    'date': entry_data['date']
                }
            }
        except Exception as e:
            logging.error(f"記帳失敗: {e}")
            return f"記帳錯誤: {e}"

    creds = Credentials.from_authorized_user_info(creds_info)
    try:
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            creds_data = {'google_email': creds_info.get('google_email'), 'token': creds.token, 'refresh_token': creds.refresh_token, 'token_uri': creds.token_uri, 'client_id': creds.client_id, 'client_secret': creds.client_secret, 'scopes': creds.scopes}
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

    # 載入動畫
    try:
        url = "https://api.line.me/v2/bot/chat/loading/start"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
        data = {"chatId": user_id, "loadingSeconds": 20}
        requests.post(url, headers=headers, json=data)
    except Exception as e:
        logging.warning(f"Failed to send loading animation: {e}")

    # 指令區
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
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"請點擊連結：\n{login_url}", quick_reply=get_quick_reply(user_id)))
        return

    # Gemini 區
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
        
        # --- 修正：使用列表來收集多張卡片與回應，支援 Carousel ---
        flex_bubbles = []
        text_responses = []
        func_response_parts = [] # 用來一次回傳給 Gemini

        if response.parts:
            for part in response.parts:
                if part.function_call:
                    fc = part.function_call
                    fname = fc.name
                    fargs = dict(fc.args)
                    
                    api_result = execute_api_logic(user_id, fname, fargs)
                    
                    # 製作卡片
                    if isinstance(api_result, dict):
                        if api_result.get('action') == 'accounting':
                            flex_bubbles.append(create_accounting_bubble(api_result['data']))
                            save_chat_history(user_id, user_msg, f"已記帳：{api_result['data']['item']}")
                        elif api_result.get('action') == 'calendar_create':
                            flex_bubbles.append(create_event_bubble(api_result))
                            save_chat_history(user_id, user_msg, f"已建立行程：{api_result.get('summary')}")
                        else:
                            # 其他結果轉文字
                            text_responses.append(str(api_result))
                    else:
                        text_responses.append(str(api_result))
                    
                    # 準備回傳給 Gemini 的資料
                    func_response_parts.append({
                        "function_response": {
                            "name": fname,
                            "response": {"result": api_result}
                        }
                    })
                
                elif part.text:
                    text_responses.append(part.text)
                    save_chat_history(user_id, user_msg, part.text)

        # 如果有執行函式，回報給 Gemini (一次性回報，避免狀態錯亂)
        if func_response_parts:
            follow_up = chat.send_message(func_response_parts)
            if follow_up.text:
                text_responses.append(follow_up.text)
                save_chat_history(user_id, "System", follow_up.text)

        # --- 組合最終回應 ---
        reply_messages = []
        
        # 1. 處理卡片 (1張單發，多張 Carousel)
        if flex_bubbles:
            if len(flex_bubbles) > 1:
                container = CarouselContainer(contents=flex_bubbles)
                reply_messages.append(FlexSendMessage(alt_text="處理結果", contents=container))
            else:
                reply_messages.append(FlexSendMessage(alt_text="處理結果", contents=flex_bubbles[0]))
        
        # 2. 處理文字
        if text_responses:
            combined_text = "\n".join(text_responses).strip()
            if combined_text:
                reply_messages.append(TextSendMessage(text=combined_text))

        # 3. 發送
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
