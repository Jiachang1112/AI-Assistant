// assets/js/pages/admin.js

import { auth, db } from '../firebase.js';
import {
  onAuthStateChanged,
  GoogleAuthProvider,
  signInWithPopup,
  signInWithRedirect,
  getRedirectResult,
  signOut,
} from 'https://www.gstatic.com/firebasejs/12.3.0/firebase-auth.js';

import {
  collection, query, orderBy, limit, onSnapshot,
  addDoc, serverTimestamp, where, getDocs, Timestamp,
  doc, getDoc
} from 'https://www.gstatic.com/firebasejs/12.3.0/firebase-firestore.js';

const $  = (sel, root=document) => root.querySelector(sel);
const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));
const toTW = ts => {
  try {
    const d = ts?.toDate ? ts.toDate() : (ts instanceof Date ? ts : null);
    return d ? d.toLocaleString('zh-TW',{hour12:false}) : '-';
  } catch { return '-'; }
};
function escapeHTML(s){
  return String(s||'').replace(/[&<>"']/g, m=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[m]));
}

const ADMIN_EMAILS = ['bruce9811123@gmail.com'].map(s=>s.trim().toLowerCase());

function isAdmin(user){
  if(!user) return false;
  const email = (user.email||'').trim().toLowerCase();
  return ADMIN_EMAILS.includes(email);
}

function ensureHomeStyles(){
  if ($('#home-css')) return;
  const css = document.createElement('style');
  css.id = 'home-css';
  css.textContent = `
  :root{--bg:#0f1318;--fg:#e6e6e6;--border:#2a2f37;--card:#151a21}
  body{background:var(--bg);color:var(--fg);margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto}
  .admin-shell{max-width:1000px;margin:auto;padding:28px}
  .hero{background:linear-gradient(135deg, rgba(59,130,246,.15), rgba(168,85,247,.10));
        border:1px solid var(--border);border-radius:18px;padding:20px;
        display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}
  .hero h5{margin:0;font-weight:800}
  .muted{color:#9aa3af}
  .btn{background:none;border:1px solid #e6e6e6;color:#e6e6e6;border-radius:10px;padding:6px 12px;cursor:pointer}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:20px;cursor:pointer;transition:.2s}
  .card:hover{border-color:#60a5fa;transform:translateY(-2px)}
  .card h4{margin:0 0 6px 0}
  .backbar{display:flex;gap:8px;margin-bottom:12px}
  .table-wrap{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:14px}
  .toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 14px}
  input, select{background:#0f1318;border:1px solid #2a2f37;color:#e6e6e6;border-radius:8px;padding:6px 10px}
  .chip{border:1px solid #2a2f37;border-radius:999px;padding:.25rem .7rem;background:#0b1220}
  table{width:100%;border-collapse:collapse}
  th,td{border-bottom:1px solid #2a2f37;padding:8px 10px;text-align:left}
  th{color:#9aa3af;font-weight:700}
  .chat-bubble{margin-bottom:8px;padding:8px 12px;border-radius:12px;max-width:80%;line-height:1.5}
  .chat-bubble.user{background:#2563eb;color:white;margin-left:auto;border-bottom-right-radius:2px}
  .chat-bubble.model{background:#374151;color:#e5e7eb;margin-right:auto;border-bottom-left-radius:2px}
  .chat-time{font-size:11px;opacity:0.7;text-align:right;margin-top:2px}
  `;
  document.head.appendChild(css);
}

function showLogin(root){
  const el = document.createElement('div');
  el.className = 'admin-shell';
  el.innerHTML = `
    <div class="card" style="text-align:center">
      <h3>管理員登入</h3>
      <p class="muted">請使用 Google 登入進入後台</p>
      <button id="googleLogin" class="btn">使用 Google 登入</button>
      <div id="loginErr" class="muted" style="margin-top:8px;color:#ef4444"></div>
    </div>
  `;
  root.replaceChildren(el);

  const provider = new GoogleAuthProvider();
  $('#googleLogin', el)?.addEventListener('click', async ()=>{
    try{ await signInWithPopup(auth, provider); }
    catch(err){
      if(err?.code==='auth/popup-blocked' || err?.code==='auth/cancelled-popup-request'){
        await signInWithRedirect(auth, provider);
      }else{
        $('#loginErr', el).textContent = err.message || '登入失敗';
      }
    }
  });
}

function renderHome(root){
  ensureHomeStyles();
  const el = document.createElement('div');
  el.className = 'admin-shell';
  el.innerHTML = `
    <div class="hero">
      <div>
        <h5>歡迎回來 👋</h5>
        <div class="muted">請選擇要進入的管理項目</div>
      </div>
      <button id="logoutBtn" class="btn">登出</button>
    </div>

    <div class="grid">
      <div class="card" id="ledgerCard">
        <h4>用戶記帳</h4>
        <div class="muted">查看或管理用戶的記帳紀錄</div>
      </div>

      <div class="card" id="chatLogCard">
        <h4>用戶對話</h4>
        <div class="muted">查看 LINE 機器人的完整對話紀錄</div>
      </div>

      <div class="card" id="loginLogCard">
        <h4>用戶登入</h4>
        <div class="muted">查看登入日誌</div>
      </div>

      <div class="card" id="ordersCard">
        <h4>訂單管理</h4>
        <div class="muted">查看與管理用戶訂單</div>
      </div>
    </div>
  `;

  $('#logoutBtn', el)?.addEventListener('click', async ()=>{
    if(confirm('確定要登出嗎？')){ try{ await signOut(auth); }catch(e){ alert('登出失敗：'+e.message); } }
  });
  $('#ledgerCard', el)?.addEventListener('click', ()=> mountLedgerModule(root));
  $('#loginLogCard', el)?.addEventListener('click', ()=> mountLoginLogModule(root));
  $('#ordersCard', el)?.addEventListener('click', ()=> mountOrdersModule(root));
  
  // 新增：點擊進入對話監控
  $('#chatLogCard', el)?.addEventListener('click', ()=> mountChatLogModule(root));

  root.replaceChildren(el);
}

// --- Chat Log Module ---
async function mountChatLogModule(root){
  ensureHomeStyles();
  const el = document.createElement('div');
  el.className = 'admin-shell';
  el.innerHTML = `
    <div class="backbar">
      <button id="backHome" class="btn">&larr; 返回選單</button>
    </div>
    <div class="hero">
      <div>
        <h5>用戶對話紀錄</h5>
        <div class="muted">查看所有使用者的完整對話（含已清空）</div>
      </div>
    </div>
    <div class="table-wrap">
      <div class="toolbar">
        <select id="userSel"><option>載入用戶中...</option></select>
        <button id="btnRefresh" class="btn">重新整理</button>
      </div>
      <div id="chatBox" style="height:500px;overflow-y:auto;padding:10px;border:1px solid #2a2f37;border-radius:12px;background:#0b0f14">
        <div class="muted text-center mt-5">請選擇用戶以查看對話</div>
      </div>
    </div>
  `;
  
  $('#backHome', el).onclick = ()=> renderHome(root);
  const userSel = $('#userSel', el);
  const chatBox = $('#chatBox', el);
  const btnRefresh = $('#btnRefresh', el);

  // 1. 載入用戶列表
  const usersSnap = await getDocs(collection(db, 'users'));
  const users = usersSnap.docs.map(d => {
    const data = d.data();
    // 優先顯示 google_email，沒有的話顯示 UID
    const label = data.google_email || data.email || d.id;
    return { id: d.id, label };
  });
  
  userSel.innerHTML = `<option value="">-- 請選擇用戶 --</option>` + 
    users.map(u => `<option value="${u.id}">${u.label}</option>`).join('');

  // 2. 載入對話函式
  async function loadChat(uid){
    if(!uid) return;
    chatBox.innerHTML = '<div class="muted text-center mt-5">載入對話中...</div>';
    
    const q = query(
      collection(db, 'users', uid, 'full_logs'), 
      orderBy('timestamp', 'asc'), 
      limit(100) // 限制載入最近 100 筆
    );
    
    const snap = await getDocs(q);
    if(snap.empty){
      chatBox.innerHTML = '<div class="muted text-center mt-5">此用戶尚無對話紀錄</div>';
      return;
    }

    chatBox.innerHTML = snap.docs.map(d => {
      const msg = d.data();
      const time = toTW(msg.timestamp);
      // 顯示 User 說的話
      const userHtml = msg.user ? 
        `<div class="chat-bubble user">
           ${escapeHTML(msg.user)}
           <div class="chat-time">${time}</div>
         </div>` : '';
      // 顯示 AI 說的話
      const modelHtml = msg.model ? 
        `<div class="chat-bubble model">
           ${escapeHTML(msg.model)}
           <div class="chat-time">${time}</div>
         </div>` : '';
      return userHtml + modelHtml;
    }).join('');
    
    // 捲動到底部
    chatBox.scrollTop = chatBox.scrollHeight;
  }

  userSel.onchange = () => loadChat(userSel.value);
  btnRefresh.onclick = () => loadChat(userSel.value);

  root.replaceChildren(el);
}

// (以下保留你原本的 LogUserLogin, mountLoginLogModule, mountOrdersModule, mountLedgerModule 等函式，這裡省略不重複貼，請保留原檔後面部分)
// ... (請把原檔後面的程式碼貼回來，或者直接把 mountChatLogModule 函式插入到 AdminPage 之前)

// --- 補上原本的 AdminPage 匯出 ---
export function AdminPage(){
  ensureHomeStyles();
  const root = document.createElement('div');
  root.innerHTML = '<div class="admin-shell"><p class="muted">載入中...</p></div>';

  getRedirectResult(auth).catch(()=>{});

  onAuthStateChanged(auth, async (user)=>{
    if(!user){ showLogin(root); return; }
    if(!isAdmin(user)){
      alert('非管理員帳號，無法進入後台。');
      try{ await signOut(auth); }catch{}
      showLogin(root);
      return;
    }
    renderHome(root);
  });

  return root;
}
