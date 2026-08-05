import os
import re
import json
from urllib.parse import parse_qs
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, 
    PostbackEvent, FlexSendMessage, QuickReply, QuickReplyButton, MessageAction
)
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

【重要回應規範】
1. 嚴禁使用 `**` 粗體語法！請直接輸出純文字。
2. 說重點！極致精簡，純文字回答請控制在 150 字以內。
3. 若用戶只是問「剩餘額度/還能吃多少」，絕對不要推薦菜單！用 3 行列出剩餘熱量與蛋白質即可。
"""

def get_quick_reply(user_id=None):
    """【精準過濾店家】只針對帶有【店家名稱】標籤的歷史紀錄提取真實店家名"""
    items = [
        QuickReplyButton(action=MessageAction(label="📊 今日卡路里", text="查看今日卡路里"))
    ]
    
    if user_id:
        try:
            res = supabase.table("daily_logs").select("id").eq("user_id", user_id).execute()
            if res.data:
                log_ids = [r["id"] for r in res.data]
                meals = supabase.table("meal_items").select("food_name").in_("daily_log_id", log_ids).execute()
                
                if meals.data:
                    freq = {}
                    for m in meals.data:
                        raw_name = m["food_name"]
                        # 嚴格使用正規表達式，只擷取 【 】 裡面的真實店家名稱（例如 【八方雲集】）
                        match = re.search(r'【(.*?)】', raw_name)
                        if match:
                            store_name = match.group(1).strip()
                            if store_name:
                                freq[store_name] = freq.get(store_name, 0) + 1
                    
                    top_stores = sorted(freq, key=freq.get, reverse=True)[:3]
                    for store in top_stores:
                        items.append(QuickReplyButton(action=MessageAction(label=f"🍽️ {store}", text=f"{store}推薦")))
        except Exception:
            pass

    return QuickReply(items=items)

def parse_profile_data(raw_text):
    """解析使用者輸入的健康數據"""
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

def process_ai_in_single_call(profile_str, today_stats, user_msg):
    """單次呼叫 Gemini 處理意圖與生成回應"""
    model = genai.GenerativeModel("gemini-3.5-flash", system_instruction=SYSTEM_PROMPT)
    cal, protein = today_stats
    
    prompt = f"""
    個人檔案：{profile_str}
    今日已攝取：{cal} kcal ｜ 蛋白質 {protein} g
    用戶訊息："{user_msg}"

    請判斷意圖並處理，輸出純 JSON 格式（嚴禁包含 ```json 標籤）：

    情境 A：用戶在【記錄飲食】（例如：我吃了排骨飯、剛剛在 7-11 喝了無糖豆漿）
    {{
        "type": "log",
        "restaurant": "若有提及明確連鎖店家則填寫（如：7-11、八方雲集），無提及則填 null",
        "food_name": "餐點名稱與份量",
        "calories": 熱量估計整數,
        "protein_g": 蛋白質估計整數
    }}

    情境 B：用戶在【詢問外食/餐廳推薦】（例如：7-11減脂推薦、麥當勞怎麼點）
    {{
        "type": "recommendation",
        "restaurant": "餐廳名稱",
        "title": "菜單主題",
        "budget": "預算/熱量說明",
        "items": ["餐點1", "餐點2"],
        "warning": "地雷提醒",
        "total_cal": 熱量數字,
        "total_protein": 蛋白質數字
    }}

    情境 C：用戶在【一般對話/問剩餘卡路里】（例如：你好、我今天還能吃多少）
    {{
        "type": "chat",
        "reply_text": "精簡回答內容（150字以內）"
    }}
    """
    try:
        res = model.generate_content(prompt).text.strip()
        cleaned = re.sub(r'^```json\s*|\s*```$', '', res, flags=re.MULTILINE)
        return json.loads(cleaned)
    except Exception:
        return {"type": "chat", "reply_text": "系統繁忙，請稍後再試！"}

def build_flex_card(data):
    """動態建構 LINE Flex Message 卡片 json"""
    restaurant = data.get("restaurant", "外食推薦")
    title = data.get("title", "精準口袋菜單")
    budget = data.get("budget", "符合個人每日額度")
    items = data.get("items", [])
    warning = data.get("warning", "注意適量攝取")
    total_cal = data.get("total_cal", 500)
    total_protein = data.get("total_protein", 30)

    items_contents = []
    for item in items:
        items_contents.append({
            "type": "text",
            "text": f"• {item}",
            "size": "sm",
            "color": "#555555",
            "margin": "xs"
        })

    flex_json = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#27AE60",
            "contents": [
                {"type": "text", "text": f"🥑 【{restaurant}】", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": title, "weight": "bold", "color": "#FFFFFF", "size": "lg", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": f"📊 {budget}", "weight": "bold", "size": "sm", "color": "#27AE60"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "📝 進店直接點：", "weight": "bold", "size": "sm", "margin": "md"},
                *items_contents,
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"⚠️ 地雷提醒：{warning}", "size": "xs", "color": "#E74C3C", "margin": "md", "wrap": True}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#27AE60",
                    "action": {
                        "type": "postback",
                        "label": f"📝 一鍵紀錄這餐 ({total_cal} kcal)",
                        "data": f"action=log_meal&restaurant={restaurant}&title={title}&cal={total_cal}&protein={total_protein}",
                        "displayText": f"我決定吃【{restaurant}】這套組合！"
                    }
                }
            ]
        }
    }
    return flex_json

def get_today_str():
    return datetime.now(TAIWAN_TZ).strftime("%Y-%m-%d")

def get_user_profile(line_user_id):
    res = supabase.table("profiles").select("id, raw_profile_text, weight_kg").eq("line_user_id", line_user_id).execute()
    return res.data[0] if res.data else None

def save_user_profile(line_user_id, raw_text):
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
    today = get_today_str()
    cals = int(intent_data.get("calories", 0))
    protein = int(intent_data.get("protein_g", 0))
    
    # 組合出乾淨的包含【店家】與餐點名稱的格式
    restaurant = intent_data.get("restaurant")
    food_name_raw = intent_data.get("food_name", "未知餐點")
    
    if restaurant:
        full_food_name = f"【{restaurant}】{food_name_raw}"
    else:
        full_food_name = food_name_raw

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

    supabase.table("meal_items").insert({
        "daily_log_id": daily_log_id,
        "meal_type": "snack",
        "food_name": full_food_name,
        "calories": cals,
        "protein_g": protein
    }).execute()

    new_cals = current_cals + cals
    new_protein = current_protein + protein
    supabase.table("daily_logs").update({
        "total_calories": new_cals,
        "total_protein_g": new_protein
    }).eq("id", daily_log_id).execute()

    return full_food_name, cals, protein, new_cals, new_protein

def get_today_summary(user_id):
    today = get_today_str()
    res = supabase.table("daily_logs").select("total_calories, total_protein_g").eq("user_id", user_id).eq("log_date", today).execute()
    if res.data:
        return res.data[0]["total_calories"], res.data[0]["total_protein_g"]
    return 0, 0

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

@handler.add(PostbackEvent)
def handle_postback(event):
    line_user_id = event.source.user_id
    data = parse_qs(event.postback.data)
    
    action = data.get("action", [""])[0]
    if action == "log_meal":
        restaurant = data.get("restaurant", ["外食"])[0]
        title = data.get("title", ["精準餐點"])[0]
        cal = int(data.get("cal", [0])[0])
        protein = int(data.get("protein", [0])[0])
        
        profile = get_user_profile(line_user_id)
        if profile:
            intent_data = {
                "restaurant": restaurant,
                "food_name": title,
                "calories": cal,
                "protein_g": protein
            }
            food, c, p, total_c, total_p = log_meal_to_supabase(profile["id"], intent_data)
            
            reply_text = (
                f"🎉 【紀錄成功】\n"
                f"🍽️ {food}\n"
                f"🔥 +{c} kcal ｜ 💪 +{p} g 蛋白質\n\n"
                f"📊 今日累計：{total_c} kcal ｜ {total_p} g 蛋白質"
            )
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text, quick_reply=get_quick_reply(profile["id"]))
            )

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    line_user_id = event.source.user_id
    user_msg = event.message.text.strip()

    profile = get_user_profile(line_user_id)

    if not profile:
        has_numbers = bool(re.search(r'\d+', user_msg))
        has_keywords = any(k in user_msg for k in ['減脂', '增肌', '男', '女', '斷食', '體重', '身高', 'kg', 'cm'])

        if has_numbers or has_keywords:
            try:
                is_success, msg = save_user_profile(line_user_id, user_msg)
                if is_success:
                    reply_text = "專屬健康檔案建立成功！請點擊下方選單或直接輸入想吃的餐廳開始使用 🥑"
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text=reply_text, quick_reply=get_quick_reply())
                    )
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
            except Exception as e:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"建檔失敗：{str(e)}"))
        else:
            reply_text = (
                "歡迎來到 BiteLogic 🥑！\n\n"
                "首次使用請先建立專屬檔案 📝\n\n"
                "請直接回覆：\n"
                "【身高 / 體重 / 性別 / 飲食目標 / 飲食習慣】\n"
                "（例如：173 / 85 / 男 / 減脂增肌 / 一天吃兩餐）"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    else:
        try:
            user_id = profile["id"]

            if user_msg == "查看今日卡路里":
                cals, protein = get_today_summary(user_id)
                weight = profile.get("weight_kg") or 70.0
                
                target_cal = int(weight * 20)
                target_protein = int(weight * 1.5)
                
                rem_cal = max(0, target_cal - cals)
                rem_protein = max(0, target_protein - protein)

                reply_text = (
                    f"📊 【今日攝取總計與進度】\n\n"
                    f"🔥 熱量：{cals} / {target_cal} kcal\n"
                    f"└ 剩餘額度：{rem_cal} kcal\n\n"
                    f"💪 蛋白質：{protein} / {target_protein} g\n"
                    f"└ 剩餘額度：{rem_protein} g\n\n"
                    f"💡 提示：直接輸入想吃的餐廳（如：麥當勞）即可獲取專屬減脂菜單！"
                )
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=reply_text, quick_reply=get_quick_reply(user_id))
                )
                return

            today_stats = get_today_summary(user_id)
            ai_res = process_ai_in_single_call(profile["raw_profile_text"], today_stats, user_msg)
            msg_type = ai_res.get("type")

            if msg_type == "log":
                food, cal, protein, total_cal, total_protein = log_meal_to_supabase(user_id, ai_res)
                reply_text = (
                    f"📝 【紀錄成功】\n"
                    f"🍽️ {food}\n"
                    f"🔥 約 {cal} kcal ｜ 💪 約 {protein} g 蛋白質\n\n"
                    f"📊 今日累計：{total_cal} kcal ｜ {total_protein} g 蛋白質"
                )
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=reply_text, quick_reply=get_quick_reply(user_id))
                )
            elif msg_type == "recommendation":
                flex_content = build_flex_card(ai_res)
                flex_message = FlexSendMessage(
                    alt_text=f"BiteLogic 推薦：{ai_res.get('restaurant', '')}口袋菜單",
                    contents=flex_content,
                    quick_reply=get_quick_reply(user_id)
                )
                line_bot_api.reply_message(event.reply_token, flex_message)
            else:
                reply_text = ai_res.get("reply_text", "請輸入想吃的餐廳名稱，例如：麥當勞、7-11、八方雲集")
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=reply_text, quick_reply=get_quick_reply(user_id))
                )

        except Exception as e:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"系統處理失敗，請重試：{str(e)}", quick_reply=get_quick_reply(profile.get("id")))
            )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
