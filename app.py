import os
import re
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# 環境變數讀取
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# 初始化套件
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

SYSTEM_PROMPT = """
你是「BiteLogic」——專為台灣外食族設計的精準外食減脂/增肌口袋菜單 AI 顧問。

【重要格式規範（絕對遵守）】
1. 嚴禁使用 `**` 粗體語法！請直接輸出純文字。
2. 請善用【】與 Emoji 做標題區隔。

【服務範疇與偏離主題拒答規範】
你只回答與「飲食、減脂、增肌、熱量、營養素、台灣連鎖餐廳/超商外食選擇」相關的問題。
非相關問題請用 1~2 句話親切拒絕並引導回飲食主題。

【回應格式範例】
🥟 【八方雲集】減脂高蛋白口袋菜單
📊 本餐預算：約 600 kcal ｜ 蛋白質 40g+
📝 進店直接點：
• 招牌水餃 8 顆（澱粉上限）
• 旗魚丸湯 1 碗（清淡低卡補水）
• 無糖豆漿 1 瓶
⚠️ 地雷提醒：千萬別點鍋貼與酸辣湯！
"""

def ask_gemini(profile_str, user_msg):
    # 鎖定 CP 值與速度最佳平衡的 gemini-3.5-flash
    model = genai.GenerativeModel(
        model_name="gemini-3.5-flash", 
        system_instruction=SYSTEM_PROMPT
    )
    
    prompt = f"我的身體數據與個人檔案：{profile_str}\n\n我的問題或指令：{user_msg}" if profile_str else user_msg
    response = model.generate_content(prompt)
    return response.text

# Supabase 資料庫操作
def get_user_profile(line_user_id):
    res = supabase.table("profiles").select("raw_profile_text").eq("line_user_id", line_user_id).execute()
    return res.data[0]["raw_profile_text"] if res.data else None

def save_user_profile(line_user_id, raw_text):
    supabase.table("profiles").upsert({
        "line_user_id": line_user_id,
        "raw_profile_text": raw_text
    }, on_conflict="line_user_id").execute()

@app.route("/", methods=['GET'])
def health_check():
    return 'BiteLogic API is running', 200

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
    line_user_id = event.source.user_id
    user_msg = event.message.text.strip()

    # 查詢用戶檔案
    existing_profile = get_user_profile(line_user_id)

    if not existing_profile:
        has_numbers = bool(re.search(r'\d+', user_msg))
        has_keywords = any(k in user_msg for k in ['減脂', '增肌', '男', '女', '斷食', '體重', '身高', 'kg', 'cm'])

        if has_numbers or has_keywords:
            try:
                save_user_profile(line_user_id, user_msg)
                reply_text = ask_gemini(user_msg, "這是我的身體數據，請確認建檔成功並詢問我想吃哪家店！")
            except Exception as e:
                reply_text = f"建檔失敗：{str(e)}"
        else:
            reply_text = (
                "歡迎來到 BiteLogic 🥑！\n\n"
                "首次使用請先建立專屬檔案 📝\n\n"
                "請直接回覆：\n"
                "【身高 / 體重 / 性別 / 飲食目標 / 飲食習慣】\n"
                "（例如：173 / 85 / 男 / 減脂增肌 / 一天吃兩餐）"
            )
    else:
        try:
            reply_text = ask_gemini(existing_profile, user_msg)
        except Exception as e:
            reply_text = f"系統繁忙，請稍後再試：{str(e)}"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
