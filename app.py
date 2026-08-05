import os
import re
import json
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

# 讀取環境變數
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

# 台灣時區設定 (UTC+8)
TAIWAN_TZ = timezone(timedelta(hours=8))

SYSTEM_PROMPT = """
你是「BiteLogic」——專為台灣外食族設計的精準外食減脂/增肌口袋菜單 AI 顧問。

【重要格式規範（絕對遵守）】
1. 嚴禁使用 `**` 粗體語法！請直接輸出純文字。
2. 請善用【】與 Emoji 做標題區隔。

【服務範疇與偏離主題拒答規範】
你只回答與「飲食、減脂、增肌、熱量、營養素、台灣連鎖餐廳/超商外食選擇」相關的問題。
非相關問題請用 1~2 句話親切拒絕並引導回飲食主題。
"""

def parse_profile_data(raw_text):
    """將用戶建檔文字解析為結構化 JSON"""
    model = genai.GenerativeModel("gemini-3.5-flash")
    prompt = f"""
    請從以下文字擷取個人的健康數據，只輸出純 JSON 格式（嚴禁包含 ```json）：
    輸入內容："{raw_text}"
    輸出格式：{{"height_cm": 180, "weight_kg": 105, "gender": "男", "goal": "減脂"}}
    欄位缺失請設為 null。
    """
    try:
        res = model.generate_content(prompt).text.strip()
        cleaned = re.sub(r'^```json\s*|\s*```$', '', res, flags=re.MULTILINE)
        return json.loads(cleaned)
    except Exception:
        return {}

def analyze_user_intent(user_msg):
    """判斷使用者意圖：要「記錄飲食」還是「詢問推薦/一般對話」"""
    model = genai.GenerativeModel("gemini-3.5-flash")
    prompt = f"""
    請分析用戶訊息意圖，僅輸出純 JSON 格式（嚴禁包含 ```json）：
    用戶訊息："{user_msg}"

    如果是【記錄飲食】（例如：我吃了排骨飯、剛剛喝了無糖豆漿、早餐：蛋餅）：
    {{
        "type": "log",
        "food_name": "餐點名稱與份量",
        "meal_type": "breakfast/lunch/dinner/snack 中最合適者",
        "calories": 熱量估計數值 (整數 kcal),
        "protein_g": 蛋白質估計數值 (整數 g)
    }}

    如果是【詢問菜單/一般對話】（例如：7-11減脂推薦、今天還能吃多少、八方雲集怎麼點）：
    {{
        "type": "query"
    }}
    """
    try:
        res = model.generate_content(prompt).text.strip()
        cleaned = re.sub(r'^```json\s*|\s*```$', '', res, flags=re.MULTILINE)
        return json.loads(cleaned)
    except Exception:
        return {"type": "query"}

def get_today_str():
    return datetime.now(TAIWAN_TZ).strftime("%Y-%m-%d")

def get_user_profile(line_user_id):
    """取得用戶檔案與 ID"""
    res = supabase.table("profiles").select("id, raw_profile_text").eq("line_user_id", line_user_id).execute()
    return res.data[0] if res.data else None

def save_user_profile(line_user_id, raw_text):
    """驗證並儲存用戶個人檔案"""
    parsed = parse_profile_data(raw_text)
    height = parsed.get("height_cm")
    weight = parsed.get("weight_kg")

    try:
        height_num = float(height) if height is not None else None
        weight_num = float(weight) if weight is not None else None
    except (ValueError, TypeError):
        return False, "數據無法解析，請重新輸入合理的身體數據（例如：175cm / 70kg / 男 / 減脂）"

    if not height_num or not (50 <= height_num <= 250):
        return False, "身高數據不太對勁喔！請填寫 50 ~ 250 公分之間的數字。"
    if not weight_num or not (20 <= weight_num <= 300):
        return False, "體重數據不太對勁喔！請填寫 20 ~ 300 公斤之間的數字。"

    payload = {
        "line_user_id": line_user_id,
        "raw_profile_text": raw_text,
        "height_cm": height_num,
        "weight_kg": weight_num,
        "gender": parsed.get("gender"),
        "goal": parsed.get("goal")
    }
    supabase.table("profiles").upsert(payload, on_conflict="line_user_id").execute()
    return True, "建檔成功"

def log_meal_to_supabase(user_id, intent_data):
    """將飲食紀錄寫入 daily_logs 與 meal_items"""
    today = get_today_str()
    cals = int(intent_data.get("calories", 0))
    protein = int(intent_data.get("protein_g", 0))
    food_name = intent_data.get("food_name", "未知餐點")
    meal_type = intent_data.get("meal_type", "snack")

    # 1. 取得或創建今日的 daily_logs
    log_res = supabase.table("daily_logs").select("id, total_calories, total_protein_g").eq("user_id", user_id).eq("log_date", today).execute()
    
    if log_res.data:
        daily_log_id = log_res.data[0]["id"]
        current_cals = log_res.data[0]["total_calories"] or 0
        current_protein = log_res.data[0]["total_protein_g"] or 0
    else:
        new_log = supabase.table("daily_logs").insert({
            "user_id": user_id,
            "log_date": today,
            "total_calories": 0,
            "total_protein_g": 0
        }).execute()
        daily_log_id = new_log.data[0]["id"]
        current_cals = 0
        current_protein = 0

    # 2. 新增每餐品項至 meal_items
    supabase.table("meal_items").insert({
        "daily_log_id": daily_log_id,
        "meal_type": meal_type,
        "food_name": food_name,
        "calories": cals,
        "protein_g": protein
    }).execute()

    # 3. 更新 daily_logs 累計總值
    new_cals = current_cals + cals
    new_protein = current_protein + protein
    supabase.table("daily_logs").update({
        "total_calories": new_cals,
        "total_protein_g": new_protein
    }).eq("id", daily_log_id).execute()

    return food_name, cals, protein, new_cals, new_protein

def get_today_summary(user_id):
    """取得今日已攝取的卡路里與蛋白質"""
    today = get_today_str()
    res = supabase.table("daily_logs").select("total_calories, total_protein_g").eq("user_id", user_id).eq("log_date", today).execute()
    if res.data:
        return res.data[0]["total_calories"], res.data[0]["total_protein_g"]
    return 0, 0

def ask_gemini(profile_str, today_stats, user_msg):
    """呼叫 Gemini 回答菜單推薦與問題"""
    model = genai.GenerativeModel(model_name="gemini-3.5-flash", system_instruction=SYSTEM_PROMPT)
    cal, protein = today_stats
    prompt = (
        f"個人檔案：{profile_str}\n"
        f"今日已攝取：{cal} kcal ｜ 蛋白質 {protein} g\n\n"
        f"用戶問題：{user_msg}"
    )
    response = model.generate_content(prompt)
    return response.text

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

    profile = get_user_profile(line_user_id)

    if not profile:
        # 未建檔用戶處理
        has_numbers = bool(re.search(r'\d+', user_msg))
        has_keywords = any(k in user_msg for k in ['減脂', '增肌', '男', '女', '斷食', '體重', '身高', 'kg', 'cm'])

        if has_numbers or has_keywords:
            try:
                is_success, msg = save_user_profile(line_user_id, user_msg)
                if is_success:
                    reply_text = ask_gemini(user_msg, (0, 0), "這是我的身體數據，請確認建檔成功並詢問我想吃哪家店！")
                else:
                    reply_text = msg
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
        # 已建檔用戶，先進行 AI 意圖辨識
        try:
            intent_data = analyze_user_intent(user_msg)

            if intent_data.get("type") == "log":
                # 執行飲食紀錄流程
                food, cal, protein, total_cal, total_protein = log_meal_to_supabase(profile["id"], intent_data)
                reply_text = (
                    f"📝 【飲食紀錄成功】\n"
                    f"🍽️ 餐點：{food}\n"
                    f"🔥 預估熱量：約 {cal} kcal\n"
                    f"💪 預估蛋白質：約 {protein} g\n\n"
                    f"📊 今日累計攝取：\n"
                    f"• 總熱量：{total_cal} kcal\n"
                    f"• 總蛋白質：{total_protein} g"
                )
            else:
                # 執行一般查詢 / 菜單推薦流程
                today_stats = get_today_summary(profile["id"])
                reply_text = ask_gemini(profile["raw_profile_text"], today_stats, user_msg)

        except Exception as e:
            reply_text = f"系統處理失敗，請重試：{str(e)}"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
