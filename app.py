import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai

app = Flask(__name__)

# --- 設定區 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# --- 初始化設定 ---
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)

# --- 🔍 關鍵診斷：列出所有可用模型 ---
print(f"============== 開始檢查模型清單 ==============")
try:
    available_models = []
    for m in genai.list_models():
        print(f"發現模型: {m.name}")
        if 'generateContent' in m.supported_generation_methods:
            available_models.append(m.name)
            print(f"   -> ✅ 支援對話生成")
    
    print(f"總結可用模型: {available_models}")

    # 自動選擇一個可用的模型 (優先選 flash, 沒有就選 pro, 再沒有就選第一個能用的)
    if 'models/gemini-1.5-flash' in available_models:
        target_model = 'gemini-1.5-flash'
    elif 'models/gemini-pro' in available_models:
        target_model = 'gemini-pro'
    elif available_models:
        target_model = available_models[0].replace('models/', '') # 移除前綴嘗試
    else:
        target_model = 'gemini-1.5-flash' # 預設賭一把
        print("❌ 警告：沒有找到任何支援對話的模型，強制設定為 flash")

    print(f"============== 最終決定使用模型: {target_model} ==============")
    model = genai.GenerativeModel(target_model)

except Exception as e:
    print(f"❌ API Key 或連線發生嚴重錯誤: {e}")
    # 為了讓程式不崩潰，還是建立一個預設的
    model = genai.GenerativeModel('gemini-1.5-flash')

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
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
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=response.text))
    except Exception as e:
        error_msg = str(e)
        print(f"對話失敗: {error_msg}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"發生錯誤，請查看 Log: {error_msg[:30]}..."))

if __name__ == "__main__":
    app.run()
