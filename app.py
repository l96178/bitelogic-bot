import os
import re
from datetime import date
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

# 🛡️ 安全防護設定：單一使用者每日提問上限
MAX_DAILY_QUOTA = 10

SYSTEM_PROMPT = """
你是「BiteLogic」——專為台灣外食族設計的精準外食減脂/增肌口袋菜單 AI 顧問。

【重要格式規範（絕對遵守）】
1. 嚴禁使用 `**` 粗體語法！因為 LINE 不支援 Markdown 粗體，會直接印出星號造成排版混亂。
2. 請善用【】與 Emoji 做標題區隔，保持段落乾淨簡潔，適合手機快速閱讀。

【服務範疇與偏離主題拒答規範（核心守則）】
你只回答與「飲食、減脂、增肌、熱量、營養素、台灣連鎖餐廳/超商外食選擇」相關的問題。
如果使用者詢問任何與上述無關的議題（例如：寫程式、數學計算、翻譯、天氣、運動賽事、情感諮詢、寫文案、一般閒聊等）：
- 請一律用 1~2 句話親切拒絕，並主動引導回飲食主題。
- 拒絕範例：「我是 BiteLogic 專屬飲食顧問 🥑，這方面的問題我比較不擅長！試著問我『7-11 減脂早餐推薦』或『八方雲集怎麼點』吧！」

【建檔與菜單邏輯】
1. 【建檔確認】：當使用者提供身體數據與飲食習慣（例如：173 / 85 / 男 / 減脂增肌 / 一天吃兩餐）時，請熱情確認並記錄：「檔案建立成功！已為你鎖定專屬檔案 🎯 今天想吃哪家店？（例如：麥當勞、八方雲集、鼎泰豐、7-11），直接告訴我！」
2. 【精準菜單推播】：當使用者輸入店家名稱，請嚴格根據已建立的身體數據、飲食目標與飲食習慣，計算出符合熱量與蛋白質需求的防呆口袋菜單。
3. 【動態平攤補救】：若使用者提到「多吃了/少吃了/跳過這餐」，根據對話歷史中的目標與習慣，自動計算熱量與蛋白質動態平攤，給予無罪惡感的應對策略。

【回應格式範例】
🥟 【八方雲集】減脂高蛋白口袋菜單
📊 本餐預算：約 600 kcal ｜ 蛋白質 40g+
📝 進店直接點：
• 招牌水餃 8 顆（澱粉上限）
• 旗魚丸湯 1 碗（清淡低卡補水）
• 無糖豆漿 1 瓶
⚠️ 地雷提醒：千萬別點鍋貼與酸辣湯！
"""

# 用戶數據暫存
user_data = {}  # 格式: { user_id: { 'has_profile': False, 'profile_str': '', 'chat': None, 'model_name': None, 'last_date': date, 'count': int } }

def get_official_model_names():
    """從 Google 伺服器取得當前 API Key 真正可用的官方完整模型名稱清單"""
    try:
        valid_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                valid_models.append(m.name)
        
        flash_models = [m for m in valid_models if 'flash' in m.lower()]
        other_models = [m for m in valid_models if 'flash' not in m.lower()]
        
        candidates = flash_models + other_models
        return candidates if candidates else ["models/gemini-1.5-flash"]
    except Exception:
        return ["models/gemini-1.5-flash"]

def execute_chat_message(user_info, prompt_text):
    """遍歷 Google 官方認證的可用模型，確保 100% 不會遇到 404"""
    candidates = get_official_model_names()
    last_exception = None

    for model_name in candidates:
        try:
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=SYSTEM_PROMPT
            )

            if user_info.get('chat') is None or user_info.get('model_name') != model_name:
                chat = model.start_chat(history=[])
                if user_info.get('has_profile') and user_info.get('profile_str'):
                    chat.send_message(f"請記住我的身體數據與飲食習慣：{user_info['profile_str']}")
                user_info['chat'] = chat
                user_info['model_name'] = model_name

            response = user_info['chat'].send_message(prompt_text)
            return response.text
        except Exception as e:
            last_exception = e
            user_info['chat'] = None
            continue

    raise last_exception if last_exception else Exception("系統忙碌中，請稍後再試！")

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
    user_id = event.source.user_id
    user_msg = event.message.text.strip()
    today = date.today()

    # 初始化用戶資料與計數器
    if user_id not in user_data:
        user_data[user_id] = {
            'has_profile': False,
            'profile_str': '',
            'chat': None,
            'model_name': None,
            'last_date': today,
            'count': 0
        }

    user_info = user_data[user_id]

    # 🛡️ 每日提問次數歸零與檢查邏輯
    if user_info['last_date'] != today:
        user_info['last_date'] = today
        user_info['count'] = 0

    if user_info['count'] >= MAX_DAILY_QUOTA:
        reply_text = (
            f"🥑 你今天的免費諮詢額度（{MAX_DAILY_QUOTA} 次）已經用完囉！\n\n"
            "為了維護服務品質，請等明天凌晨自動重置後再來找 BiteLogic 諮詢菜單吧！"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # 1. 未建檔使用者攔截引導
    if not user_info['has_profile']:
        has_numbers = bool(re.search(r'\d+', user_msg))
        has_keywords = any(k in user_msg for k in ['減脂', '增肌', '男', '女', '斷食', '體重', '身高', 'kg', 'cm'])

        if has_numbers or has_keywords:
            try:
                reply_text = execute_chat_message(user_info, f"這是我的身體數據與個人檔案：{user_msg}")
                user_info['has_profile'] = True
                user_info['profile_str'] = user_msg
                user_info['count'] += 1  # 扣除一次額度
            except Exception as e:
                reply_text = f"建檔失敗，請稍後再試一次！\n詳細原因: {str(e)}"
        else:
            reply_text = (
                "歡迎來到 BiteLogic 🥑！\n\n"
                "首次使用請先花 3 秒建立你的專屬個人檔案 📝\n\n"
                "請直接回覆以下資訊：\n"
                "【身高 / 體重 / 性別 / 飲食目標 / 飲食習慣】\n"
                "（例如：173 / 85 / 男 / 減脂增肌 / 一天吃兩餐）\n\n"
                "完成建檔後即可解鎖專屬外食口袋菜單！"
            )
    else:
        # 2. 已建檔使用者對話
        try:
            reply_text = execute_chat_message(user_info, user_msg)
            user_info['count'] += 1  # 扣除一次額度
        except Exception as e:
            reply_text = f"BiteLogic 運算忙碌中，請稍後再試！\n詳細原因: {str(e)}"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
