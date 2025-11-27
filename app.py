import os
import json
import datetime
from flask import Flask, request, abort
from dotenv import load_dotenv

# LINE 相關套件
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage

# Google 相關套件
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build

# 載入 .env 的設定，確保金鑰安全
load_dotenv()

app = Flask(__name__)

# 設定 API Key
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))

# Google Calendar 驗證 (使用 Service Account)
SCOPES = ['https://www.googleapis.com/auth/calendar']
SERVICE_ACCOUNT_FILE = 'service_account.json'
CALENDAR_ID = os.getenv('CALENDAR_ID')

def get_calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build('calendar', 'v3', credentials=creds)
    return service

# 設定 Gemini 的 Prompt，讓它變成一個「參數提取器」
def ask_gemini_to_extract_event(text):
    # 取得當前時間，讓 AI 知道「明天」是幾號
    now = datetime.datetime.now().isoformat()
    
    prompt = f"""
    你是一個日曆助理。現在時間是 {now}。
    使用者的輸入是："{text}"
    
    如果使用者想要建立行程，請回傳一個純 JSON 格式，包含以下欄位：
    - "action": "create_event"
    - "summary": 活動標題
    - "start_time": ISO 8601 格式的開始時間 (例如 2023-11-20T10:00:00)
    - "end_time": ISO 8601 格式的結束時間 (通常比開始時間晚1小時)
    
    如果這只是一般聊天，請回傳 JSON:
    - "action": "chat"
    - "reply": 你的回覆內容
    
    請只回傳 JSON，不要有其他 markdown 標記。
    """
    
    model = genai.GenerativeModel('gemini-pro')
    response = model.generate_content(prompt)
    
    # 簡單清理 Gemini 有時會多加的 ```json 標記
    clean_text = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)

def add_event_to_calendar(event_data):
    try:
        service = get_calendar_service()
        event = {
            'summary': event_data.get('summary', '未命名活動'),
            'start': {
                'dateTime': event_data['start_time'],
                'timeZone': 'Asia/Taipei',
            },
            'end': {
                'dateTime': event_data['end_time'],
                'timeZone': 'Asia/Taipei',
            },
        }
        event = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        return f"已幫您預約：{event['summary']}，時間是 {event_data['start_time']}"
    except Exception as e:
        return f"預約失敗，發生錯誤：{str(e)}"

# LINE Webhook 入口
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# 處理訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text
    
    try:
        # 1. 先問 Gemini 這是聊天還是要排程
        ai_response = ask_gemini_to_extract_event(user_msg)
        
        reply_text = ""
        
        # 2. 判斷動作
        if ai_response.get("action") == "create_event":
            # 3. 執行 Google Calendar API
            reply_text = add_event_to_calendar(ai_response)
        else:
            # 純聊天
            reply_text = ai_response.get("reply", "我不太確定您的意思。")
            
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
        
    except Exception as e:
        # 錯誤處理
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"處理時發生錯誤：{str(e)}")
        )

if __name__ == "__main__":
    app.run(port=5000)
