# accounting.py
# 這裡專門放：記帳邏輯、Firebase 寫入、記帳卡片樣式、網頁報表資料

import logging
import datetime
from firebase_admin import firestore
from linebot.models import BubbleContainer, BoxComponent, TextComponent

# --- 記帳核心邏輯 ---

def get_default_ledger_id(db, user_id):
    """取得或建立預設帳本 ID"""
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

def record_entry(db, user_id, args):
    """執行記帳寫入資料庫"""
    try:
        ledger_id = get_default_ledger_id(db, user_id)
        if not ledger_id: return "錯誤：無法取得帳本。"
        
        now_iso = datetime.datetime.now().strftime("%Y-%m-%d")
        # 處理金額與參數
        amount = float(args.get('amount', 0))
        item = args.get('item', '未命名')
        note = args.get('note', '')
        full_note = f"{item} {note}".strip()
        
        entry_data = {
            'type': args.get('type', 'expense'), 
            'amount': amount, 
            'categoryId': args.get('category', '其他'), 
            'note': full_note, 
            'date': now_iso, 
            'createdAt': firestore.SERVER_TIMESTAMP, 
            'updatedAt': firestore.SERVER_TIMESTAMP, 
            'source': 'line-bot'
        }
        
        db.collection('users').document(user_id).collection('ledgers').document(ledger_id).collection('entries').add(entry_data)
        
        # 回傳給 app.py 用來顯示的資料
        return {
            'status': 'success', 
            'action': 'accounting', 
            'data': {
                'item': item, 
                'amount': amount, 
                'category': entry_data['categoryId'], 
                'type': entry_data['type'], 
                'date': entry_data['date']
            }
        }
    except Exception as e:
        logging.error(f"記帳失敗: {e}")
        return f"記帳錯誤: {e}"

def get_journal_data(db, user_id):
    """取得網頁報表所需的資料"""
    try:
        ledger_id = get_default_ledger_id(db, user_id)
        if not ledger_id:
            return [], 0, 0

        docs = db.collection('users').document(user_id).collection('ledgers').document(ledger_id).collection('entries').order_by('createdAt', direction=firestore.Query.DESCENDING).limit(50).stream()
        
        entries = []
        total_income = 0
        total_expense = 0

        for doc in docs:
            d = doc.to_dict()
            amt = float(d.get('amount', 0))
            if d.get('type') == 'income':
                total_income += amt
            else:
                total_expense += amt
            entries.append(d)
            
        return entries, int(total_income), int(total_expense)
    except Exception as e:
        logging.error(f"讀取報表失敗: {e}")
        return [], 0, 0

# --- UI 樣式邏輯 ---

def create_accounting_bubble(data):
    """產生記帳成功的 Flex Message"""
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
                TextComponent(text=icon, size='3xl', flex=0, align='center', gravity='center'),
                TextComponent(text=title_text, weight='bold', color='#ffffff', size='lg', align='start', gravity='center', margin='md', flex=1)
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

# --- 網頁 HTML ---
JOURNAL_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>收支日記本</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #f8f9fa; font-family: "Noto Sans TC", sans-serif; }
        .card { border-radius: 15px; border: none; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin-bottom: 20px; }
        .total-income { color: #10b981; }
        .total-expense { color: #ef4444; }
        .transaction-item { border-left: 5px solid #ccc; background: white; padding: 15px; margin-bottom: 10px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        .type-income { border-left-color: #10b981; }
        .type-expense { border-left-color: #ef4444; }
        .amount-income { color: #10b981; font-weight: bold; }
        .amount-expense { color: #ef4444; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container py-4">
        <h2 class="text-center mb-4">📖 我的收支日記</h2>
        
        <div class="row text-center mb-4">
            <div class="col-4">
                <div class="card p-3">
                    <small class="text-muted">總收入</small>
                    <h4 class="total-income">+{{ total_income }}</h4>
                </div>
            </div>
            <div class="col-4">
                <div class="card p-3">
                    <small class="text-muted">總支出</small>
                    <h4 class="total-expense">-{{ total_expense }}</h4>
                </div>
            </div>
            <div class="col-4">
                <div class="card p-3">
                    <small class="text-muted">結餘</small>
                    <h4 class="{{ 'text-success' if (total_income - total_expense) >= 0 else 'text-danger' }}">
                        {{ total_income - total_expense }}
                    </h4>
                </div>
            </div>
        </div>

        <h5 class="mb-3">最近交易紀錄</h5>
        <div>
            {% if entries %}
                {% for entry in entries %}
                <div class="transaction-item {{ 'type-income' if entry.type == 'income' else 'type-expense' }} d-flex justify-content-between align-items-center">
                    <div>
                        <div class="fw-bold">{{ entry.note }}</div>
                        <small class="text-muted">{{ entry.date }} | <span class="badge bg-light text-dark border">{{ entry.categoryId }}</span></small>
                    </div>
                    <div class="{{ 'amount-income' if entry.type == 'income' else 'amount-expense' }}">
                        {{ '+' if entry.type == 'income' else '-' }}{{ entry.amount }}
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="text-center text-muted py-5">
                    <p>目前還沒有記帳紀錄喔！<br>試試在 LINE 輸入「午餐100」</p>
                </div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""
