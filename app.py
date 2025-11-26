import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai

app = Flask(__name__)

# --- 設定區 (請換成你的 Key) ---
# 建議之後改用環境變數 (Environment Variables) 比較安全，現在先直接填入測試
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# --- 初始化設定 ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)

# 建立 Gemini 模型
model = genai.GenerativeModel('gemini-1.5-flash')

@app.route("/callback", methods=['POST'])
def callback():
    # 取得 LINE 的簽章
    signature = request.headers['X-Line-Signature']
    # 取得請求內容
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # 驗證簽章並處理事件
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# 當收到「文字訊息」時的處理邏輯
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text
    
    try:
        # 1. 呼叫 Gemini
        response = model.generate_content(user_msg)
        reply_text = response.text
        
        # 2. 回覆 LINE
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        # 錯誤處理
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="抱歉，我的大腦打結了...")
        )
        print(f"Error: {e}")

if __name__ == "__main__":
    app.run()
