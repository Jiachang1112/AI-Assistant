import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai

app = Flask(__name__)

# --- 設定區 (從 Render 環境變數讀取) ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# --- 初始化 LINE 與 Gemini ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)

# 🔥 關鍵修改：直接指定最穩定的 Flash 模型
# 這是目前免費額度最高 (15 RPM)、速度最快的模型，最適合做 LINE 機器人
model = genai.GenerativeModel('gemini-1.5-flash')

# ✅ UptimeRobot 的應門口 (新增這個！)
# 當 UptimeRobot 每 5 分鐘來敲首頁時，回傳 "alive" 讓它知道機器人活著
# 這樣 Render 就不會進入休眠模式，LINE 訊息就能「秒回」
@app.route("/")
def home():
    return "Hello! I am alive!", 200

@app.route("/callback", methods=['POST'])
def callback():
    # 取得 LINE 傳來的簽章 (安全性檢查)
    signature = request.headers['X-Line-Signature']
    # 取得訊息內容
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# 當收到文字訊息時
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text
    try:
        # 1. 丟給 Gemini 思考
        response = model.generate_content(user_msg)
        reply_text = response.text
        
        # 2. 回傳給使用者
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        # 錯誤處理 (印出 Log 供除錯)
        print(f"Error: {e}")
        # 如果真的遇到 429 或其他錯誤，回傳友善訊息
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="我現在有點忙 (或是發生連線錯誤)，請再試一次...")
        )

if __name__ == "__main__":
    app.run()
