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

# --- 初始化 ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    # 🔥 關鍵診斷：啟動時檢查 Key 權限與可用模型
    try:
        logging.info("🔍 --- 開始檢查 Google Gemini API Key ---")
        logging.info(f"使用的 Key 前五碼: {GEMINI_API_KEY[:5]}...")
        available_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                available_models.append(m.name)
        logging.info(f"✅ 你的 Key 可以使用的模型: {available_models}")
        if not available_models:
            logging.error("❌ 警告：你的 Key 似乎無法存取任何生成模型！請檢查 Google Cloud 專案是否啟用了 'Generative Language API'。")
    except Exception as e:
        logging.error(f"❌ API Key 檢查失敗 (這代表 Key 無效或權限不足): {e}")

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

# --- 內嵌 HTML ---
# 1. 後台管理頁面 (整合你上傳的 admin.html)
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI 助理 - 對話紀錄後台</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        * { box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); height: 100vh; margin: 0; display: flex; flex-direction: column; color: #333; }
        .glass-card { background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(18px); border: 1px solid rgba(255, 255, 255, 0.35); border-radius: 20px; box-shadow: 0 10px 40px rgba(0, 0, 0, 0.08); overflow: hidden; transition: all 0.3s ease; }
        .main-container { flex: 1; display: flex; overflow: hidden; max-width: 1400px; margin: 0 auto; width: 100%; padding: 20px; gap: 20px; }
        .sidebar { width: 340px; display: flex; flex-direction: column; background: rgba(255, 255, 255, 0.9); backdrop-filter: blur(12px); border-radius: 20px; border: 1px solid rgba(255, 255, 255, 0.4); box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1); }
        .sidebar-header { padding: 20px 20px 10px 20px; background: transparent; display: flex; justify-content: space-between; align-items: center; }
        .sidebar-title { font-size: 1.2rem; font-weight: 800; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .search-box { padding: 0 20px 15px 20px; border-bottom: 1px solid rgba(0,0,0,0.05); }
        .search-input { width: 100%; padding: 8px 12px; border-radius: 10px; border: 1px solid #e0e0e0; background: rgba(255,255,255,0.8); transition: all 0.2s; }
        .search-input:focus { outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1); }
        .user-list { flex: 1; overflow-y: auto; padding: 10px; }
        .user-item { padding: 12px 16px; border-radius: 12px; margin-bottom: 8px; cursor: pointer; transition: all 0.2s ease; border: 1px solid transparent; background: rgba(255,255,255,0.6); }
        .user-item:hover { transform: translateY(-2px); background: white; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }
        .user-item.active { background: #f3e8ff; border-color: #d8b4fe; color: #6b21a8; }
        .user-name { font-weight: bold; display: block; font-size: 0.95rem; margin-bottom: 2px; }
        .user-uid { font-size: 0.7rem; color: #888; font-family: monospace; word-break: break-all; line-height: 1.2; }
        .chat-area { flex: 1; display: flex; flex-direction: column; background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(18px); border-radius: 20px; box-shadow: 0 10px 40px rgba(0, 0, 0, 0.1); }
        .chat-header { padding: 20px; border-bottom: 1px solid rgba(0,0,0,0.05); font-weight: bold; color: #444; font-size: 1.1rem; }
        .chat-box { flex: 1; padding: 20px; overflow-y: auto; background: #fcfaff; display: flex; flex-direction: column; gap: 15px; }
        .message { max-width: 75%; padding: 12px 16px; border-radius: 16px; position: relative; font-size: 0.95rem; line-height: 1.6; word-wrap: break-word; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
        .message.user { align-self: flex-end; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border-radius: 16px 4px 16px 16px; }
        .message.user .timestamp { color: rgba(255,255,255,0.8); }
        .message.model { align-self: flex-start; background: white; color: #333; border: 1px solid #eef2f6; border-radius: 4px 16px 16px 16px; }
        .timestamp { font-size: 0.7rem; color: #999; margin-top: 6px; text-align: right; }
        .btn-custom-primary { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; padding: 8px 16px; border-radius: 10px; font-weight: 600; transition: all 0.2s; }
        .btn-custom-primary:hover { opacity: 0.9; transform: translateY(-1px); color: white; box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4); }
        .btn-custom-secondary { background: #f3f4f6; color: #374151; border: none; padding: 6px 12px; border-radius: 8px; font-weight: 600; font-size: 0.85rem; transition: all 0.2s; }
        .btn-custom-secondary:hover { background: #e5e7eb; color: #111; }
        .btn-icon { padding: 6px 10px; font-size: 1rem; }
        #login-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(135deg, rgba(102,126,234,0.4), rgba(118,75,162,0.4)); backdrop-filter: blur(10px); z-index: 9999; display: flex; align-items: center; justify-content: center; flex-direction: column; }
        .login-card { background: rgba(255,255,255,0.95); padding: 40px; border-radius: 24px; box-shadow: 0 20px 60px rgba(0,0,0,0.15); text-align: center; border: 1px solid rgba(255,255,255,0.5); width: 320px; }
        @media (max-width: 768px) { .main-container { flex-direction: column; padding: 10px; } .sidebar { width: 100%; height: 300px; margin-bottom: 10px; } }
    </style>
</head>
<body>
    <div id="login-overlay">
        <div class="login-card">
            <h3 class="mb-4 fw-bold" style="color:#4a4a4a;">🔐 後台登入</h3>
            <p class="text-muted mb-4" style="font-size:0.9rem;">請使用管理員帳號登入</p>
            <button class="btn-custom-primary w-100 py-2 d-flex align-items-center justify-content-center gap-2" onclick="googleLogin()">
                Google 登入
            </button>
            <p id="login-msg" class="mt-3 text-danger mb-0" style="font-size:0.8rem;"></p>
        </div>
    </div>
    <div class="main-container" id="app-container" style="display: none;">
        <div class="sidebar">
            <div class="sidebar-header">
                <span class="sidebar-title">👥 AI 助理後台</span>
                <div class="d-flex gap-2"><button class="btn-custom-secondary btn-icon" onclick="loadUserList()" title="重新整理">🔄</button><button class="btn-custom-secondary" onclick="logout()">登出</button></div>
            </div>
            <div class="search-box"><input type="text" id="user-search" class="search-input" placeholder="🔍 搜尋..."></div>
            <div class="user-list" id="user-list"><div class="text-center p-3 text-muted">載入中...</div></div>
        </div>
        <div class="chat-area">
            <div class="chat-header" id="chat-header">請選擇使用者</div>
            <div class="chat-box" id="chat-box"><div class="text-center mt-5 text-muted"><div style="font-size: 3rem; margin-bottom: 10px;">👈</div>請從左側選擇一位使用者</div></div>
        </div>
    </div>
    <script type="module">
        import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-app.js';
        import { getAuth, signInWithPopup, GoogleAuthProvider, signOut, onAuthStateChanged } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-auth.js';
        import { getFirestore, collection, getDocs, query, orderBy, limit } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
        const firebaseConfig = { apiKey: "AIzaSyBKDZvPGy0ovaN0PMFixMDjNB-pSJbUCBQ", authDomain: "ai-assistant-8adfd.firebaseapp.com", projectId: "ai-assistant-8adfd", storageBucket: "ai-assistant-8adfd.firebasestorage.app", messagingSenderId: "578156755980", appId: "1:578156755980:web:608a5d27a479a4103ac229", measurementId: "G-V9W1NW82Q7" };
        const app = initializeApp(firebaseConfig); const auth = getAuth(app); const db = getFirestore(app); const ADMIN_EMAIL = "bruce9811123@gmail.com";
        let allUsersCache = [];
        window.googleLogin = async () => { const provider = new GoogleAuthProvider(); try { await signInWithPopup(auth, provider); } catch (error) { document.getElementById('login-msg').textContent = "登入失敗：" + error.message; } };
        window.logout = () => { signOut(auth); window.location.reload(); };
        onAuthStateChanged(auth, async (user) => { const overlay = document.getElementById('login-overlay'); const container = document.getElementById('app-container'); const msg = document.getElementById('login-msg');
            if (user) { if (user.email.toLowerCase() === ADMIN_EMAIL.toLowerCase()) { overlay.style.display = 'none'; container.style.display = 'flex'; document.getElementById('user-search').addEventListener('input', renderUserList); loadUserList(); } else { msg.textContent = "權限不足"; setTimeout(() => signOut(auth), 2000); } } else { overlay.style.display = 'flex'; container.style.display = 'none'; } });
        window.loadUserList = async function() { const listContainer = document.getElementById('user-list'); listContainer.innerHTML = '<div class="text-center p-3 text-muted">載入中...</div>'; try { const q = query(collection(db, "users")); const snapshot = await getDocs(q); allUsersCache = []; if (snapshot.empty) { listContainer.innerHTML = '<div class="p-3 text-center text-muted">無資料</div>'; return; } snapshot.forEach((doc) => { allUsersCache.push({ id: doc.id, ...doc.data() }); }); renderUserList(); } catch (e) { console.error(e); listContainer.innerHTML = `<div class="p-3 text-danger">失敗：${e.message}</div>`; } }
        function renderUserList() { const listContainer = document.getElementById('user-list'); const searchTerm = document.getElementById('user-search').value.trim().toLowerCase(); listContainer.innerHTML = ''; const filteredUsers = allUsersCache.filter(user => { const name = (user.google_email || "").toLowerCase(); const uid = user.id.toLowerCase(); return name.includes(searchTerm) || uid.includes(searchTerm); }); if (filteredUsers.length === 0) { listContainer.innerHTML = '<div class="p-3 text-center text-muted">無結果</div>'; return; } filteredUsers.forEach(user => { const div = document.createElement('div'); div.className = 'user-item'; const displayName = user.google_email || "未綁定"; const fullUid = user.id; div.innerHTML = `<span class="user-name">${displayName}</span><span class="user-uid">UID: ${fullUid}</span>`; div.onclick = () => { document.querySelectorAll('.user-item').forEach(el => el.classList.remove('active')); div.classList.add('active'); loadChatHistory(user.id, displayName); }; listContainer.appendChild(div); }); }
        async function loadChatHistory(uid, name) { document.getElementById('chat-header').textContent = `正在查看：${name}`; const chatBox = document.getElementById('chat-box'); chatBox.innerHTML = '<div class="text-center mt-5 text-muted">載入中...</div>'; try { const q = query(collection(db, "users", uid, "full_logs"), orderBy("timestamp", "asc"), limit(100)); const snapshot = await getDocs(q); if (snapshot.empty) { chatBox.innerHTML = '<div class="text-center mt-5 text-muted">無紀錄</div>'; return; } chatBox.innerHTML = ''; snapshot.forEach(doc => { const msg = doc.data(); let date = ''; if (msg.timestamp && msg.timestamp.seconds) { date = new Date(msg.timestamp.seconds * 1000).toLocaleString('zh-TW', { hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }); } if (msg.user) { const uDiv = document.createElement('div'); uDiv.className = 'message user'; uDiv.innerHTML = `${escapeHtml(msg.user)}<div class="timestamp">${date}</div>`; chatBox.appendChild(uDiv); } if (msg.model) { const mDiv = document.createElement('div'); mDiv.className = 'message model'; mDiv.innerHTML = `${escapeHtml(msg.model)}<div class="timestamp">${date}</div>`; chatBox.appendChild(mDiv); } }); chatBox.scrollTop = chatBox.scrollHeight; } catch (e) { console.error(e); chatBox.innerHTML = '<div class="text-center mt-5 text-danger">讀取失敗</div>'; } }
        function escapeHtml(text) { if (!text) return ""; return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;").replace(/\n/g, "<br>"); }
    </script>
</body>
</html>
"""

# 2. 收支日記頁面
JOURNAL_HTML = """<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>收支日記</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"></head><body class="bg-light"><div class="container py-4"><h3 class="text-center mb-4">📖 收支日記</h3><div class="card p-3 mb-3"><div class="d-flex justify-content-between"><div>收入 <span class="text-success">+{{ total_income }}</span></div><div>支出 <span class="text-danger">-{{ total_expense }}</span></div><div>結餘 <b>{{ total_income - total_expense }}</b></div></div></div><ul class="list-group">{% for e in entries %}<li class="list-group-item d-flex justify-content-between"><div><b>{{ e.note }}</b><br><small class="text-muted">{{ e.date }}</small></div><span class="{{ 'text-success' if e.type=='income' else 'text-danger' }}">{{ '+' if e.type=='income' else '-' }}{{ e.amount }}</span></li>{% endfor %}</ul></div></body></html>"""

# --- Routes ---
@app.route("/")
def home(): return "OK - Bot is running", 200

@app.route("/admin")
def admin_page():
    return render_template_string(ADMIN_HTML)

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

# --- 資料庫函式 ---
def save_user_credentials(user_id, creds_data):
    try: db.collection('users').document(user_id).set(creds_data, merge=True)
    except Exception as e: logging.error(f"DB Error: {e}")

def get_user_credentials(user_id):
    try:
        doc = db.collection('users').document(user_id).get()
        return doc.to_dict() if doc.exists else None
    except: return None

def delete_user_credentials(user_id):
    try: db.collection('users').document(user_id).delete(); return True
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
    try: db.collection('users').document(user_id).set({'chat_history': []}, merge=True); return True
    except: return False

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
def create_calendar_event(title: str, start_time: str, end_time: str = None, description: str = ""): return "Event request received."
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

# --- Bubbles ---
def create_introduction_bubble(): return BubbleContainer(body=BoxComponent(layout='vertical', contents=[TextComponent(text='👋 我是 AI 助理', weight='bold', size='xl', align='center'), TextComponent(text='日曆・記帳・郵件', align='center', margin='md')]), footer=BoxComponent(layout='vertical', contents=[ButtonComponent(style='primary', action=MessageAction(label='✨ 試試看', text='幫我新增明天早上9點開會'))]))
def create_event_bubble(event_data):
    summary = event_data.get('summary') or '未命名行程'
    start = event_data['start'].get('dateTime', event_data['start'].get('date'))
    return BubbleContainer(header=BoxComponent(layout='horizontal', backgroundColor='#1DB446', contents=[TextComponent(text='📅 行程已建立', color='#ffffff', weight='bold')]), body=BoxComponent(layout='vertical', contents=[TextComponent(text=summary, weight='bold', size='xl'), TextComponent(text=start, size='sm', color='#666666', margin='md')]), footer=BoxComponent(layout='vertical', contents=[ButtonComponent(style='secondary', action=URIAction(label='查看', uri=event_data.get('htmlLink')))]))
def create_accounting_bubble(data, user_id):
    color = '#10b981' if data.get('type') == 'income' else '#ef4444'
    return BubbleContainer(body=BoxComponent(layout='vertical', contents=[TextComponent(text=data.get('category', '其他'), weight='bold', color=color), TextComponent(text=f"${int(data.get('amount'))}", weight='bold', size='3xl', margin='md'), TextComponent(text=f"備註: {data.get('item', '')}", size='sm', color='#aaaaaa', margin='md')]), footer=BoxComponent(layout='vertical', contents=[ButtonComponent(style='secondary', action=URIAction(label='編輯', uri=url_for('view_journal', userid=user_id, _external=True)))]))

# --- API Logic ---
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
    
    if msg == "清空對話": clear_chat_history(uid); line_bot_api.reply_message(event.reply_token, TextSendMessage(text="已清空", quick_reply=get_quick_reply(uid))); return
    if msg in ["功能介紹", "請問你可以幫我做什麼？"]: line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="介紹", contents=create_introduction_bubble(), quick_reply=get_quick_reply(uid))); return
    if msg == "登出": delete_user_credentials(uid); line_bot_api.reply_message(event.reply_token, TextSendMessage(text="已登出", quick_reply=get_quick_reply(uid))); return
    if msg == "設定角色": line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入：設定風格：xxx", quick_reply=get_quick_reply(uid))); return
    if msg.startswith("設定風格："): save_user_style(uid, msg.split("：")[1]); line_bot_api.reply_message(event.reply_token, TextSendMessage(text="已設定", quick_reply=get_quick_reply(uid))); return
    if msg == "查看報表": line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="報表", contents=BubbleContainer(body=BoxComponent(layout='vertical', contents=[TextComponent(text='📊 點擊查看', align='center')]), footer=BoxComponent(layout='vertical', contents=[ButtonComponent(style='primary', action=URIAction(label='開啟', uri=url_for('view_journal', userid=uid, _external=True)))])))); return
    
    try:
        requests.post("https://api.line.me/v2/bot/chat/loading/start", headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"}, json={"chatId": uid, "loadingSeconds": 20})
        
        doc = db.collection('users').document(uid).get()
        hist = []
        style = None
        if doc.exists:
            d = doc.to_dict()
            style = d.get('reply_style')
            for h in d.get('chat_history', []): hist.append({"role": h['role'], "parts": [h['text']]})
            
        # 🔥 關鍵修改：三層備援機制，保證不死機 🔥
        try:
            # 1. 首選：Gemini 1.5 Flash (快、便宜、功能強)
            model = genai.GenerativeModel("gemini-1.5-flash", tools=tools_list, system_instruction=get_system_instruction(style))
            chat = model.start_chat(history=hist, enable_automatic_function_calling=False)
            response = chat.send_message(msg)
        except Exception as e_flash:
            logging.warning(f"⚠️ 1.5-Flash 失敗，嘗試切換至 1.0-Pro... 錯誤: {e_flash}")
            try:
                # 2. 備援：Gemini 1.0 Pro (穩定舊版)
                model = genai.GenerativeModel("gemini-1.0-pro", tools=tools_list, system_instruction=get_system_instruction(style))
                chat = model.start_chat(history=hist, enable_automatic_function_calling=False)
                response = chat.send_message(msg)
            except Exception as e_pro:
                logging.warning(f"⚠️ 1.0-Pro 失敗，嘗試切換至 gemini-pro... 錯誤: {e_pro}")
                # 3. 最後手段：gemini-pro (通用別名)
                model = genai.GenerativeModel("gemini-pro", tools=tools_list, system_instruction=get_system_instruction(style))
                chat = model.start_chat(history=hist, enable_automatic_function_calling=False)
                response = chat.send_message(msg)

        reply_objs = []
        txt_res = []
        
        if response.parts:
            for p in response.parts:
                if p.function_call:
                    api_res = execute_api_logic(uid, p.function_call.name, dict(p.function_call.args))
                    chat.send_message(genai.protos.Content(parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(name=p.function_call.name, response={"result": str(api_res)}))]))
                    if isinstance(api_res, dict):
                        if api_res.get('action') == 'accounting': reply_objs.append(create_accounting_bubble(api_res['data'], uid))
                        elif api_res.get('action') == 'calendar_create': reply_objs.append(create_event_bubble(api_res))
                    else: txt_res.append(str(api_res))
                elif p.text:
                    txt_res.append(p.text)
                    save_chat_history(uid, msg, p.text)
        
        if reply_objs: reply_objs = [FlexSendMessage(alt_text="結果", contents=CarouselContainer(contents=reply_objs) if len(reply_objs)>1 else reply_objs[0])]
        if txt_res: reply_objs.append(TextSendMessage(text="\n".join(txt_res)))
        
        if reply_objs:
            reply_objs[-1].quick_reply = get_quick_reply(uid)
            line_bot_api.reply_message(event.reply_token, reply_objs)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="處理完畢", quick_reply=get_quick_reply(uid)))
            
    except Exception as e:
        err_msg = str(e)
        # 如果還是 404，那真的是 Key 的問題，不再是程式碼問題了
        if "404" in err_msg:
            err_msg = "❌ API Key 無效：請檢查 Render Logs，程式啟動時已列出你的 Key 可用的模型。請確保 Google Cloud 專案已啟用 Generative Language API。"
        logging.error(e)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"系統錯誤: {err_msg}", quick_reply=get_quick_reply(uid)))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
