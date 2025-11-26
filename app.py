import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai

app = Flask(__name__)

# --- 偵錯區：印出版本 ---
# 這行會在 Log 裡告訴我們真相
print(f"-------------- 目前 GenAI 套件版本: {genai.__version__} --------------")

# --- 設定區 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# --- 初始化設定 ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)

# --- 暫時換成 gemini-pro 測試 ---
# 如果 gemini-pro 會動，代表是 flash 模型的問題
# 如果連 gemini-pro 都不動，代表是 API Key 或環境的問題
model = genai.GenerativeModel('gemini-pro') 

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text
    try:
        response = model.generate_content(user_msg)
        reply_text = response.text
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        # 印出詳細錯誤到 Log
        print(f"Error Detail: {e}")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="抱歉，我的大腦打結了...")
        )

if __name__ == "__main__":
    app.run()
