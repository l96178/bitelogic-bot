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
4. 推薦菜單時絕對不要提及任何價格與金額！僅關注熱量與蛋白質。
5. 【數字嚴格規範】：回答若提及熱量或蛋白質剩餘額度，必須完全照抄系統提供的「正確剩餘數字」，絕對禁止自己心算或隨意發明數字！
"""

def calculate_precise_targets(weight_kg, height_cm, gender, goal, activity_level, meal_pattern):
    """加入男性 1500 / 女性 1200 kcal 衛福部與臨床安全熱量底線保護機制"""
    weight = float(weight_kg) if weight_kg else 70.0
    height = float(height_cm) if height_cm else 170.0
    
    # BMR 計算 (Mifflin-St Jeor 公式)
    if gender == "女":
        bmr = (10 * weight) + (6.25 * height) - (5 * 30) - 161
        min_safe_cal = 1200
    else:
        bmr = (10 * weight) + (6.25 * height) - (5 * 30) + 5
        min_safe_cal = 1500

    # PAL 活動係數計算
    if "重度" in str(activity_level):
        pal = 1.55
    elif "站立" in str(activity_level) or "輕度" in str(activity_level):
        pal = 1.375
    else:
        pal = 1.2

    tdee = bmr * pal

    # 目標熱量試算
    goal_str = str(goal)
    if "增肌" in goal_str and "減脂" in goal_str:
        target_cal = int(tdee * 0.85)
    elif "增肌" in goal_str:
        target_cal = int(tdee * 1.1)
    else:
        target_cal = int(tdee * 0.8)

    # 安全保護（大體重防暴增、小體重防過低）
    if weight >= 90:
        target_cal = min(target_cal, int(weight * 20))
        max_protein = 180
    else:
        target_cal = max(target_cal, min_safe_cal)  # 觸發健康安全底線
        max_protein = 200

    target_protein = int(min(weight * 1.6, max_protein))
    return target_cal, target_protein

def get_quick_reply(user_id=None):
    """限制只撈最近 30 天紀錄，徹底避免 SQL 查詢暴增卡死"""
    items = [
        QuickReplyButton(action=MessageAction(label="📊 今日卡路里", text="查看今日卡路里")),
        QuickReplyButton(action=MessageAction(label="⚙️ 修改個人檔案", text="修改檔案"))
    ]
    
    if user_id:
        try:
            thirty_days_ago = (datetime.now(TAIWAN_TZ) - timedelta(days=30)).strftime("%Y-%m-%d")
            res = supabase.table("daily_logs").select("id").eq("user_id", user_id).gte("log_date", thirty_days_ago).execute()
            if res.data:
                log_ids = [r["id"] for r in res.data]
                meals = supabase.table("meal_items").select("food_name").in_("daily_log_id", log_ids).execute()
                
                if meals.data:
                    freq = {}
                    for m in meals.data:
                        raw_name = m["food_name"]
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

def build_summary_flex_card(cals, target_cal, protein, target_protein, goal, last_logged_info=None):
    rem_cal = max(0, target_cal - cals)
    rem_protein = max(0, target_protein - protein)

    cal_pct = min(100, max(0, int((cals / target_cal) * 100))) if target_cal > 0 else 0
    protein_pct = min(100, max(0, int((protein / target_protein) * 100))) if target_protein > 0 else 0

    cal_bar_w = f"{max(3, cal_pct)}%"
    protein_bar_w = f"{max(3, protein_pct)}%"

    if cals > target_cal:
        cal_subtext = f"└ ⚠️ 已超出上限：{cals - target_cal} kcal"
    else:
        cal_subtext = f"└ 剩餘額度：{rem_cal} kcal"

    if protein >= target_protein:
        protein_subtext = "└ 🎉 蛋白質已成功達標！"
    else:
        protein_subtext = f"└ 距離目標：還差 {rem_protein} g"

    body_contents = []

    if last_logged_info:
        food_name = last_logged_info.get("food", "")
        item_cal = last_logged_info.get("cal", 0)
        item_protein = last_logged_info.get("protein", 0)
        body_contents.extend([
            {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#ECFDF5",
                "cornerRadius": "md",
                "paddingAll": "md",
                "contents": [
                    {"type": "text", "text": "📝 成功寫入飲食紀錄", "size": "xs", "color": "#059669", "weight": "bold"},
                    {"type": "text", "text": f"{food_name}", "size": "sm", "weight": "bold", "color": "#065F46", "margin": "xs", "wrap": True},
                    {"type": "text", "text": f"+{item_cal} kcal ｜ +{item_protein} g 蛋白質", "size": "xs", "color": "#047857", "margin": "xs"}
                ]
            },
            {"type": "separator", "margin": "md"}
        ])

    body_contents.extend([
        {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "🔥 熱量攝取", "size": "sm", "weight": "bold", "color": "#374151"},
                        {"type": "text", "text": f"{cals} / {target_cal} kcal ({cal_pct}%)", "size": "xs", "align": "end", "color": "#6B7280"}
                    ]
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#E5E7EB",
                    "height": "8px",
                    "cornerRadius": "4px",
                    "margin": "sm",
                    "contents": [
                        {
                            "type": "box",
                            "layout": "vertical",
                            "backgroundColor": "#EF4444" if cal_pct >= 100 else "#27AE60",
                            "height": "8px",
                            "width": cal_bar_w,
                            "cornerRadius": "4px",
                            "contents": []
                        }
                    ]
                },
                {"type": "text", "text": cal_subtext, "size": "xs", "color": "#9CA3AF", "margin": "xs"}
            ]
        },
        {"type": "separator"},
        {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "💪 蛋白質攝取", "size": "sm", "weight": "bold", "color": "#374151"},
                        {"type": "text", "text": f"{protein} / {target_protein} g ({protein_pct}%)", "size": "xs", "align": "end", "color": "#6B7280"}
                    ]
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#E5E7EB",
                    "height": "8px",
                    "cornerRadius": "4px",
                    "margin": "sm",
                    "contents": [
                        {
                            "type": "box",
                            "layout": "vertical",
                            "backgroundColor": "#3B82F6",
                            "height": "8px",
                            "width": protein_bar_w,
                            "cornerRadius": "4px",
                            "contents": []
                        }
                    ]
                },
                {"type": "text", "text": protein_subtext, "size": "xs", "color": "#9CA3AF", "margin": "xs"}
            ]
        },
        {"type": "separator"},
        {"type": "text", "text": "💡 提示：輸入「刪除上一筆」可撤銷最近一次紀錄。", "size": "xs", "color": "#6B7280", "wrap": True}
    ])

    title_text = "紀錄成功與今日進度" if last_logged_info else "今日攝取總計與進度"

    flex_json = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1F2937",
            "paddingAll": "lg",
            "contents": [
                {"type": "text", "text": title_text, "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"目前模式：{goal or '健康減脂'}", "color": "#9CA3AF", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "paddingAll": "lg",
            "contents": body_contents
        }
    }
    return flex_json

def parse_profile_data(raw_text):
    parsed = {}
    
    nums = [float(n) for n in re.findall(r'\d+(?:\.\d+)?', raw_text)]
    if len(nums) >= 2:
        height_candidates = [n for n in nums if 50 <= n <= 250]
        weight_candidates = [n for n in nums if 20 <= n <= 300]
        
        if height_candidates:
            parsed["height_cm"] = height_candidates[0]
        if weight_candidates:
            weights = [w for w in weight_candidates if w != parsed.get("height_cm")]
            if weights:
                parsed["weight_kg"] = weights[0]

    if "男" in raw_text: parsed["gender"] = "男"
    elif "女" in raw_text: parsed["gender"] = "女"
    
    if "增肌" in raw_text and "減脂" in raw_text:
        parsed["goal"] = "增肌減脂"
    elif "增肌" in raw_text:
        parsed["goal"] = "增肌"
    elif "減脂" in raw_text:
        parsed["goal"] = "減脂"

    if "重度" in raw_text or "勞動" in raw_text:
        parsed["activity_level"] = "重度勞動"
    elif "站立" in raw_text or "走動" in raw_text:
        parsed["activity_level"] = "輕度走動"
    else:
        parsed["activity_level"] = "久坐辦公室"

    if any(k in raw_text for k in ["168", "兩餐", "2餐", "斷食"]):
        parsed["meal_pattern"] = "168斷食(一天兩餐)"
    else:
        parsed["meal_pattern"] = "一天三餐"

    return parsed

def process_ai_in_single_call(profile_str, today_stats, target_stats, user_msg):
    model = genai.GenerativeModel("gemini-3.5-flash", system_instruction=SYSTEM_PROMPT)
    cal, protein = today_stats
    target_cal, target_protein = target_stats
    
    rem_cal = max(0, target_cal - cal)
    rem_protein = max(0, target_protein - protein)

    is_two_meals = "168" in profile_str or "兩餐" in profile_str
    meal_cal_cap = min(800, rem_cal) if is_two_meals else min(650, rem_cal)
    meal_protein_cap = min(45, rem_protein)

    prompt = f"""
    個人檔案：{profile_str}
    今日已攝取：{cal} / {target_cal} kcal（正確剩餘：{rem_cal} kcal）
    蛋白質狀況：{protein} / {target_protein} g（正確還差：{rem_protein} g）
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
    【單餐健康目標】：請組合一套約 {meal_cal_cap} kcal / 蛋白質 {meal_protein_cap} g 的菜單！若額度尚有大幅缺口，請在 warning 提示用戶可於下午或運動後補充高蛋白飲品/豆漿。
    {{
        "type": "recommendation",
        "restaurant": "餐廳名稱",
        "title": "菜單主題（必須精簡且控制在 10 個字以內！例如：麥當勞高蛋白組）",
        "budget": "說明（嚴禁提及價格！例如：單餐飽足目標 {meal_cal_cap} kcal / 蛋白質 {meal_protein_cap}g）",
        "items": ["餐點1", "餐點2", "餐點3"],
        "warning": "地雷提醒與補充建議",
        "total_cal": 這套組合的總熱量數字,
        "total_protein": 這套組合的總蛋白質數字
    }}

    情境 C：用戶在【一般對話/問剩餘卡路里】（例如：你好、我今天還能吃多少）
    {{
        "type": "chat",
        "reply_text": "精簡回答內容（150字以內。若提到剩餘熱量或蛋白質，必須完全照抄上述正確數字：熱量剩 {rem_cal} kcal、蛋白質差 {rem_protein} g）"
    }}
    """
    try:
        res = model.generate_content(prompt)
        if not res or not res.text:
            return {"type": "chat", "reply_text": "AI 暫時無法產生內容，請再試一次。"}
        
        raw_text = res.text.strip()
        cleaned = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE)
        json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        
        if json_match:
            return json.loads(json_match.group(0))
        return {"type": "chat", "reply_text": raw_text}
    except Exception as e:
        print(f"❌ Gemini Error: {str(e)}")
        return {"type": "chat", "reply_text": f"AI 連線處理失敗，原因：{str(e)[:50]}"}

def build_flex_card(data):
    """【漏洞 4 第一與第二防線】：Prompt 限制 10 字內 + Python 切割[:15]，絕不讓 LINE 按鈕失效"""
    restaurant = data.get("restaurant", "外食推薦")
    title = data.get("title", "精準口袋菜單")
    safe_title = title[:15]  # 第二防線：字數安全截斷
    
    budget = data.get("budget", "符合個人每日熱量控制")
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
            "margin": "xs",
            "wrap": True
        })

    flex_json = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#27AE60",
            "contents": [
                {"type": "text", "text": f"【{restaurant}】", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": title, "weight": "bold", "color": "#FFFFFF", "size": "lg", "margin": "xs", "wrap": True}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": f"📊 {budget}", "weight": "bold", "size": "sm", "color": "#27AE60", "wrap": True},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "📝 進店直接點：", "weight": "bold", "size": "sm", "margin": "md"},
                *items_contents,
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"⚠️ 地雷與補充提醒：{warning}", "size": "xs", "color": "#E74C3C", "margin": "md", "wrap": True}
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
                        "data": f"action=log_meal&restaurant={restaurant}&title={safe_title}&cal={total_cal}&protein={total_protein}",
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
    res = supabase.table("profiles").select("id, raw_profile_text, height_cm, weight_kg, gender, goal").eq("line_user_id", line_user_id).execute()
    return res.data[0] if res.data else None

def save_user_profile(line_user_id, raw_text):
    """【漏洞 6 防禦】：強制檢查必須有身高與體重，否則退回重新輸入"""
    parsed = parse_profile_data(raw_text)
    height = parsed.get("height_cm")
    weight = parsed.get("weight_kg")
    gender = parsed.get("gender", "男")
    goal = parsed.get("goal", "減脂")
    activity = parsed.get("activity_level", "久坐辦公室")
    meal_pattern = parsed.get("meal_pattern", "一天三餐")

    if not height or not weight:
        return False, "⚠️ 無法完全辨識您的身高與體重！請務必包含數字（例如：173 / 85 / 男 / 減脂 / 久坐辦公室 / 168斷食）", None, None, None

    full_profile_text = f"{raw_text} ｜ {activity} ｜ {meal_pattern}"

    payload = {
        "line_user_id": line_user_id,
        "raw_profile_text": full_profile_text,
        "height_cm": height,
        "weight_kg": weight,
        "gender": gender,
        "goal": goal
    }
    supabase.table("profiles").upsert(payload, on_conflict="line_user_id").execute()
    
    target_cal, target_protein = calculate_precise_targets(weight, height, gender, goal, activity, meal_pattern)
    return True, full_profile_text, weight, target_cal, target_protein

def delete_last_meal(user_id):
    """【漏洞 3 防禦】：提供撤銷最後一筆紀錄的功能"""
    today = get_today_str()
    log_res = supabase.table("daily_logs").select("id, total_calories, total_protein_g").eq("user_id", user_id).eq("log_date", today).execute()
    if not log_res.data:
        return "今日尚無任何紀錄可刪除。"
    
    daily_log_id = log_res.data[0]["id"]
    current_cals = log_res.data[0]["total_calories"] or 0
    current_protein = log_res.data[0]["total_protein_g"] or 0

    meal_res = supabase.table("meal_items").select("id, food_name, calories, protein_g").eq("daily_log_id", daily_log_id).order("id", desc=True).limit(1).execute()
    if not meal_res.data:
        return "今日尚無飲食項目可刪除。"

    last_meal = meal_res.data[0]
    supabase.table("meal_items").delete().eq("id", last_meal["id"]).execute()

    new_cals = max(0, current_cals - last_meal["calories"])
    new_protein = max(0, current_protein - last_meal["protein_g"])
    supabase.table("daily_logs").update({
        "total_calories": new_cals,
        "total_protein_g": new_protein
    }).eq("id", daily_log_id).execute()

    return f"🗑️ 已成功刪除最近一筆紀錄：【{last_meal['food_name']}】(-{last_meal['calories']} kcal)"

def log_meal_to_supabase(user_id, intent_data):
    today = get_today_str()
    cals = int(intent_data.get("calories", 0))
    protein = int(intent_data.get("protein_g", 0))
    
    restaurant = intent_data.get("restaurant")
    food_name_raw = intent_data.get("food_name", "未知餐點")
    
    if restaurant and restaurant != "null":
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
            
            p_parsed = parse_profile_data(profile.get("raw_profile_text", ""))
            target_cal, target_protein = calculate_precise_targets(
                profile.get("weight_kg"), profile.get("height_cm"), profile.get("gender"), 
                profile.get("goal"), p_parsed.get("activity_level"), p_parsed.get("meal_pattern")
            )

            last_info = {"food": food, "cal": c, "protein": p}
            summary_flex = build_summary_flex_card(total_c, target_cal, total_p, target_protein, profile.get("goal"), last_logged_info=last_info)
            flex_message = FlexSendMessage(
                alt_text=f"BiteLogic 紀錄成功：{food}",
                contents=summary_flex,
                quick_reply=get_quick_reply(profile["id"])
            )
            line_bot_api.reply_message(event.reply_token, flex_message)

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    line_user_id = event.source.user_id
    user_msg = event.message.text.strip()

    profile = get_user_profile(line_user_id)

    # 【漏洞 2 防禦】：處理用戶發起重新建檔的命令
    if user_msg == "修改檔案" or user_msg == "重新建檔":
        reply_text = (
            "⚙️ 【重新建立專屬健康檔案】\n\n"
            "請直接回覆最新數據：\n"
            "【身高 / 體重 / 性別 / 飲食目標 / 活動程度 / 飲食習慣】\n\n"
            "💡 範例：173 / 85 / 男 / 增肌減脂 / 久坐辦公室 / 168斷食"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    if not profile or "重新建檔" in user_msg or (any(k in user_msg for k in ['cm', 'kg', '久坐', '辦公室', '斷食']) and len(re.findall(r'\d+', user_msg)) >= 2):
        has_numbers = len(re.findall(r'\d+', user_msg)) >= 2
        if has_numbers:
            try:
                is_success, raw_p_text, weight, target_cal, target_protein = save_user_profile(line_user_id, user_msg)
                if is_success:
                    p_parsed = parse_profile_data(raw_p_text)
                    reply_text = (
                        f"🎉 【專屬健康檔案更新成功】\n\n"
                        f"🎯 目標模式：{p_parsed.get('goal', '減脂')}\n"
                        f"🏃 活動程度：{p_parsed.get('activity_level', '久坐辦公室')}\n"
                        f"🍽️ 飲食習慣：{p_parsed.get('meal_pattern', '一天三餐')}\n\n"
                        f"📊 您的每日精準控制目標：\n"
                        f"• 建議總熱量：約 {target_cal} kcal / 日\n"
                        f"• 建議蛋白質：約 {target_protein} g / 日\n\n"
                        f"💡 直接輸入想吃的餐廳（如：麥當勞、7-11）即可獲取口袋菜單！"
                    )
                    
                    new_profile = get_user_profile(line_user_id)
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text=reply_text, quick_reply=get_quick_reply(new_profile["id"] if new_profile else None))
                    )
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=raw_p_text))
            except Exception as e:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"建檔失敗：{str(e)}"))
        else:
            reply_text = (
                "歡迎來到 BiteLogic 🥑！\n\n"
                "首次使用請先建立專屬檔案 📝\n\n"
                "請直接回覆：\n"
                "【身高 / 體重 / 性別 / 飲食目標 / 活動程度 / 飲食習慣】\n\n"
                "💡 範例：173 / 85 / 男 / 增肌減脂 / 久坐辦公室 / 168斷食"
            )
            quick_reply_demo = QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="📝 填寫範例A (久坐/三餐)", text="173 / 85 / 男 / 減脂 / 久坐辦公室 / 一天三餐")),
                QuickReplyButton(action=MessageAction(label="📝 填寫範例B (168斷食)", text="173 / 85 / 男 / 增肌減脂 / 久坐辦公室 / 168斷食"))
            ])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text, quick_reply=quick_reply_demo))
    else:
        try:
            user_id = profile["id"]
            p_parsed = parse_profile_data(profile.get("raw_profile_text", ""))
            
            target_cal, target_protein = calculate_precise_targets(
                profile.get("weight_kg"), profile.get("height_cm"), profile.get("gender"), 
                profile.get("goal"), p_parsed.get("activity_level"), p_parsed.get("meal_pattern")
            )

            # 【漏洞 3 防禦】：執行刪除最新一筆紀錄
            if user_msg == "刪除上一筆" or user_msg == "刪除紀錄":
                del_msg = delete_last_meal(user_id)
                cals, protein = get_today_summary(user_id)
                summary_flex = build_summary_flex_card(cals, target_cal, protein, target_protein, profile.get("goal"))
                line_bot_api.reply_message(
                    event.reply_token,
                    [TextSendMessage(text=del_msg), FlexSendMessage(alt_text="今日進度", contents=summary_flex, quick_reply=get_quick_reply(user_id))]
                )
                return

            if user_msg == "查看今日卡路里":
                cals, protein = get_today_summary(user_id)
                summary_flex = build_summary_flex_card(cals, target_cal, protein, target_protein, profile.get("goal"))
                flex_message = FlexSendMessage(
                    alt_text="BiteLogic 今日攝取進度",
                    contents=summary_flex,
                    quick_reply=get_quick_reply(user_id)
                )
                line_bot_api.reply_message(event.reply_token, flex_message)
                return

            today_stats = get_today_summary(user_id)
            target_stats = (target_cal, target_protein)
            ai_res = process_ai_in_single_call(profile["raw_profile_text"], today_stats, target_stats, user_msg)
            msg_type = ai_res.get("type")

            if msg_type == "log":
                food, cal, protein, total_cal, total_protein = log_meal_to_supabase(user_id, ai_res)
                last_info = {"food": food, "cal": cal, "protein": protein}
                summary_flex = build_summary_flex_card(total_cal, target_cal, total_protein, target_protein, profile.get("goal"), last_logged_info=last_info)
                flex_message = FlexSendMessage(
                    alt_text=f"BiteLogic 紀錄成功：{food}",
                    contents=summary_flex,
                    quick_reply=get_quick_reply(user_id)
                )
                line_bot_api.reply_message(event.reply_token, flex_message)
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
