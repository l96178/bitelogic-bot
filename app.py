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
你是「BiteLogic」——專為台灣外食族設計的精準外食減脂/增肌口袋菜單 AI 顧問。

【重要格式規範（絕對遵守）】
1. 嚴禁使用 `**` 粗體語法！因為 LINE 不支援 Markdown 粗體，會直接印出星號造成排版混亂。
2. 請善用【】與 Emoji 做標題區隔，保持段落乾淨、乾淨簡潔，適合手機快速閱讀。

【互動機制與對話流程】
1. 【模糊打招呼】若使用者只傳「hello」、「嗨」、「吃什麼」、「你好」等模糊訊息：
   請用 2 句內極簡回覆，例如：
   「哈囉！我是 BiteLogic 🥑 
   請直接告訴我你現在想吃哪家店（例如：麥當勞、八方雲集、7-11、鼎泰豐），我直接幫你配專屬口袋菜單！」

2. 【直接給店家】若使用者只輸入店家名稱（例如「八方雲集」、「麥當勞」）：
   【絕對不要問問題】！直接預設出一份適合該店家的「標準減脂高蛋白組合」（約 500-600 kcal），並在最後附註：「如果有特定熱量目標或要改增肌，隨時告訴我！」

3. 【回應格式範例】
🥟 【八方雲集】減脂高蛋白口袋菜單
📊 預算：約 550 kcal ｜ 蛋白質 35g+
📝 進店直接點：
• 招牌水餃 8 顆（澱粉上限）
• 蕈菇豆腐湯 1 碗（補充蛋白質與飽足感）
• 無糖豆漿 1 瓶
⚠️ 地雷提醒：千萬別點鍋貼與酸辣湯！

4. 【情境補救】若使用者提到「多吃了/少吃了/168/跳過這餐」，自動進行熱量平攤，給予無罪惡感的應對策略。
"""

# 全域模型快取，避免重複初始化
cached_model = None

def get_working_model():
    global cached_model
    if cached_model is not None:
        return cached_model

    try:
        available_models = [
            m.name.replace("models/", "") for m in genai.list_models()
            if 'generateContent' in m.supported_generation_methods
        ]
    except Exception:
        available_models = []

    priority_list = [
        "gemini-3.5-flash", "gemini-3.5-flash-lite", 
        "gemini-2.0-flash", "gemini-1.5-flash"
    ]
    
    candidates = []
    for p in priority_list:
        if p in available_models:
            candidates.append(p)
            
    for a in available_models:
        if a not in candidates:
            candidates.append(a)

    if not candidates:
        candidates = ["gemini-1.5-flash"]

    for m_name in candidates:
        try:
            m = genai.GenerativeModel(
                model_name=m_name,
                system_instruction=SYSTEM_PROMPT
            )
            cached_model = m
            return cached_model
        except Exception:
            continue

    cached_model = genai.GenerativeModel(
        model_name=candidates[0],
        system_instruction=SYSTEM_PROMPT
    )
    return cached_model

@app.route("/", methods=['GET'])
def health_check():
    return 'BiteLogic is alive!', 200

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

    try:
        model = get_working_model()
        response = model.generate_content(user_msg)
        reply_text = response.text
    except Exception as e:
        reply_text = f"BiteLogic 運算忙碌中，請稍後再試一次！"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
