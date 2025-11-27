import os
import logging

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

import google.generativeai as genai

# 啟用 log，方便在 Render Logs 看到錯誤
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET or not GEMINI_API_KEY:
    raise RuntimeError("環境變數沒有設好，請在 Render Environment 確認三個值都有設定")

# --- 初始化 LINE / Gemini ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

genai.configure(api_key=GEMINI_API_KEY)

# --- 設定系統指令 ---
# 指令重點：
# 1. 根據使用者的語言回應 (Detect language)。
# 2. 強制規則：如果是中文，必須使用繁體中文 (Traditional Chinese)。
sys_instruction = """
你是一個有用的 AI 助手。
請根據使用者輸入的語言來決定回應的語言（例如使用者用英文，你就回英文）。
但在使用中文時，請務必遵守以下最高指導原則：
「所有中文回應都必須使用繁體中文 (Traditional Chinese)，絕對禁止使用簡體中文。」
"""

model = genai.GenerativeModel(
    "gemini-2.0-flash",
    system_instruction=sys_instruction
)
# 健康檢查（給 UptimeRobot 或瀏覽器測試）
@app.route("/")
def home():
    return "OK - AI Assistant is running", 200

# LINE Webhook 入口
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    logging.info(f"收到 LINE webhook：{body}")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logging.exception("Invalid signature")
        abort(400)

    return "OK"

# 處理文字訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text
    logging.info(f"使用者說：{user_msg}")

    try:
        # 呼叫 Gemini
        response = model.generate_content(user_msg)
        reply_text = response.text
        logging.info(f"Gemini 回覆：{reply_text}")
    except Exception as e:
        logging.exception("呼叫 Gemini 發生錯誤")
        reply_text = "系統有點忙碌，請稍後再試。"

    # 回覆 LINE 使用者
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
