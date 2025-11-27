import os
import io
import logging
from datetime import datetime, timezone

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    ImageMessage,
    TextSendMessage,
    FlexSendMessage,
    BubbleContainer,
    BoxComponent,
    TextComponent,
    ButtonComponent,
    URIAction
)

import google.generativeai as genai
from google.cloud import firestore
import requests

# ----------------- 基本設定 -----------------
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET or not GEMINI_API_KEY:
    raise RuntimeError("環境變數沒設好，請在 Render Environment 設定三個值")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Gemini：文字用 2.0-flash，多模態也能用 1.5-flash
genai.configure(api_key=GEMINI_API_KEY)
text_model = genai.GenerativeModel("gemini-2.0-flash")
vision_model = genai.GenerativeModel("gemini-1.5-flash")

# Firestore（需要在 Render 設定 GOOGLE_APPLICATION_CREDENTIALS 或 Workload Identity）
db = firestore.Client()
users_ref = db.collection("users")
logs_ref = db.collection("chat_logs")

# 你的自家 API（SuperTool）URL（如果沒有先留空字串）
SUPERTOOL_API_URL = os.environ.get("SUPERTOOL_API_URL", "")  # TODO: 在 Environment 設定


# ----------------- 小工具函式 -----------------
def should_wake_up(text: str) -> bool:
    """⑤ 限定關鍵字啟動：只有包含 'ai' 或 '助手' 才啟動 AI，否則不理或簡單回覆。"""
    t = text.strip().lower()
    return t.startswith("ai ") or "助手" in t or t.startswith("ai：") or t.startswith("ai:")


def detect_mode(text: str) -> str:
    """
    ② 判斷使用模式：
      #總結 開頭 → 長文總結
      #翻譯 → 翻譯模式
      #寫作 → 寫作 / 改寫模式
      #工具 → 呼叫自家 API
      其他 → 一般聊天 + 記憶
    """
    t = text.strip()
    if t.startswith("#總結"):
        return "summary"
    if t.startswith("#翻譯"):
        return "translate"
    if t.startswith("#寫作"):
        return "writing"
    if t.startswith("#工具"):
        return "tool"
    return "chat"


def strip_mode_prefix(text: str, mode: str) -> str:
    if mode == "summary":
        return text.replace("#總結", "", 1).strip()
    if mode == "translate":
        return text.replace("#翻譯", "", 1).strip()
    if mode == "writing":
        return text.replace("#寫作", "", 1).strip()
    if mode == "tool":
        return text.replace("#工具", "", 1).strip()
    return text


def get_user_doc(user_id: str):
    return users_ref.document(user_id)


def load_user_memory(user_id: str, limit: int = 10):
    """① 從 Firestore 讀最近幾句對話記錄，當作對話記憶。"""
    snaps = (
        logs_ref.where("user_id", "==", user_id)
        .order_by("ts", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    history = []
    for s in reversed(list(snaps)):
        d = s.to_dict()
        history.append(f"{d['role']}：{d['text']}")
    return "\n".join(history)


def save_message(user_id: str, role: str, text: str):
    """① 把對話存進 Firestore。"""
    logs_ref.add(
        {
            "user_id": user_id,
            "role": role,
            "text": text,
            "ts": datetime.now(timezone.utc),
        }
    )


def build_flex_bubble(title: str, body_text: str) -> FlexSendMessage:
    """
    ⑥ 漂亮氣泡框（Flex Message）
    ⑦ 可回傳卡片／按鈕
    """
    bubble = BubbleContainer(
        body=BoxComponent(
            layout="vertical",
            contents=[
                TextComponent(text=title, weight="bold", size="xl", wrap=True),
                TextComponent(text=body_text, size="sm", wrap=True, margin="md"),
            ],
        ),
        footer=BoxComponent(
            layout="vertical",
            contents=[
                ButtonComponent(
                    style="primary",
                    height="sm",
                    action=URIAction(
                        label="Google",
                        uri="https://www.google.com"  # 你可以改成自己的網站 / SuperTool
                    ),
                )
            ],
        ),
    )
    return FlexSendMessage(alt_text=title, contents=bubble)


def call_supertool_api(payload_text: str) -> str:
    """
    ⑧ 呼叫你自己的 API（例：SuperTool）
    假設是 POST JSON，回傳一段文字。
    """
    if not SUPERTOOL_API_URL:
        return "尚未設定 SuperTool API URL。"

    try:
        resp = requests.post(
            SUPERTOOL_API_URL,
            json={"query": payload_text},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # TODO: 依照你 API 的格式調整
        return data.get("result", str(data))
    except Exception as e:
        logging.exception("呼叫 SuperTool API 失敗")
        return f"呼叫自家 API 失敗：{e}"


# ----------------- Gemini 呼叫封裝 -----------------
def gemini_chat(user_id: str, user_text: str) -> str:
    """① + ② 一般聊天，會帶入使用者記憶。"""
    history = load_user_memory(user_id, limit=10)

    system_prompt = (
        "你是一個友善、口語化的中文 AI 助手，回答要簡潔、實用、自然。\n"
        "你會記得使用者過去說過的事情，適度在對話中提到，但不要硬要裝熟。\n"
    )

    prompt = (
        f"{system_prompt}\n\n"
        f"【以下是你和使用者最近的對話紀錄】\n{history}\n\n"
        f"【使用者最新訊息】\n{user_text}\n\n"
        "請直接以中文回答，不要顯示系統提示。"
    )

    resp = text_model.generate_content(prompt)
    return resp.text.strip()


def gemini_summary(text: str) -> str:
    prompt = (
        "請用條列式幫我總結下面的內容，重點明確、使用繁體中文：\n\n"
        f"{text}"
    )
    return text_model.generate_content(prompt).text.strip()


def gemini_translate(text: str) -> str:
    prompt = (
        "請偵測下面文字語言，並翻譯成流暢自然的繁體中文或英文（依照情境選擇最適合的目標語言），"
        "僅輸出翻譯結果：\n\n"
        f"{text}"
    )
    return text_model.generate_content(prompt).text.strip()


def gemini_writing(text: str) -> str:
    prompt = (
        "請根據下面的需求，幫我寫一段優化後的內容，可以適度補充細節、讓語氣更自然：\n\n"
        f"{text}"
    )
    return text_model.generate_content(prompt).text.strip()


def gemini_vision_answer(image_bytes: bytes, user_hint: str) -> str:
    """
    ③ 圖片辨識，多模態 Gemini。
    使用 1.5-flash，輸入：[image, 文字提示]
    """
    img_part = {
        "mime_type": "image/jpeg",
        "data": image_bytes,
    }
    prompt = user_hint or "請幫我描述這張圖片的內容，使用繁體中文。"
    resp = vision_model.generate_content([img_part, prompt])
    return resp.text.strip()


# ----------------- Flask 路由 -----------------
@app.route("/")
def home():
    return "OK - AI Assistant with memory & tools", 200


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


# ----------------- LINE 事件處理 -----------------
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event: MessageEvent):
    user_id = event.source.user_id
    user_text = event.message.text

    logging.info(f"[Text] {user_id}: {user_text}")

    # ⑤ 關鍵字啟動：沒有關鍵字就只記錄，不啟動 AI（你也可以改成回覆簡短提示）
    if not should_wake_up(user_text):
        # 也可以選擇什麼都不回
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="（如需叫我，請輸入：ai + 問題，或包含『助手』關鍵字）")
        )
        return

    mode = detect_mode(user_text)
    pure_text = strip_mode_prefix(user_text, mode)

    # ① 記憶：先存使用者訊息
    save_message(user_id, "User", user_text)

    try:
        if mode == "summary":
            result = gemini_summary(pure_text)
            title = "📝 長文總結"
        elif mode == "translate":
            result = gemini_translate(pure_text)
            title = "🌐 翻譯結果"
        elif mode == "writing":
            result = gemini_writing(pure_text)
            title = "✍️ 寫作 / 改寫"
        elif mode == "tool":
            result = call_supertool_api(pure_text)
            title = "🛠 SuperTool 結果"
        else:
            result = gemini_chat(user_id, pure_text)
            title = "🤖 AI 助手回覆"

        # ① 把 AI 回覆也存起來
        save_message(user_id, "Assistant", result)

        # ⑥ ⑦ 用 Flex 氣泡回覆（也可改成 TextSendMessage）
        flex = build_flex_bubble(title, result)
        line_bot_api.reply_message(event.reply_token, flex)

    except Exception as e:
        logging.exception("處理文字訊息時發生錯誤")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="系統有點忙，請稍後再試。")
        )


@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event: MessageEvent):
    """
    ③ 圖片辨識：使用 Gemini multimodal。
    如果使用者上一句是 ai 開頭的文字，你也可以額外從 Firestore 讀來當 hint，這裡先簡化。
    """
    user_id = event.source.user_id
    message_id = event.message.id

    logging.info(f"[Image] {user_id} sent image {message_id}")

    try:
        content = line_bot_api.get_message_content(message_id)
        image_bytes = io.BytesIO()
        for chunk in content.iter_content():
            image_bytes.write(chunk)
        img_data = image_bytes.getvalue()

        answer = gemini_vision_answer(img_data, user_hint="請幫我看這張圖說明重點。")
        save_message(user_id, "Assistant", f"(image answer) {answer}")

        flex = build_flex_bubble("🖼 圖片解析", answer)
        line_bot_api.reply_message(event.reply_token, flex)

    except Exception as e:
        logging.exception("處理圖片訊息時發生錯誤")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="圖片解析失敗，請稍後再試或換一張圖片。")
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
