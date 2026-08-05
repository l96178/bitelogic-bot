import os
import re
import json
from urllib.parse import parse_qs, urlencode
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, 
    PostbackEvent, FlexSendMessage, QuickReply, QuickReplyButton, MessageAction, PostbackAction
)
import google.generativeai as genai
from supabase import create_client, Client

app = Flask(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TAIWAN_TZ = timezone(timedelta(hours=8))

# 精簡 System Prompt 降低 Token 耗費
SYSTEM_PROMPT = "你是 BiteLogic 外食 AI 顧問。純文字回答、無粗體、無Emoji、控在 100 字內。不提價格。若提剩餘額度必須完全照抄給定的正確數字。"

GOAL_MAP_TO_DB = {"減脂": "fat_loss", "增肌": "muscle_gain", "增肌減脂": "recomp"}
GOAL_MAP_TO_DISP = {"fat_loss": "減脂", "muscle_gain": "增肌", "recomp": "增肌減脂"}

def format_num(val):
    try:
        f = float(val)
        return int(f) if f.is_integer() else f
    except Exception:
        return val

def extract_last_restaurant(raw_text):
    if not raw_text: return None
    match = re.search(r'上次餐廳：([^\s｜]+)', raw_text)
    return match.group(1).strip() if match else None

def update_last_restaurant_in_profile(user_id, current_raw_text, store_name):
    if not store_name or store_name == "null": return
    cleaned_text = re.sub(r'\s*｜\s*上次餐廳：[^\s｜]+', '', current_raw_text or '').strip()
    new_raw_text = f"{cleaned_text} ｜ 上次餐廳：{store_name}"
    supabase.table("profiles").update({"raw_profile_text": new_raw_text}).eq("id", user_id).execute()

def calculate_precise_targets(weight_kg, height_cm, age, gender, goal, activity_level, meal_pattern):
    weight = float(weight_kg) if weight_kg else 70.0
    height = float(height_cm) if height_cm else 170.0
    age_num = float(age) if age else 30.0
    
    bmr = (10 * weight) + (6.25 * height) - (5 * age_num) + (-161 if gender == "女" else 5)
    min_safe_cal = 1200 if gender == "女" else 1500

    act_str = str(activity_level)
    pal = 1.725 if "重度" in act_str else (1.55 if "規律" in act_str or "運動" in act_str else (1.375 if "走動" in act_str or "輕度" in act_str else 1.2))

    tdee = bmr * pal
    goal_str = str(goal)

    if "muscle_gain" in goal_str or "增肌" in goal_str:
        target_cal = int(tdee * (0.88 if "recomp" in goal_str or "減脂" in goal_str else 1.1))
    else:
        target_cal = int(tdee * 0.8)

    target_cal = max(target_cal, int(bmr + 50), min_safe_cal)
    max_protein = 180 if weight >= 90 else 200
    target_protein = int(min(weight * 1.6, max_protein))
    
    return target_cal, target_protein

def get_quick_reply(user_id=None):
    items = [
        QuickReplyButton(action=MessageAction(label="今日卡路里", text="查看今日卡路里")),
        QuickReplyButton(action=MessageAction(label="修改個人檔案", text="修改檔案"))
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
                        match = re.search(r'【(.*?)】', m["food_name"])
                        if match and match.group(1).strip():
                            s = match.group(1).strip()
                            freq[s] = freq.get(s, 0) + 1
                    for store in sorted(freq, key=freq.get, reverse=True)[:3]:
                        items.append(QuickReplyButton(action=MessageAction(label=f"{store}推薦", text=f"{store}推薦")))
        except Exception:
            pass
    return QuickReply(items=items)

def build_summary_flex_card(cals, target_cal, protein, target_protein, goal, last_logged_info=None):
    rem_cal = max(0, target_cal - cals)
    rem_protein = max(0, target_protein - protein)

    cal_pct = min(100, max(0, int((cals / target_cal) * 100))) if target_cal > 0 else 0
    protein_pct = min(100, max(0, int((protein / target_protein) * 100))) if target_protein > 0 else 0

    cal_subtext = f"└ 已超出上限：{cals - target_cal} kcal" if cals > target_cal else f"└ 剩餘額度：{rem_cal} kcal"
    protein_subtext = "└ 蛋白質已成功達標！" if protein >= target_protein else f"└ 距離目標：還差 {rem_protein} g"

    body_contents = []

    if last_logged_info:
        body_contents.extend([
            {
                "type": "box", "layout": "vertical", "backgroundColor": "#ECFDF5", "cornerRadius": "md", "paddingAll": "md",
                "contents": [
                    {"type": "text", "text": "成功寫入飲食紀錄", "size": "xs", "color": "#059669", "weight": "bold"},
                    {"type": "text", "text": f"{last_logged_info.get('food', '')}", "size": "sm", "weight": "bold", "color": "#065F46", "margin": "xs", "wrap": True},
                    {"type": "text", "text": f"+{last_logged_info.get('cal', 0)} kcal ｜ +{last_logged_info.get('protein', 0)} g 蛋白質", "size": "xs", "color": "#047857", "margin": "xs"}
                ]
            },
            {"type": "separator", "margin": "md"}
        ])

    disp_goal = GOAL_MAP_TO_DISP.get(goal, goal) or "健康減脂"

    body_contents.extend([
        {
            "type": "box", "layout": "vertical",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "熱量攝取", "size": "sm", "weight": "bold", "color": "#374151"},
                        {"type": "text", "text": f"{cals} / {target_cal} kcal ({cal_pct}%)", "size": "xs", "align": "end", "color": "#6B7280"}
                    ]
                },
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#E5E7EB", "height": "8px", "cornerRadius": "4px", "margin": "sm",
                    "contents": [{"type": "box", "layout": "vertical", "backgroundColor": "#EF4444" if cal_pct >= 100 else "#27AE60", "height": "8px", "width": f"{max(3, cal_pct)}%", "cornerRadius": "4px", "contents": []}]
                },
                {"type": "text", "text": cal_subtext, "size": "xs", "color": "#9CA3AF", "margin": "xs"}
            ]
        },
        {"type": "separator"},
        {
            "type": "box", "layout": "vertical",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "蛋白質攝取", "size": "sm", "weight": "bold", "color": "#374151"},
                        {"type": "text", "text": f"{protein} / {target_protein} g ({protein_pct}%)", "size": "xs", "align": "end", "color": "#6B7280"}
                    ]
                },
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#E5E7EB", "height": "8px", "cornerRadius": "4px", "margin": "sm",
                    "contents": [{"type": "box", "layout": "vertical", "backgroundColor": "#3B82F6", "height": "8px", "width": f"{max(3, protein_pct)}%", "cornerRadius": "4px", "contents": []}]
                },
                {"type": "text", "text": protein_subtext, "size": "xs", "color": "#9CA3AF", "margin": "xs"}
            ]
        },
        {"type": "separator"},
        {"type": "text", "text": "提示：輸入「刪除上一筆」可撤銷最近一次紀錄。", "size": "xs", "color": "#6B7280", "wrap": True}
    ])

    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1F2937", "paddingAll": "lg",
            "contents": [
                {"type": "text", "text": "紀錄成功與今日進度" if last_logged_info else "今日攝取總計與進度", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"目前模式：{disp_goal}", "color": "#9CA3AF", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "lg", "contents": body_contents}
    }

def parse_basic_profile(raw_text):
    parsed = {}
    nums = [float(n) for n in re.findall(r'\d+(?:\.\d+)?', raw_text)]
    if len(nums) >= 3:
        h = [n for n in nums if 50 <= n <= 250]
        w = [n for n in nums if 20 <= n <= 300]
        a = [n for n in nums if 10 <= n <= 100]
        if h: parsed["height_cm"] = format_num(h[0])
        if w:
            w_filtered = [item for item in w if item != parsed.get("height_cm")]
            if w_filtered: parsed["weight_kg"] = format_num(w_filtered[0])
        if a:
            a_filtered = [item for item in a if item != parsed.get("height_cm") and item != parsed.get("weight_kg")]
            parsed["age"] = format_num(a_filtered[0]) if a_filtered else 30
    elif len(nums) == 2:
        parsed["height_cm"], parsed["weight_kg"], parsed["age"] = format_num(nums[0]), format_num(nums[1]), 30

    parsed["gender"] = "女" if "女" in raw_text else "男"
    return parsed if (parsed.get("height_cm") and parsed.get("weight_kg")) else None

def get_today_meals_list(user_id):
    today = get_today_str()
    log_res = supabase.table("daily_logs").select("id").eq("user_id", user_id).eq("log_date", today).execute()
    if not log_res.data: return []
    meals_res = supabase.table("meal_items").select("food_name, calories, protein_g").eq("daily_log_id", log_res.data[0]["id"]).order("created_at", desc=False).execute()
    return meals_res.data if meals_res.data else []

def process_ai_in_single_call(profile_str, today_stats, target_stats, user_msg, last_restaurant=None, today_meals=None):
    model = genai.GenerativeModel("gemini-2.5-flash-lite", system_instruction=SYSTEM_PROMPT)
    cal, protein = today_stats
    target_cal, target_protein = target_stats
    
    rem_cal, rem_protein = max(0, target_cal - cal), max(0, target_protein - protein)
    logged_count = len(today_meals) if today_meals else 0
    total_planned_meals = 2 if ("168" in profile_str or "兩餐" in profile_str) else 3
    remaining_meals = max(1, total_planned_meals - logged_count)

    meal_cal_cap = int(rem_cal / remaining_meals)
    meal_protein_cap = int(rem_protein / remaining_meals)

    # 修正 f-string 內的 JSON 括號轉義
    prompt = f"""
    檔案:{profile_str}|上次餐廳:{last_restaurant or '無'}
    今日已攝取:{cal}/{target_cal}kcal,剩餘:{rem_cal}kcal|蛋白質還差:{rem_protein}g|剩餘餐數:{remaining_meals}
    用戶說:"{user_msg}"

    判斷意圖，僅輸出純 JSON:
    A.飲食紀錄: {{"type":"log","restaurant":"連鎖店或null","food_name":"名稱","calories":整數,"protein_g":整數}}
    B.餐廳推薦/調整: 設計約{meal_cal_cap}kcal/{meal_protein_cap}g蛋白質組合。若為調整語氣且上次餐廳存在，優先沿用{last_restaurant}。
    {{"type":"recommendation","restaurant":"店名","title":"10字內主題","budget":"單餐目標{meal_cal_cap}kcal","items":[{{"name":"單品名","cal":整數,"protein":整數}}],"warning":"避坑提示","total_cal":整數,"total_protein":整數}}
    C.一般對話/問額度: {{"type":"chat","reply_text":"精簡回覆(熱量剩{rem_cal}kcal,蛋白質差{rem_protein}g)"}}
    """
    try:
        res = model.generate_content(prompt)
        if not res or not res.text: return {"type": "chat", "reply_text": "AI 暫時無法回應，請重試。"}
        cleaned = re.sub(r'^```json\s*|\s*```$', '', res.text.strip(), flags=re.MULTILINE)
        json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        return json.loads(json_match.group(0)) if json_match else {"type": "chat", "reply_text": cleaned}
    except Exception as e:
        return {"type": "chat", "reply_text": f"AI 連線失敗：{str(e)[:40]}"}

def build_flex_card(data):
    restaurant = data.get("restaurant", "外食推薦")
    title = data.get("title", "精準口袋菜單")
    budget = data.get("budget", "符合個人每日熱量控制")
    items = data.get("items", [])
    warning = data.get("warning", "注意適量攝取")
    total_cal = data.get("total_cal", 500)
    total_protein = data.get("total_protein", 30)

    items_contents, item_names_list = [], []

    for item in items:
        if isinstance(item, dict):
            name, c, p = item.get("name", "餐點"), item.get("cal", 0), item.get("protein", 0)
            items_contents.append({"type": "text", "text": f"• {name} (約 {c} kcal / {p}g 蛋白質)", "size": "sm", "color": "#555555", "margin": "xs", "wrap": True})
            item_names_list.append(name)
        elif isinstance(item, str):
            clean_name = item.split("(")[0].replace("•", "").strip() if "(" in item else item.replace("•", "").strip()
            items_contents.append({"type": "text", "text": f"• {item}" if not item.startswith("•") else item, "size": "sm", "color": "#555555", "margin": "xs", "wrap": True})
            item_names_list.append(clean_name)

    log_title = "、".join(item_names_list) if item_names_list else title
    safe_log_title = log_title[:65]

    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#27AE60",
            "contents": [
                {"type": "text", "text": f"【{restaurant}】", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": title, "weight": "bold", "color": "#FFFFFF", "size": "lg", "margin": "xs", "wrap": True}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": budget, "weight": "bold", "size": "sm", "color": "#27AE60", "wrap": True},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "進店直接點：", "weight": "bold", "size": "sm", "margin": "md"},
                *items_contents,
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"地雷與補充提醒：{warning}", "size": "xs", "color": "#E74C3C", "margin": "md", "wrap": True}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [
                {
                    "type": "button", "style": "primary", "color": "#27AE60",
                    "action": {
                        "type": "postback",
                        "label": f"一鍵紀錄這餐 ({total_cal} kcal)",
                        "data": f"action=log_meal&restaurant={restaurant}&title={safe_log_title}&cal={total_cal}&protein={total_protein}",
                        "displayText": f"我決定吃【{restaurant}】這套組合！"
                    }
                }
            ]
        }
    }

def get_today_str(): return datetime.now(TAIWAN_TZ).strftime("%Y-%m-%d")

def get_user_profile(line_user_id):
    res = supabase.table("profiles").select("id, raw_profile_text, height_cm, weight_kg, age, gender, goal, target_calories, target_protein_g").eq("line_user_id", line_user_id).execute()
    return res.data[0] if res.data else None

def delete_last_meal(user_id):
    today = get_today_str()
    log_res = supabase.table("daily_logs").select("id, total_calories, total_protein_g").eq("user_id", user_id).eq("log_date", today).execute()
    if not log_res.data: return "今日尚無任何紀錄可刪除。"
    
    daily_log_id = log_res.data[0]["id"]
    meal_res = supabase.table("meal_items").select("id, food_name, calories, protein_g").eq("daily_log_id", daily_log_id).order("id", desc=True).limit(1).execute()
    if not meal_res.data: return "今日尚無飲食項目可刪除。"

    last_meal = meal_res.data[0]
    supabase.table("meal_items").delete().eq("id", last_meal["id"]).execute()

    new_cals = max(0, (log_res.data[0]["total_calories"] or 0) - last_meal["calories"])
    new_protein = max(0, (log_res.data[0]["total_protein_g"] or 0) - last_meal["protein_g"])
    supabase.table("daily_logs").update({"total_calories": new_cals, "total_protein_g": new_protein}).eq("id", daily_log_id).execute()

    return f"已成功刪除最近一筆紀錄：【{last_meal['food_name']}】(-{last_meal['calories']} kcal)"

def log_meal_to_supabase(user_id, intent_data):
    today = get_today_str()
    cals, protein = int(intent_data.get("calories", 0)), int(intent_data.get("protein_g", 0))
    restaurant, food_name_raw = intent_data.get("restaurant"), intent_data.get("food_name", "未知餐點")
    full_food_name = f"【{restaurant}】{food_name_raw}" if (restaurant and restaurant != "null") else food_name_raw

    log_res = supabase.table("daily_logs").select("id, total_calories, total_protein_g").eq("user_id", user_id).eq("log_date", today).execute()
    if log_res.data:
        daily_log_id = log_res.data[0]["id"]
        current_cals, current_protein = log_res.data[0]["total_calories"] or 0, log_res.data[0]["total_protein_g"] or 0
    else:
        new_log = supabase.table("daily_logs").insert({"user_id": user_id, "log_date": today, "total_calories": 0, "total_protein_g": 0}).execute()
        daily_log_id, current_cals, current_protein = new_log.data[0]["id"], 0, 0

    supabase.table("meal_items").insert({"daily_log_id": daily_log_id, "meal_type": "snack", "food_name": full_food_name, "calories": cals, "protein_g": protein}).execute()

    new_cals, new_protein = current_cals + cals, current_protein + protein
    supabase.table("daily_logs").update({"total_calories": new_cals, "total_protein_g": new_protein}).eq("id", daily_log_id).execute()

    return full_food_name, cals, protein, new_cals, new_protein

def get_today_summary(user_id):
    today = get_today_str()
    res = supabase.table("daily_logs").select("total_calories, total_protein_g").eq("user_id", user_id).eq("log_date", today).execute()
    return (res.data[0]["total_calories"], res.data[0]["total_protein_g"]) if res.data else (0, 0)

@app.route("/", methods=['GET'])
def health_check(): return 'BiteLogic API is running', 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(PostbackEvent)
def handle_postback(event):
    line_user_id = event.source.user_id
    data = parse_qs(event.postback.data)
    action = data.get("action", [""])[0]

    if action == "log_meal":
        restaurant = data.get("restaurant", ["外食"])[0]
        title = data.get("title", ["精準餐點"])[0]
        cal, protein = int(data.get("cal", [0])[0]), int(data.get("protein", [0])[0])
        
        profile = get_user_profile(line_user_id)
        if profile:
            intent_data = {"restaurant": restaurant, "food_name": title, "calories": cal, "protein_g": protein}
            food, c, p, total_c, total_p = log_meal_to_supabase(profile["id"], intent_data)
            target_cal, target_protein = profile.get("target_calories") or 2000, profile.get("target_protein_g") or 150

            summary_flex = build_summary_flex_card(total_c, target_cal, total_p, target_protein, profile.get("goal"), last_logged_info={"food": food, "cal": c, "protein": p})
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"BiteLogic 紀錄成功：{food}", contents=summary_flex, quick_reply=get_quick_reply(profile["id"])))

    elif action == "step_goal":
        h, w, a, g = data.get("h", ["170"])[0], data.get("w", ["70"])[0], data.get("a", ["30"])[0], data.get("g", ["男"])[0]
        goal = data.get("goal", ["減脂"])[0]

        q_items = [
            QuickReplyButton(action=PostbackAction(label="久坐辦公", data=urlencode({"action": "step_activity", "h": h, "w": w, "a": a, "g": g, "goal": goal, "act": "久坐辦公"}), display_text="我選擇：久坐辦公")),
            QuickReplyButton(action=PostbackAction(label="時常走動", data=urlencode({"action": "step_activity", "h": h, "w": w, "a": a, "g": g, "goal": goal, "act": "時常走動"}), display_text="我選擇：時常走動")),
            QuickReplyButton(action=PostbackAction(label="規律運動", data=urlencode({"action": "step_activity", "h": h, "w": w, "a": a, "g": g, "goal": goal, "act": "規律運動"}), display_text="我選擇：規律運動")),
            QuickReplyButton(action=PostbackAction(label="重度勞動", data=urlencode({"action": "step_activity", "h": h, "w": w, "a": a, "g": g, "goal": goal, "act": "重度勞動"}), display_text="我選擇：重度勞動"))
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"已選擇飲食目標：【{goal}】\n\n下一步，請選擇您的【日常活動程度】：\n（直接點選下方按鈕）", quick_reply=QuickReply(items=q_items)))

    elif action == "step_activity":
        h, w, a, g = data.get("h", ["170"])[0], data.get("w", ["70"])[0], data.get("a", ["30"])[0], data.get("g", ["男"])[0]
        goal, act = data.get("goal", ["減脂"])[0], data.get("act", ["久坐辦公"])[0]

        q_items = [
            QuickReplyButton(action=PostbackAction(label="一天三餐", data=urlencode({"action": "step_meal", "h": h, "w": w, "a": a, "g": g, "goal": goal, "act": act, "meal": "一天三餐"}), display_text="我選擇：一天三餐")),
            QuickReplyButton(action=PostbackAction(label="168斷食 (一天兩餐)", data=urlencode({"action": "step_meal", "h": h, "w": w, "a": a, "g": g, "goal": goal, "act": act, "meal": "168斷食(一天兩餐)"}), display_text="我選擇：168斷食(一天兩餐)"))
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"已設定活動程度：【{act}】\n\n最後一步，請選擇您的【飲食模式/餐數】：\n（直接點選下方按鈕）", quick_reply=QuickReply(items=q_items)))

    elif action == "step_meal":
        h, w, a = float(data.get("h", [170])[0]), float(data.get("w", [70])[0]), float(data.get("a", [30])[0])
        g, goal_text, act, meal = data.get("g", ["男"])[0], data.get("goal", ["減脂"])[0], data.get("act", ["久坐辦公"])[0], data.get("meal", ["一天三餐"])[0]

        db_goal = GOAL_MAP_TO_DB.get(goal_text, "fat_loss")
        target_cal, target_protein = calculate_precise_targets(w, h, a, g, goal_text, act, meal)
        full_profile_text = f"身高{format_num(h)}cm / 體重{format_num(w)}kg / {format_num(a)}歲 / {g} / {goal_text} / {act} / {meal}"

        payload = {
            "line_user_id": line_user_id, "raw_profile_text": full_profile_text, "height_cm": format_num(h),
            "weight_kg": format_num(w), "age": format_num(a), "gender": g, "goal": db_goal,
            "target_calories": target_cal, "target_protein_g": target_protein, "updated_at": datetime.now(timezone.utc).isoformat()
        }
        supabase.table("profiles").upsert(payload, on_conflict="line_user_id").execute()
        new_profile = get_user_profile(line_user_id)

        reply_text = (
            f"【專屬健康檔案建檔成功】\n\n基本數據：{format_num(h)}cm / {format_num(w)}kg / {format_num(a)}歲\n"
            f"目標模式：{goal_text}\n活動程度：{act}\n飲食習慣：{meal}\n\n"
            f"您的每日精準控制目標：\n• 建議總熱量：約 {target_cal} kcal / 日\n• 建議蛋白質：約 {target_protein} g / 日\n\n"
            f"提示：直接輸入想吃的餐廳（如：麥當勞、7-11）即可獲取口袋菜單！"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text, quick_reply=get_quick_reply(new_profile["id"] if new_profile else None)))

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    line_user_id = event.source.user_id
    user_msg = event.message.text.strip()
    profile = get_user_profile(line_user_id)

    if user_msg in ["修改檔案", "重新建檔"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="【重新建立專屬健康檔案】\n\n請直接回覆基本數據：\n【身高 / 體重 / 年齡 / 性別】\n\n範例：173 / 85 / 30 / 男"))
        return

    basic_profile = parse_basic_profile(user_msg)

    if not profile or basic_profile:
        if basic_profile:
            h, w, a, g = str(basic_profile["height_cm"]), str(basic_profile["weight_kg"]), str(basic_profile.get("age", 30)), basic_profile["gender"]
            q_items = [
                QuickReplyButton(action=PostbackAction(label="健康減脂", data=urlencode({"action": "step_goal", "h": h, "w": w, "a": a, "g": g, "goal": "減脂"}), display_text="我選擇：健康減脂")),
                QuickReplyButton(action=PostbackAction(label="精準增肌", data=urlencode({"action": "step_goal", "h": h, "w": w, "a": a, "g": g, "goal": "增肌"}), display_text="我選擇：精準增肌")),
                QuickReplyButton(action=PostbackAction(label="增肌減脂", data=urlencode({"action": "step_goal", "h": h, "w": w, "a": a, "g": g, "goal": "增肌減脂"}), display_text="我選擇：增肌減脂"))
            ]
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"收到您的基本數據！\n({h}cm / {w}kg / {a}歲 / {g})\n\n請選擇您的【飲食目標】：\n（直接點選下方按鈕）", quick_reply=QuickReply(items=q_items)))
            return
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="歡迎來到 BiteLogic！\n\n首次使用請先建立專屬檔案\n\n請直接回覆基本數據：\n【身高 / 體重 / 年齡 / 性別】\n\n範例：173 / 85 / 30 / 男"))
            return

    try:
        user_id = profile["id"]
        target_cal, target_protein = profile.get("target_calories"), profile.get("target_protein_g")
        raw_p_text = profile.get("raw_profile_text", "")

        if not target_cal or not target_protein:
            target_cal, target_protein = calculate_precise_targets(profile.get("weight_kg"), profile.get("height_cm"), profile.get("age", 30), profile.get("gender"), profile.get("goal"), raw_p_text, raw_p_text)

        if any(k in user_msg for k in ["今天吃了啥", "今天吃了什麼", "今天吃了哪些", "吃了啥", "吃了什麼", "飲食紀錄", "紀錄明細"]):
            meals = get_today_meals_list(user_id)
            if not meals:
                reply_text = "今天尚無任何飲食紀錄。"
            else:
                lines = ["今日已紀錄餐點："]
                for idx, m in enumerate(meals, 1): lines.append(f"{idx}. {m['food_name']} (+{m['calories']} kcal / +{m['protein_g']}g 蛋白質)")
                cals, protein = get_today_summary(user_id)
                lines.append(f"\n今日總計：{cals} / {target_cal} kcal ｜ 蛋白質：{protein} / {target_protein} g")
                lines.append(f"剩餘額度：{max(0, target_cal - cals)} kcal ｜ 蛋白質還差：{max(0, target_protein - protein)} g")
                reply_text = "\n".join(lines)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text, quick_reply=get_quick_reply(user_id)))
            return

        if user_msg in ["刪除上一筆", "刪除紀錄"]:
            del_msg = delete_last_meal(user_id)
            cals, protein = get_today_summary(user_id)
            summary_flex = build_summary_flex_card(cals, target_cal, protein, target_protein, profile.get("goal"))
            line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=del_msg), FlexSendMessage(alt_text="今日進度", contents=summary_flex, quick_reply=get_quick_reply(user_id))])
            return

        if user_msg == "查看今日卡路里":
            cals, protein = get_today_summary(user_id)
            summary_flex = build_summary_flex_card(cals, target_cal, protein, target_protein, profile.get("goal"))
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="BiteLogic 今日攝取進度", contents=summary_flex, quick_reply=get_quick_reply(user_id)))
            return

        last_restaurant = extract_last_restaurant(raw_p_text)
        today_stats, today_meals = get_today_summary(user_id), get_today_meals_list(user_id)

        ai_res = process_ai_in_single_call(raw_p_text, today_stats, (target_cal, target_protein), user_msg, last_restaurant=last_restaurant, today_meals=today_meals)
        msg_type = ai_res.get("type")

        if msg_type == "log":
            food, cal, protein, total_cal, total_protein = log_meal_to_supabase(user_id, ai_res)
            summary_flex = build_summary_flex_card(total_cal, target_cal, total_protein, target_protein, profile.get("goal"), last_logged_info={"food": food, "cal": cal, "protein": protein})
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"BiteLogic 紀錄成功：{food}", contents=summary_flex, quick_reply=get_quick_reply(user_id)))
        elif msg_type == "recommendation":
            rec_store = ai_res.get("restaurant")
            if rec_store and rec_store != "null": update_last_restaurant_in_profile(user_id, raw_p_text, rec_store)

            cals, _ = today_stats
            if cals >= target_cal:
                store_display = rec_store if (rec_store and rec_store != "null") else "外食店家"
                over_cal = cals - target_cal
                status_str = f"已超標 {over_cal} kcal" if over_cal > 0 else "已完全額滿"
                reply_text = f"⚠️ 今日熱量額度已達上限囉！（{status_str}）\n\n今天不建議再攝取任何額外熱量。若一定要去【{store_display}】，請僅選擇「無糖茶類、零卡汽水或瓶裝水」，避免影響今日減脂成果！"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text, quick_reply=get_quick_reply(user_id)))
                return

            flex_content = build_flex_card(ai_res)
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"BiteLogic 推薦：{ai_res.get('restaurant', '')}口袋菜單", contents=flex_content, quick_reply=get_quick_reply(user_id)))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=ai_res.get("reply_text", "請輸入想吃的餐廳名稱，例如：麥當勞、7-11、八方雲集"), quick_reply=get_quick_reply(user_id)))

    except Exception as e:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"系統處理失敗，請重試：{str(e)}", quick_reply=get_quick_reply(profile.get("id"))))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
