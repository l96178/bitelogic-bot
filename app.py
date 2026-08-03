import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai

app = Flask(__name__)

# 從環境變數讀取金鑰
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 設定 Gemini API
genai.configure(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """
你是一位專為台灣外食族設計的「精準外食減脂/增肌導航 AI 顧問」（BiteLogic）。
你的核心任務是根據使用者的狀態與指定的台灣在地外食店家，給出「進店不看菜單、直接點」的防呆口袋菜單。

[行為準則]
1. 彈性適應：使用者可自訂每日餐數（1餐/2餐168/3餐/多餐）。若使用者提到「多吃了」或「少吃了/跳過這餐」，請自動進行熱量與蛋白質的動態平攤或調整，不產生罪惡感。
2. 防呆提醒：餐點必須包含客製化指令（去美乃滋、飯少、清醬油沾著吃、不加煎炸類）與地雷警語。
3. 台灣在地知識：精通四大超商、八方雲集、麥當勞、Sukiya、麥味登、鼎泰豐及路邊小吃店的熱量與營養素。

[回應格式範例]
🥟 【鼎泰豐】減脂高蛋白口袋菜單
📊 本餐預算：約 700 kcal ｜ 蛋白質 40g+
📝 進店直接點：
1. 元氣雞湯 1 碗 🥣（先喝湯吃肉打底）
2. 紹興醉雞 1 份 🍗
3. 小籠包 限 4 顆 🥟（澱粉上限）
⚠️ 地雷提醒：千萬別點排骨炒飯與紅油抄手！
"""

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
    user_msg = event.message.text.strip()

    # 最新新版 API Key 正式支援的 Gemini 3 世代模型清單
    candidate_models = [
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite"
    ]

    # 自動嘗試向後相容
    try:
        fetched = [
            m.name.replace("models/", "") for m in genai.list_models()
            if 'generateContent' in m.supported_generation_methods
        ]
        # 將最新的 3 世代模型排在最前面
        fetched.sort(key=lambda name: ("3" in name), reverse=True)
        for m_name in fetched:
            if m_name not in candidate_models:
                candidate_models.append(m_name)
    except Exception:
        pass

    reply_text = None
    last_err = ""

    # 輪詢嘗試模型，直到成功取得回覆
    for model_name in candidate_models:
        try:
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=SYSTEM_PROMPT
            )
            response = model.generate_content(user_msg)
            reply_text = response.text
            if reply_text:
                break
        except Exception as e:
            last_err = f"[{model_name}] {str(e)}"
            continue

    if not reply_text:
        reply_text = f"BiteLogic 運算失敗: {last_err}"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
