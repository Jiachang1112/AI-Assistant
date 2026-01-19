import os
import logging
import datetime
import json
import requests
from flask import Flask, request, abort, redirect, url_for, session, render_template_string
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, 
    FlexSendMessage, BubbleContainer, BoxComponent, 
    TextComponent, ButtonComponent, URIAction,
    QuickReply, QuickReplyButton, MessageAction,
    CarouselContainer, ImageComponent, SeparatorComponent, IconComponent
)

import google.generativeai as genai
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import firebase_admin
from firebase_admin import credentials, firestore

# 設定 Log
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

# 🔥 系統啟動時檢查版本 (Debug 用)
try:
    logging.info(f"GenAI Library Version: {genai.__version__}")
    logging.info("Available Models:")
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            logging.info(f" - {m.name}")
except Exception as e:
    logging.error(f"Check Models Failed: {e}")

db = None
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
    'https://www.googleapis.com/auth/gmail.readonly',
    'openid'
]

# --- 資料庫操作 ---
def save_user_credentials(user_id, creds_data):
    try:
        doc_ref = db.collection('users').document(user_id)
        doc_ref.set(creds_data, merge=True)
    except Exception as e: logging.error(f"Firebase Error: {e}")

def get_user_credentials(user_id):
    try:
        doc = db.collection('users').document(user_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        logging.error(f"Firebase Error: {e}")
        return None

def delete_user_credentials(user_id):
    try:
        db.collection('users').document(user_id).delete()
        return True
    except: return False

def save_user_style(user_id, style):
    try: db.collection('users').document(user_id).set({'reply_style': style}, merge=True)
    except: pass

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
    except: return []

def save_chat_history(user_id, user_text, model_text):
    try:
        doc_ref = db.collection('users').document(user_id)
        doc = doc_ref.get()
        current = doc.to_dict().get('chat_history', []) if doc.exists else []
        current.append({"role": "user", "text": user_text})
        current.append({"role": "model", "text": model_text})
        if len(current) > 20: current = current[-20:]
        doc_ref.set({'chat_history': current}, merge=True)
        doc_ref.collection('full_logs').add({'user': user_text, 'model': model_text, 'timestamp': firestore.SERVER_TIMESTAMP})
    except Exception as e: logging.error(f"History Error: {e}")

def clear_chat_history(user_id):
    try:
        db.collection('users').document(user_id).set({'chat_history': []}, merge=True)
        return True
    except: return False

# --- 核心邏輯 ---
def get_default_ledger_id(user_id):
    try:
        ledgers_ref = db.collection('users').document(user_id).collection('ledgers')
        snap = ledgers_ref.where('isDefault', '==', True).limit(1).get()
        if snap: return snap[0].id
        snap = ledgers_ref.order_by('createdAt').limit(1).get()
        if snap: return snap[0].id
        _, ref = ledgers_ref.add({'name': '預設帳本', 'currency': 'TWD', 'isDefault': True, 'createdAt': firestore.SERVER_TIMESTAMP})
        return ref.id
    except: return None

def get_ledger_balance(user_id, ledger_id):
    try:
        docs = db.collection('users').document(user_id).collection('ledgers').document(ledger_id).collection('entries').stream()
        balance = 0.0
        for doc in docs:
            d = doc.to_dict()
            amt = float(d.get('amount', 0))
            if d.get('type') == 'income': balance += amt
            else: balance -= amt
        return int(balance)
    except: return 0

# --- Tools ---
def create_calendar_event(title: str, start_time: str, end_time: str = None, description: str = ""): return "Event creation request received."
def get_calendar_events(time_min: str = None): return "Calendar list request received."
def add_accounting_entry(item: str, amount: float, category: str = "其他", type: str = "expense", note: str = ""): return "Accounting request received."
def get_recent_emails(query: str = "is:unread", max_results: int = 5): return "Gmail request received."
tools_list = [create_calendar_event, get_calendar_events, add_accounting_entry, get_recent_emails]

def get_system_instruction(style=None):
    now = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    instr = f"你是一個專業的 Google 日曆助理與生活記帳助手。現在時間 {now}。\n1. 時間事項必須呼叫 Calendar 工具。\n2. 金額必須呼叫記帳工具。\n3. 信箱問題呼叫 Email 工具。\n4. 未綁定請引導登入。"
    if style: instr += f"\n\n風格：{style}"
    return instr

def get_quick_reply(user_id):
    creds = get_user_credentials(user_id)
    is_logged_in = creds and creds.get('refresh_token')
    items = [
        QuickReplyButton(action=MessageAction(label="📊 查看報表", text="查看報表")),
        QuickReplyButton(action=MessageAction(label="🔍 查詢行程", text="查詢接下來的行程")),
        QuickReplyButton(action=MessageAction(label="➕ 新增範例", text="幫我新增明天早上9點開會")),
        QuickReplyButton(action=MessageAction(label="🎭 設定角色", text="設定角色")),
        QuickReplyButton(action=MessageAction(label="🧹 清空對話", text="清空對話")),
        QuickReplyButton(action=MessageAction(label="📧 查詢信件", text="查詢未讀信件")),
    ]
    if is_logged_in: items.append(QuickReplyButton(action=MessageAction(label="👋 登出", text="登出")))
    else: items.append(QuickReplyButton(action=MessageAction(label="🔗 綁定 Google", text="登入")))
    return QuickReply(items=items)

# --- Flex Messages ---
def create_introduction_bubble():
    return BubbleContainer(body=BoxComponent(layout='vertical', contents=[TextComponent(text='👋 我是 AI 助理', weight='bold', size='xl', align='center'), TextComponent(text='日曆・記帳・郵件', align='center', margin='md')]), footer=BoxComponent(layout='vertical', contents=[ButtonComponent(style='primary', action=MessageAction(label='✨ 試試看', text='幫我新增明天早上9點開會'))]))

def create_event_bubble(event_data):
    summary = event_data.get('summary') or '未命名行程'
    start = event_data['start'].get('dateTime', event_data['start'].get('date'))
    return BubbleContainer(header=BoxComponent(layout='horizontal', backgroundColor='#1DB446', contents=[TextComponent(text='📅 行程已建立', color='#ffffff', weight='bold')]), body=BoxComponent(layout='vertical', contents=[TextComponent(text=summary, weight='bold', size='xl'), TextComponent(text=start, size='sm', color='#666666', margin='md')]), footer=BoxComponent(layout='vertical', contents=[ButtonComponent(style='secondary', action=URIAction(label='查看', uri=event_data.get('htmlLink')))]))

def create_accounting_bubble(data, user_id):
    color = '#10b981' if data.get('type') == 'income' else '#ef4444'
    return BubbleContainer(body=BoxComponent(layout='vertical', contents=[TextComponent(text=data.get('category', '其他'), weight='bold', color=color), TextComponent(text=f"${int(data.get('amount'))}", weight='bold', size='3xl', margin='md'), TextComponent(text=f"備註: {data.get('item', '')}", size='sm', color='#aaaaaa', margin='md')]), footer=BoxComponent(layout='vertical', contents=[ButtonComponent(style='secondary', action=URIAction(label='編輯', uri=url_for('view_journal', userid=user_id, _external=True)))]))

JOURNAL_HTML = """<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>收支日記</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"></head><body class="bg-light"><div class="container py-4"><h3 class="text-center mb-4">📖 收支日記</h3><div class="card p-3 mb-3"><div class="d-flex justify-content-between"><div>收入 <span class="text-success">+{{ total_income }}</span></div><div>支出 <span class="text-danger">-{{ total_expense }}</span></div><div>結餘 <b>{{ total_income - total_expense }}</b></div></div></div><ul class="list-group">{% for e in entries %}<li class="list-group-item d-flex justify-content-between"><div><b>{{ e.note }}</b><br><small class="text-muted">{{ e.date }}</small></div><span class="{{ 'text-success' if e.type=='income' else 'text-danger' }}">{{ '+' if e.type=='income' else '-' }}{{ e.amount }}</span></li>{% endfor %}</ul></div></body></html>"""

@app.route("/")
def home(): return "OK", 200

@app.route("/journal")
def view_journal():
    user_id = request.args.get('userid')
    if not user_id: return "Error", 403
    try:
        ledger_id = get_default_ledger_id(user_id)
        if not ledger_id: return render_template_string(JOURNAL_HTML, entries=[], total_income=0, total_expense=0)
        docs = db.collection('users').document(user_id).collection('ledgers').document(ledger_id).collection('entries').order_by('date', direction=firestore.Query.DESCENDING).limit(30).stream()
        entries, inc, exp = [], 0, 0
        for doc in docs:
            d = doc.to_dict()
            amt = float(d.get('amount', 0))
            if d.get('type') == 'income': inc += amt
            else: exp += amt
            entries.append(d)
        return render_template_string(JOURNAL_HTML, entries=entries, total_income=int(inc), total_expense=int(exp))
    except: return "Error loading journal"

@app.route("/login")
def login():
    session['uid'] = request.args.get('userid')
    flow = Flow.from_client_config({"web": {"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}, scopes=SCOPES, redirect_uri=url_for('oauth2callback', _external=True))
    url, state = flow.authorization_url(prompt='consent')
    session['state'] = state
    return redirect(url)

@app.route("/oauth2callback")
def oauth2callback():
    if not session.get('state'): return "Session expired"
    flow = Flow.from_client_config({"web": {"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}, scopes=SCOPES, state=session['state'], redirect_uri=url_for('oauth2callback', _external=True))
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    try: email = build('oauth2', 'v2', credentials=creds).userinfo().get().execute().get('email')
    except: email = "unknown"
    save_user_credentials(session['uid'], {'google_email': email, 'token': creds.token, 'refresh_token': creds.refresh_token, 'token_uri': creds.token_uri, 'client_id': creds.client_id, 'client_secret': creds.client_secret, 'scopes': creds.scopes})
    line_bot_api.push_message(session['uid'], TextSendMessage(text=f"🎉 綁定成功：{email}", quick_reply=get_quick_reply(session['uid'])))
    return "綁定成功"

@app.route("/callback", methods=["POST"])
def callback():
    try: handler.handle(request.get_data(as_text=True), request.headers["X-Line-Signature"])
    except: abort(400)
    return "OK"

def execute_api_logic(user_id, fname, args):
    creds_info = get_user_credentials(user_id)
    if not creds_info or not creds_info.get('refresh_token'): return "請先登入"
    
    if fname == "add_accounting_entry":
        ledger_id = get_default_ledger_id(user_id)
        if not ledger_id: return "找無帳本"
        entry = {'type': args.get('type', 'expense'), 'amount': float(args.get('amount', 0)), 'categoryId': args.get('category', '其他'), 'note': args.get('item', '') + ' ' + args.get('note', ''), 'date': datetime.datetime.now().strftime("%Y-%m-%d"), 'createdAt': firestore.SERVER_TIMESTAMP}
        db.collection('users').document(user_id).collection('ledgers').document(ledger_id).collection('entries').add(entry)
        entry['balance'] = get_ledger_balance(user_id, ledger_id)
        return {'action': 'accounting', 'data': entry}

    creds = Credentials.from_authorized_user_info(creds_info)
    if creds.expired: creds.refresh(requests.Request()); save_user_credentials(user_id, {'token': creds.token, 'refresh_token': creds.refresh_token, 'token_uri': creds.token_uri, 'client_id': creds.client_id, 'client_secret': creds.client_secret, 'scopes': creds.scopes, 'google_email': creds_info.get('google_email')})
    
    if fname == "create_calendar_event":
        service = build('calendar', 'v3', credentials=creds)
        start = args.get('start_time')
        end = args.get('end_time') or start
        res = service.events().insert(calendarId='primary', body={'summary': args.get('title', '新行程'), 'start': {'dateTime': start, 'timeZone': 'Asia/Taipei'}, 'end': {'dateTime': end, 'timeZone': 'Asia/Taipei'}}).execute()
        res['action'] = 'calendar_create'
        return res
    
    if fname == "get_calendar_events":
        service = build('calendar', 'v3', credentials=creds)
        events = service.events().list(calendarId='primary', timeMin=datetime.datetime.utcnow().isoformat()+'Z', maxResults=5, singleEvents=True, orderBy='startTime').execute().get('items', [])
        return "近期行程：\n" + "\n".join([f"- {e['start'].get('dateTime', e['start'].get('date'))} {e.get('summary')}" for e in events]) if events else "無近期行程"

    if fname == "get_recent_emails":
        service = build('gmail', 'v1', credentials=creds)
        msgs = service.users().messages().list(userId='me', q=args.get('query', 'is:unread'), maxResults=5).execute().get('messages', [])
        if not msgs: return "無新信件"
        res = []
        for m in msgs:
            h = service.users().messages().get(userId='me', id=m['id'], format='metadata').execute().get('payload', {}).get('headers', [])
            sub = next((x['value'] for x in h if x['name']=='Subject'), '無題')
            res.append(f"📩 {sub}")
        return "\n".join(res)
    
    return "未知操作"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    uid = event.source.user_id
    msg = event.message.text.strip()
    if db is None: line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ DB Error")); return
    
    if msg == "清空對話": clear_chat_history(uid); line_bot_api.reply_message(event.reply_token, TextSendMessage(text="已清空", quick_reply=get_quick_reply(uid))); return
    if msg in ["功能介紹", "請問你可以幫我做什麼？"]: line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="介紹
