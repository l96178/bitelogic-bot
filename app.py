import os
import re
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
2. 請善用【】與 Emoji 做標題區隔，保持段落乾淨簡潔，適合手機快速閱讀。

【建檔與菜單邏輯】
1. 【建檔確認】：當使用者提供身體數據與飲食習慣（例如：173 / 85 / 男 / 減脂增肌 / 一天吃兩餐）時，請熱情確認並記錄：「檔案建立成功！已為你鎖定專屬檔案 🎯 今天想吃哪家店？（例如：麥當勞、八方雲集、鼎泰豐、7-11），直接告訴我！」
2. 【精準菜單推播】：當使用者輸入店家名稱，請嚴格根據已建立的身體數據、飲食目標與飲食習慣，計算出符合熱量與蛋白質需求的防呆口袋菜單。
3. 【動態平攤補救】：若使用者提到「多吃了/少吃了/跳過這餐」，根據對話歷史中的目標與習慣，自動計算熱量與蛋白質動態平攤，給予無罪惡感的應對策略。

【回應格式範例】
🥟 【八方雲集】減脂高蛋白口袋菜單
📊 本餐預算：約 600 kcal ｜ 蛋白質 40g+
📝 進店直接點：
• 招牌水餃 8 顆（澱粉上限）
• 蕈菇豆腐湯 1 碗（補充蛋白質與飽足感）
• 無糖豆漿 1 瓶
⚠️ 地雷提醒：千萬別點鍋貼與酸辣湯！
"""

user_data = {}  # 格式: { user_id: { 'has_profile': False, 'profile_str': '', 'chat': session } }

def create_fresh_model():
    return genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=SYSTEM_PROMPT
    )

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

    # 初始化用戶狀態
    if user_id not in user_data:
        model = create_fresh_model()
        user_data[user_id] = {
            'has_profile': False,
            'profile_str': '',
            'chat': model.start_chat(history=[])
        }

    user_info = user_data[user_id]
    chat = user_info['chat']

    # 1. 未建檔使用者攔截
    if not user_info['has_profile']:
        has_numbers = bool(re.search(r'\d+', user_msg))
        has_keywords = any(k in user_msg for k in ['減脂', '增肌', '男', '女', '斷食', '體重', '身高', 'kg', 'cm'])

        if has_numbers or has_keywords:
            try:
                response = chat.send_message(f"這是我的身體數據與個人檔案：{user_msg}")
                reply_text = response.text
                user_info['has_profile'] = True
                user_info['profile_str'] = user_msg  # 備份個人數據
            except Exception as e:
                reply_text = f"建檔失敗，錯誤原因: {str(e)}"
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
            response = chat.send_message(user_msg)
            reply_text = response.text
        except Exception as e:
            # 若對話過長或出錯，自動清理歷史紀錄並重新套用數據檔案
            try:
                model = create_fresh_model()
                new_chat = model.start_chat(history=[])
                new_chat.send_message(f"請記住我的身體數據與飲食習慣：{user_info['profile_str']}")
                response = new_chat.send_message(user_msg)
                user_info['chat'] = new_chat
                reply_text = response.text
            except Exception as inner_e:
                reply_text = f"BiteLogic 運算失敗\n錯誤訊息: {str(inner_e)}"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
