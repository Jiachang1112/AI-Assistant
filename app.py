import os
import logging
import traceback
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai

# 啟用基本 log
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# --- 設定區 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# 啟動時先檢查環境變數有沒有抓到
if not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境變數 LINE_CHANNEL_ACCESS_TOKEN 沒有設定")
if not LINE_CHANNEL_SECRET:
    raise RuntimeError("環境變數 LINE_CHANNEL_SECRET 沒有設定")
if not GEMINI_API_KEY:
    raise RuntimeError("環境變數 GEMINI_API_KEY 沒有設定")

# --- 初始化 ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)

# ✅ 強制使用 gemini-1.5-flash（免費額度高、穩定）
model = genai.GenerativeModel("gemini-1.5-flash")  # 或 "gemini-1.5-flash-latest"

# ✅ UptimeRobot 用的健康檢查
@app.route("/")
def home():
    return "Hello! I am alive!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    logging.info(f"收到 LINE webhook：{body}")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logging.exception("Invalid signature")
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text
    logging.info(f"使用者說：{user_msg}")

    try:
        # 呼叫 AI
        response = model.generate_content(user_msg)
        reply_text = response.text
        logging.info(f"Gemini 回覆：{reply_text}")

        # 回覆訊息
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        # 這裡把完整錯誤印出來，去 Render Logs 看
        logging.error("呼叫 Gemini 發生錯誤：", exc_info=True)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="系統重啟中，請稍後再試。")
        )

if __name__ == "__main__":
    app.run()

@app.route("/debug_models")
def debug_models():
    try:
        models = list(genai.list_models())
        result = []
        for m in models:
            result.append(f"{m.name} | supported: {m.supported_generation_methods}")
        return "<br>".join(result), 200
    except Exception as e:
        return f"Error: {e}", 500
