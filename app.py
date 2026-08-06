import os
import re
import json
import traceback
from urllib.parse import parse_qs, urlencode
from datetime import datetime, timezone, timedelta
from typing import Annotated, List, Literal, Optional, Union
from flask import Flask, request, abort
from pydantic import BaseModel, Field, TypeAdapter
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, FlexSendMessage, QuickReply, QuickReplyButton, MessageAction, PostbackAction
)
from google import genai  # 全新官方 SDK
from supabase import create_client, Client

app = Flask(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 初始化 Google GenAI 新版 Client
client = genai.Client(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TAIWAN_TZ = timezone(timedelta(hours=8))

SYSTEM_PROMPT = "你是 BiteLogic 外食營養師。除了熱量與蛋白質達標，也重視餐盤均衡（蔬菜纖維、避免單一食物疊加）。純文字回答、無粗體、無Emoji、控在 100 字內。不提價格。若提剩餘額度必須完全照抄給定的正確數字。"

GOAL_MAP_TO_DB = {"減脂": "fat_loss", "增肌": "muscle_gain", "增肌減脂": "recomp"}
GOAL_MAP_TO_DISP = {"fat_loss": "減脂", "muscle_gain": "增肌", "recomp": "增肌減脂"}

# ============ 結構化輸出 Schema（判別聯集：每種意圖各自定義必填欄位）============

class RecItem(BaseModel):
    name: str = Field(description="單品名稱")
    cal: int
    protein: int

class LogResult(BaseModel):
    """用戶回報吃了什麼 → 寫入飲食紀錄"""
    type: Literal["log"]
    restaurant: Optional[str] = Field(default=None, description="連鎖店名，非連鎖填 null")
    food_name: str
    calories: float
    protein_g: float
    is_correction: Optional[bool] = Field(default=None, description="用戶在更正上一筆紀錄的數值時為 true，此時 calories/protein_g 填更正後的完整正確值(不是差額)")

class RecommendationResult(BaseModel):
    """用戶想知道某餐廳怎麼點 → 產生口袋菜單。items 為菜單本體，必須列出 1~5 個具體單品。"""
    type: Literal["recommendation"]
    restaurant: str
    title: str = Field(description="10字內主題")
    items: List[RecItem] = Field(min_length=1, max_length=5, description="進店直接點的具體品項清單，絕不可為空")
    warning: str = Field(description="避坑提示；若蛋白質未達標，須在此建議補充方式")
    total_cal: int
    total_protein: int

class ChatResult(BaseModel):
    """一般對話或詢問剩餘額度"""
    type: Literal["chat"]
    reply_text: str

AIResultAdapter = TypeAdapter(
    Annotated[Union[LogResult, RecommendationResult, ChatResult], Field(discriminator="type")]
)

def normalize_ai_result(d):
    """容錯層：模型偶爾會搞混欄位名(如 log 意圖誤用 total_cal/total_protein)。
    在 schema 驗證前把常見的別名搬回正名，減少不必要的失敗。"""
    if not isinstance(d, dict):
        return d
    t = d.get("type")
    if t == "log":
        if "calories" not in d and "total_cal" in d: d["calories"] = d.pop("total_cal")
        if "protein_g" not in d and "total_protein" in d: d["protein_g"] = d.pop("total_protein")
        if "protein_g" not in d and "protein" in d: d["protein_g"] = d.pop("protein")
        if not d.get("food_name"):
            for alias in ("title", "name", "food", "item"):
                if d.get(alias):
                    d["food_name"] = d.pop(alias)
                    break
        if not d.get("food_name") and "calories" in d:
            d["food_name"] = "未知餐點"
    elif t == "recommendation":
        if "total_cal" not in d and "calories" in d: d["total_cal"] = d.pop("calories")
        if "total_protein" not in d and "protein_g" in d: d["total_protein"] = d.pop("protein_g")
    return d

# ================================================================================

def format_num(val):
    try:
        f = float(val)
        return int(f) if f.is_integer() else f
    except Exception:
        return val

def update_last_restaurant(user_id, store_name):
    """last_restaurant 改為 profiles 獨立欄位，不再塞在 raw_profile_text 裡用 regex 進出。"""
    if not store_name or store_name == "null":
        return
    supabase.table("profiles").update({
        "last_restaurant": store_name,
        "last_restaurant_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", user_id).execute()

def get_last_restaurant(profile, max_age_days=3):
    """只沿用近幾天內的餐廳，避免上週吃的店影響今天的「調整」判斷。"""
    store = profile.get("last_restaurant")
    if not store:
        return None
    ts = profile.get("last_restaurant_at")
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - dt > timedelta(days=max_age_days):
                return None
        except Exception:
            pass
    return store

def calculate_metabolic_profile(weight_kg, height_cm, age, gender, goal, activity_level, meal_pattern):
    """回傳 (bmr, tdee, target_cal, target_protein) 完整代謝數據。"""
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

    return int(bmr), int(tdee), target_cal, target_protein

def calculate_precise_targets(weight_kg, height_cm, age, gender, goal, activity_level, meal_pattern):
    _, _, target_cal, target_protein = calculate_metabolic_profile(weight_kg, height_cm, age, gender, goal, activity_level, meal_pattern)
    return target_cal, target_protein

def _kv_row(label, value, sub=None):
    """卡片內的「標籤:值」橫列。sub 為值下方的小字補充(避免長字串在右欄折行)。"""
    right = [{"type": "text", "text": str(value), "size": "sm", "weight": "bold", "color": "#1F2937", "align": "end", "wrap": True}]
    if sub:
        right.append({"type": "text", "text": str(sub), "size": "xxs", "color": "#9CA3AF", "align": "end", "wrap": True})
    return {
        "type": "box", "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#6B7280", "flex": 4, "gravity": "top"},
            {"type": "box", "layout": "vertical", "flex": 5, "contents": right}
        ]
    }

def _section_caption(text):
    return {"type": "text", "text": text, "size": "xs", "color": "#9CA3AF", "weight": "bold", "margin": "md"}

def build_profile_flex_card(profile):
    """個人檔案卡片(含 BMR/TDEE 推導)。"""
    raw = profile.get("raw_profile_text") or ""
    h, w, a, g = profile.get("height_cm"), profile.get("weight_kg"), profile.get("age") or 30, profile.get("gender") or "男"
    goal_disp = GOAL_MAP_TO_DISP.get(profile.get("goal"), profile.get("goal")) or "減脂"

    parts = [p.strip() for p in raw.split("/")]
    act = parts[5] if len(parts) >= 7 else "未設定"
    meal = parts[6] if len(parts) >= 7 else "未設定"

    bmr, tdee, calc_tc, calc_tp = calculate_metabolic_profile(w, h, a, g, profile.get("goal"), raw, raw)
    tc = profile.get("target_calories") or calc_tc
    tp = profile.get("target_protein_g") or calc_tp
    pct = int(round(tc / tdee * 100)) if tdee else 0

    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1F2937", "paddingAll": "lg",
            "contents": [
                {"type": "text", "text": "我的健康檔案", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"目標模式：{goal_disp}", "color": "#9CA3AF", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "lg",
            "contents": [
                _section_caption("基本數據"),
                _kv_row("身高 / 體重", f"{format_num(h)} cm / {format_num(w)} kg"),
                _kv_row("年齡 / 性別", f"{format_num(a)} 歲 / {g}"),
                _kv_row("活動程度", act),
                _kv_row("飲食習慣", meal),
                {"type": "separator", "margin": "md"},
                _section_caption("代謝估算"),
                _kv_row("基礎代謝率 BMR", f"約 {bmr} kcal"),
                _kv_row("總消耗 TDEE", f"約 {tdee} kcal"),
                {"type": "separator", "margin": "md"},
                _section_caption("每日控制目標"),
                _kv_row("總熱量", f"{tc} kcal", sub=f"TDEE 的 {pct}%"),
                _kv_row("蛋白質", f"{tp} g"),
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "提示：輸入「修改檔案」可重新建檔；輸入「體重 104.5」可更新體重並同步目標。", "size": "xs", "color": "#6B7280", "wrap": True, "margin": "md"}
            ]
        }
    }

def _progress_bar_block(label, current, target, unit, bar_color, over_color="#EF4444"):
    pct = min(100, max(0, int((current / target) * 100))) if target > 0 else 0
    return {
        "type": "box", "layout": "vertical",
        "contents": [
            {
                "type": "box", "layout": "horizontal",
                "contents": [
                    {"type": "text", "text": label, "size": "sm", "weight": "bold", "color": "#374151"},
                    {"type": "text", "text": f"{current} / {target} {unit} ({pct}%)", "size": "xs", "align": "end", "color": "#6B7280"}
                ]
            },
            {
                "type": "box", "layout": "vertical", "backgroundColor": "#E5E7EB", "height": "8px", "cornerRadius": "4px", "margin": "sm",
                "contents": [{"type": "box", "layout": "vertical", "backgroundColor": over_color if pct >= 100 else bar_color, "height": "8px", "width": f"{max(3, pct)}%", "cornerRadius": "4px", "contents": []}]
            }
        ]
    }

def build_today_card(meals, cals, protein, target_cal, target_protein, goal, last_logged_info=None, info_label="成功寫入飲食紀錄"):
    """統一的「今日」卡片:今日進度查詢、飲食明細、紀錄成功、更正、刪除後全部共用。
    含逐筆清單 + 進度條;last_logged_info 存在時頂部顯示綠色成功框。"""
    goal_disp = GOAL_MAP_TO_DISP.get(goal, goal) or "健康減脂"
    header_title = "紀錄成功與今日進度" if last_logged_info else "今日進度總覽"

    body_contents = []

    if last_logged_info:
        body_contents.extend([
            {
                "type": "box", "layout": "vertical", "backgroundColor": "#ECFDF5", "cornerRadius": "md", "paddingAll": "md",
                "contents": [
                    {"type": "text", "text": info_label, "size": "xs", "color": "#059669", "weight": "bold"},
                    {"type": "text", "text": f"{last_logged_info.get('food', '')}", "size": "sm", "weight": "bold", "color": "#065F46", "margin": "xs", "wrap": True},
                    {"type": "text", "text": f"{last_logged_info.get('cal', 0)} kcal ｜ {last_logged_info.get('protein', 0)} g 蛋白質", "size": "xs", "color": "#047857", "margin": "xs"}
                ]
            },
            {"type": "separator", "margin": "md"}
        ])

    if meals:
        for idx, m in enumerate(meals, 1):
            body_contents.append({
                "type": "box", "layout": "vertical", "margin": "sm",
                "contents": [
                    {"type": "text", "text": f"{idx}. {m['food_name']}", "size": "sm", "color": "#1F2937", "wrap": True},
                    {"type": "text", "text": f"+{m['calories']} kcal ｜ +{m['protein_g']} g 蛋白質", "size": "xs", "color": "#9CA3AF", "margin": "xs"}
                ]
            })
    else:
        body_contents.append({"type": "text", "text": "今天尚無任何飲食紀錄。", "size": "sm", "color": "#6B7280", "margin": "sm"})

    rem_cal = max(0, target_cal - cals)
    rem_protein = max(0, target_protein - protein)
    footer_line = f"剩餘額度：{rem_cal} kcal ｜ 蛋白質還差：{rem_protein} g" if cals <= target_cal else f"已超出上限 {cals - target_cal} kcal ｜ 蛋白質還差：{rem_protein} g"

    body_contents.extend([
        {"type": "separator", "margin": "lg"},
        _progress_bar_block("熱量攝取", cals, target_cal, "kcal", "#27AE60"),
        _progress_bar_block("蛋白質攝取", protein, target_protein, "g", "#3B82F6"),
        {"type": "text", "text": footer_line, "size": "xs", "color": "#6B7280", "margin": "md", "wrap": True},
        {"type": "separator", "margin": "md"},
        {"type": "text", "text": "提示：輸入「刪除上一筆」可撤銷、「你記少了,是X大卡」可更正最近一筆。", "size": "xs", "color": "#6B7280", "wrap": True}
    ])

    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1F2937", "paddingAll": "lg",
            "contents": [
                {"type": "text", "text": header_title, "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"{get_today_str()} ｜ {goal_disp} ｜ 共 {len(meals)} 筆", "color": "#9CA3AF", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "lg", "contents": body_contents}
    }

def get_quick_reply(user_id=None):
    base_items = [
        QuickReplyButton(action=MessageAction(label="今日卡路里", text="查看今日卡路里")),
        QuickReplyButton(action=MessageAction(label="個人檔案", text="個人檔案"))
    ]
    store_items = []
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
                        store_items.append(QuickReplyButton(action=MessageAction(label=f"{store}推薦", text=f"{store}推薦")))
        except Exception:
            pass
    # 尚無飲食紀錄（如剛建檔完）時給預設起手式，讓用戶點一下就能體驗核心功能；
    # 一旦有紀錄，自動被個人常用店取代
    if not store_items:
        store_items = [
            QuickReplyButton(action=MessageAction(label="7-11推薦", text="7-11推薦")),
            QuickReplyButton(action=MessageAction(label="全家推薦", text="全家推薦"))
        ]
    return QuickReply(items=store_items + base_items)

def parse_basic_profile(raw_text, strict=False):
    """從訊息解析身高/體重/年齡。
    strict=True（已建檔用戶）：只接受「數字/數字/數字」的明確建檔格式，
    避免一般訊息裡恰好出現的數字（如「7-11推薦」「御飯糰250卡」）誤觸重新建檔。"""
    if strict and not re.search(r'\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?', raw_text):
        return None

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
        # 兩數字也必須通過合理範圍檢查（修復「7-11」被當成 7cm/11kg 的 bug）
        if not (50 <= nums[0] <= 250 and 20 <= nums[1] <= 300):
            return None
        parsed["height_cm"], parsed["weight_kg"], parsed["age"] = format_num(nums[0]), format_num(nums[1]), 30

    parsed["gender"] = "女" if "女" in raw_text else "男"
    return parsed if (parsed.get("height_cm") and parsed.get("weight_kg")) else None

def get_or_create_daily_log_id(user_id):
    today = get_today_str()
    log_res = supabase.table("daily_logs").select("id").eq("user_id", user_id).eq("log_date", today).execute()
    if log_res.data:
        return log_res.data[0]["id"]
    new_log = supabase.table("daily_logs").insert({"user_id": user_id, "log_date": today}).execute()
    return new_log.data[0]["id"]

def get_today_meals_list(user_id):
    today = get_today_str()
    log_res = supabase.table("daily_logs").select("id").eq("user_id", user_id).eq("log_date", today).execute()
    if not log_res.data: return []
    meals_res = supabase.table("meal_items").select("id, food_name, calories, protein_g").eq("daily_log_id", log_res.data[0]["id"]).order("created_at", desc=False).execute()
    return meals_res.data if meals_res.data else []

def get_today_summary(user_id):
    """總計改為讀取時從 meal_items 加總（單一資料來源），不再維護 daily_logs 上的累計欄位，
    避免「讀取→加減→寫回」的併發覆寫問題。"""
    meals = get_today_meals_list(user_id)
    return (
        sum(int(m.get("calories") or 0) for m in meals),
        sum(int(m.get("protein_g") or 0) for m in meals),
    )

def log_weight(user_id, weight, profile):
    """記錄今日體重(同日覆蓋),同步更新 profile 體重並重算每日目標。
    回傳確認訊息文字。"""
    today = get_today_str()

    # 前一筆(今天以前)用於顯示變化
    prev_res = supabase.table("weight_logs").select("weight_kg, log_date").eq("user_id", user_id).lt("log_date", today).order("log_date", desc=True).limit(1).execute()
    prev = prev_res.data[0] if prev_res.data else None

    supabase.table("weight_logs").upsert(
        {"user_id": user_id, "log_date": today, "weight_kg": weight},
        on_conflict="user_id,log_date"
    ).execute()

    # 同步 profile 體重、重算目標(蛋白質目標依體重浮動,不同步會漂移)
    raw_p_text = profile.get("raw_profile_text") or ""
    new_raw = re.sub(r'體重\d+(?:\.\d+)?kg', f'體重{format_num(weight)}kg', raw_p_text) if '體重' in raw_p_text else raw_p_text
    new_target_cal, new_target_protein = calculate_precise_targets(
        weight, profile.get("height_cm"), profile.get("age", 30), profile.get("gender"),
        profile.get("goal"), raw_p_text, raw_p_text
    )
    supabase.table("profiles").update({
        "weight_kg": weight, "raw_profile_text": new_raw,
        "target_calories": new_target_cal, "target_protein_g": new_target_protein,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", user_id).execute()

    lines = [f"已記錄今日體重:{format_num(weight)} kg"]
    if prev:
        diff = round(float(weight) - float(prev["weight_kg"]), 1)
        arrow = "↓" if diff < 0 else ("↑" if diff > 0 else "→")
        lines.append(f"與上次({prev['log_date']})相比:{arrow} {abs(diff)} kg")
    lines.append(f"每日目標已同步更新:{new_target_cal} kcal / 蛋白質 {new_target_protein} g")
    lines.append("\n提示:輸入「體重紀錄」可查看近期趨勢。")
    return "\n".join(lines)

def get_weight_history_text(user_id, limit=8):
    res = supabase.table("weight_logs").select("log_date, weight_kg").eq("user_id", user_id).order("log_date", desc=True).limit(limit).execute()
    if not res.data:
        return "尚無體重紀錄。輸入「體重 104.5」即可記錄第一筆。"
    rows = list(reversed(res.data))
    lines = ["近期體重紀錄:"]
    prev_w = None
    for r in rows:
        w = float(r["weight_kg"])
        mark = ""
        if prev_w is not None:
            d = round(w - prev_w, 1)
            mark = f"({'↓' if d < 0 else '↑'}{abs(d)})" if d != 0 else "(→)"
        lines.append(f"{r['log_date']}:{format_num(w)} kg {mark}")
        prev_w = w
    total = round(float(rows[-1]["weight_kg"]) - float(rows[0]["weight_kg"]), 1)
    if len(rows) >= 2 and total != 0:
        lines.append(f"\n區間變化:{'↓' if total < 0 else '↑'} {abs(total)} kg")
    return "\n".join(lines)

def get_menu_context(user_msg, last_restaurant=None):
    """查自建菜單庫 store_menus：若用戶訊息（或沿用的上次餐廳）命中某家店，
    回傳 (店名, 權威菜單字串) 注入 prompt，讓 AI 只負責「組合」、不負責「記憶」。
    表為空或查詢失敗時回 (None, None)，走原本純 AI 推薦路徑。"""
    try:
        res = supabase.table("store_menus").select("store_name, aliases, item_name, calories, protein_g, notes").eq("is_active", True).execute()
        if not res.data:
            return None, None

        stores = {}
        for r in res.data:
            entry = stores.setdefault(r["store_name"], {"aliases": r.get("aliases") or "", "items": []})
            entry["items"].append(r)

        msg_l = user_msg.lower()
        last_l = (last_restaurant or "").lower()
        for store, info in stores.items():
            names = [store] + [a.strip() for a in info["aliases"].split(",") if a.strip()]
            if any(n.lower() and (n.lower() in msg_l or n.lower() == last_l) for n in names):
                lines = []
                for it in info["items"]:
                    note = f"({it['notes']})" if it.get("notes") else ""
                    lines.append(f"{it['item_name']}:{it['calories']}kcal/{it['protein_g']}g蛋白{note}")
                return store, "；".join(lines)
    except Exception:
        # 表不存在或查詢失敗都不影響主流程
        pass
    return None, None

def process_ai_in_single_call(profile_str, today_stats, target_stats, user_msg, last_restaurant=None, today_meals=None, menu_context=None, avoid_items=None, explicit_store=None, expected_log=False):
    cal, protein = today_stats
    target_cal, target_protein = target_stats

    # 用戶明確指定店家時，上次餐廳不可干擾判斷；回報吃了什麼時同理
    if explicit_store or expected_log:
        last_restaurant = None

    rem_cal, rem_protein = max(0, target_cal - cal), max(0, target_protein - protein)
    logged_count = len(today_meals) if today_meals else 0
    total_planned_meals = 2 if ("168" in profile_str or "兩餐" in profile_str) else 3
    remaining_meals = max(1, total_planned_meals - logged_count)

    meal_cal_cap = int(rem_cal / remaining_meals)
    meal_protein_cap = int(rem_protein / remaining_meals)

    menu_store, menu_str = menu_context if menu_context else (None, None)
    menu_section = ""
    if menu_str:
        menu_section = f"\n    【{menu_store} 官方菜單資料】(熱量/蛋白質以此為準，推薦品項只能從中選取，數字照抄不可自行估算):\n    {menu_str}\n"

    avoid_section = ""
    if avoid_items:
        avoid_section = f"\n    用戶已拒絕的組合:{('、'.join(avoid_items))[:200]}。必須推薦與其明顯不同的新組合(至少更換主食方向)，不可只微調份量。\n"

    store_section = ""
    if explicit_store:
        store_section = f"\n    【用戶明確指定餐廳:{explicit_store}】restaurant 欄位與所有品項必須屬於此店，嚴禁使用任何其他店家。\n"
    if expected_log:
        store_section += "\n    【此訊息是飲食紀錄回報】用戶在告知已經吃了什麼，必須輸出 type=log，絕不可輸出推薦。\n"

    prompt = f"""
    {SYSTEM_PROMPT}

    檔案:{profile_str}|上次餐廳:{last_restaurant or '無'}
    今日已攝取:{cal}/{target_cal}kcal,剩餘:{rem_cal}kcal|蛋白質還差:{rem_protein}g|剩餘餐數:{remaining_meals}
    用戶說:"{user_msg}"
{menu_section}{avoid_section}{store_section}
    判斷意圖，依 schema 輸出對應欄位:
    A.飲食紀錄(type=log): 訊息以「我吃了/剛吃了/喝了」等回報語氣開頭者必為此類。欄位名必須是 restaurant(連鎖店名,非連鎖填null)、food_name、calories、protein_g(不可用 total_cal/total_protein)。
      若用戶是在更正剛剛那筆紀錄的數值(如「你記少了」「其實是228大卡」)，同樣輸出 type=log 並設 is_correction=true，calories/protein_g 填更正後的完整正確值(不是差額)，food_name 沿用原品名。
    B.餐廳推薦/調整(type=recommendation): 設計單餐組合。僅在「用戶訊息未提及任何店名」且為調整語氣(如:換一個、太多了)時，才沿用上次餐廳{last_restaurant or ''};用戶訊息中提到的店名永遠優先。
      硬性規則(份量最優先): 用戶一天只吃 {total_planned_meals} 餐、今天還剩 {remaining_meals} 餐要分完 {rem_cal} kcal，因此**這一餐必須吃到 {int(meal_cal_cap*0.8)}~{meal_cal_cap} kcal**。給出明顯低於此區間的組合是嚴重錯誤(會害用戶下一餐被迫暴食)，寧可份量加大、加點主食或加倍肉量，也不可湊出一份 400~600 kcal 的輕食。total_protein 目標 {meal_protein_cap}g、至少 {int(meal_protein_cap*0.8)}g，優先組合高蛋白品項(肉類加量/加蛋/豆腐/無糖豆漿)。
      若對品項的熱量/蛋白質數字不確定(特別是超商鮮食、新品、台灣分店限定品項)，先用 google_search 查官方或近期資料再作答，不可憑印象編造。
      餐盤結構(同為硬性): 組合須包含「蛋白質主食 + 蔬菜/纖維配菜」，該店有蔬菜、沙拉、湯品類就必須納入至少一項；禁止用單一品項的極端規格(如三倍肉)硬衝蛋白質——寧可蛋白質停在下限、也要保留蔬菜的熱量空間，缺口在 warning 建議店外補足。若該店確實無任何蔬菜類品項，才允許純主食組合，且 warning 須提醒本餐缺蔬菜、建議下一餐或店外補充。
      丼飯/牛丼類店家常有「增肉減飯」「肉大碗」「肉量加倍」等選項，減脂與增肌目標應優先納入這類選項。
      若該店品項組合實在無法達到蛋白質下限，取該店可達的最高蛋白組合，並在 warning 具體建議店外補充方式(如無糖豆漿、茶葉蛋、乳清)。
      填 restaurant、title(10字內主題)、items(每項含name/cal/protein)、warning、total_cal、total_protein。
    C.一般對話/問額度(type=chat): 填 reply_text，精簡回覆(熱量剩{rem_cal}kcal,蛋白質差{rem_protein}g)。
    """
    # 查表命中 -> 用自建權威資料,不需搜尋;未命中 -> 開 Google Search 讓模型查即時資料,而非憑記憶猜
    use_search = menu_str is None

    def _call(extra_note="", with_search=use_search):
        kwargs = dict(
            model="gemini-3.5-flash-lite",
            input=prompt + extra_note,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": AIResultAdapter.json_schema()
            },
            store=False  # prompt 內含用戶身體數據，不留存於 Google 端
        )
        if with_search:
            kwargs["tools"] = [{"type": "google_search"}]
        interaction = client.interactions.create(**kwargs)
        return getattr(interaction, "output_text", None) or (interaction.text if hasattr(interaction, 'text') else str(interaction))

    try:
        try:
            res_text = _call()
        except Exception:
            if use_search:
                # 搜尋工具不可用(如未開計費)或與 structured output 併用失敗時，降級為純模型模式
                print("⚠️ 含搜尋的呼叫失敗，降級為無搜尋重試：")
                print(traceback.format_exc())
                res_text = _call(with_search=False)
            else:
                raise
        if not res_text:
            return {"type": "chat", "reply_text": "AI 暫時無法回應，請重試。"}

        def _parse(txt):
            data = json.loads(txt)
            try:
                return AIResultAdapter.validate_python(data).model_dump()
            except Exception:
                # 欄位名容錯(如 log 誤用 total_cal)後再驗一次
                return AIResultAdapter.validate_python(normalize_ai_result(data)).model_dump()

        # 防線1：schema 驗證失敗 -> 帶欄位名糾正指令重試一次
        try:
            result = _parse(res_text)
        except Exception:
            print("⚠️ schema 驗證失敗，帶糾正指令重試一次")
            res_text = _call("\n注意：維持你原本判斷的意圖類型不變，只修正欄位名。type=log 用 food_name/calories/protein_g；type=recommendation 用 items/total_cal/total_protein；type=chat 用 reply_text。")
            result = _parse(res_text)

        # 防線4：明確的紀錄語氣卻回了推薦 -> 糾正重試一次
        if expected_log and result["type"] == "recommendation":
            print("⚠️ 紀錄語氣卻回推薦，糾正重試")
            res_text = _call("\n糾正：用戶是在回報已經吃了的食物，必須輸出 type=log(填 food_name/calories/protein_g)，不是推薦。")
            result = _parse(res_text)
            if result["type"] == "recommendation":
                return {"type": "chat", "reply_text": "抱歉沒聽懂，請再描述一次你吃了什麼(例如:我吃了7-11茶葉蛋)。"}

        # 防線2：若判為推薦但菜單仍為空，帶著糾正指令重試一次
        if result["type"] == "recommendation" and not result.get("items"):
            print("⚠️ recommendation 缺 items，重試一次")
            res_text = _call("\n注意：items 是菜單本體，必須列出 1~5 個具體單品（含名稱/熱量/蛋白質），不可為空。")
            result = _parse(res_text)
            if result["type"] == "recommendation" and not result.get("items"):
                return {"type": "chat", "reply_text": f"這家店的菜單資訊暫時生成失敗，請再傳一次「{user_msg}」試試。"}

        # 防線3：用戶明確指定店家，但模型推薦了別家 -> 糾正重試一次
        if explicit_store and result["type"] == "recommendation":
            rec_store = (result.get("restaurant") or "").lower()
            es = explicit_store.lower()
            if es not in rec_store and rec_store not in es:
                print(f"⚠️ 指定 {explicit_store} 但推薦了 {result.get('restaurant')}，糾正重試")
                res_text = _call(f"\n嚴重錯誤糾正：用戶指定的是【{explicit_store}】，你剛推薦了別家。restaurant 與所有品項必須改為 {explicit_store} 的。")
                result = _parse(res_text)
                rec_store = (result.get("restaurant") or "").lower() if result["type"] == "recommendation" else ""
                if result["type"] == "recommendation" and es not in rec_store and rec_store not in es:
                    return {"type": "chat", "reply_text": f"抱歉，剛剛推薦錯店了。請再傳一次「{explicit_store}推薦」。"}

        # 防線5：份量檢查。prompt 的區間規則只是建議，模型常給出遠低於單餐目標的組合
        # （如兩餐制卻只推 464/1030 kcal，等於逼用戶下一餐吃 1600）。此處由代碼驗證並要求補足。
        if result["type"] == "recommendation" and result.get("items"):
            def _sum_items(res):
                its = res.get("items") or []
                return (
                    sum(int(i.get("cal") or 0) for i in its if isinstance(i, dict)),
                    sum(int(i.get("protein") or 0) for i in its if isinstance(i, dict)),
                )

            floor_cal = int(meal_cal_cap * 0.8)
            sum_cal, sum_pro = _sum_items(result)
            if sum_cal < floor_cal:
                print(f"⚠️ 推薦份量不足({sum_cal} < {floor_cal})，要求補足重試")
                res_text = _call(
                    f"\n份量不足糾正：你上一組只有 {sum_cal} kcal，但用戶這餐的目標是 {meal_cal_cap} kcal"
                    f"（一天只吃 {total_planned_meals} 餐，剩 {remaining_meals} 餐要分完 {rem_cal} kcal）。"
                    f"請加大份量或增加品項，讓 items 的熱量總和落在 {floor_cal}~{meal_cal_cap} kcal 之間，"
                    f"蛋白質總和至少 {int(meal_protein_cap*0.8)}g。不可再給明顯低於目標的組合。"
                )
                retried = _parse(res_text)
                if retried["type"] == "recommendation" and retried.get("items"):
                    r_cal, _ = _sum_items(retried)
                    # 取較接近目標的那一組
                    if abs(r_cal - meal_cal_cap) < abs(sum_cal - meal_cal_cap):
                        result = retried

            # 卡片與紀錄一律以品項實際加總為準，避免 AI 自報的 total 與清單對不上
            sum_cal, sum_pro = _sum_items(result)
            if sum_cal > 0:
                result["total_cal"], result["total_protein"] = sum_cal, sum_pro

        # budget 字串由代碼決定性生成（含蛋白質目標），不交給 AI 自由發揮
        if result["type"] == "recommendation":
            result["budget"] = f"單餐目標：{meal_cal_cap} kcal ｜ 蛋白質 {meal_protein_cap} g"

        return result

    except Exception:
        print("❌ Interactions API 完整報錯細節：")
        print(traceback.format_exc())
        return {"type": "chat", "reply_text": "AI 連線失敗，請稍後重試。"}

def build_flex_card(data, rec_id):
    """postback 只帶 rec_id，餐點內容落地在 pending_recommendations，
    避免中文 urlencode 後撞上 LINE postback data 300 字元上限，也避免店名/品名含 & = 時 parse_qs 解壞。"""
    restaurant = data.get("restaurant") or "外食推薦"
    title = data.get("title") or "精準口袋菜單"
    budget = data.get("budget") or "符合個人每日熱量控制"
    items = data.get("items") or []
    warning = data.get("warning") or "注意適量攝取"
    total_cal = data.get("total_cal") or 500
    total_protein = data.get("total_protein") or 0
    combo_summary = f"本組合合計：{total_cal} kcal ｜ 蛋白質 {total_protein} g"

    items_contents = []
    for item in items:
        if isinstance(item, dict):
            name, c, p = item.get("name", "餐點"), item.get("cal", 0), item.get("protein", 0)
            items_contents.append({"type": "text", "text": f"• {name} (約 {c} kcal / {p}g 蛋白質)", "size": "sm", "color": "#555555", "margin": "xs", "wrap": True})
        elif isinstance(item, str):
            items_contents.append({"type": "text", "text": f"• {item}" if not item.startswith("•") else item, "size": "sm", "color": "#555555", "margin": "xs", "wrap": True})

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
                {"type": "text", "text": combo_summary, "weight": "bold", "size": "sm", "color": "#1F2937", "margin": "md"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"地雷與補充提醒：{warning}", "size": "xs", "color": "#E74C3C", "margin": "md", "wrap": True}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "button", "style": "primary", "color": "#27AE60",
                    "action": {
                        "type": "postback",
                        "label": f"一鍵紀錄這餐 ({total_cal} kcal)",
                        "data": f"action=log_meal&rec_id={rec_id}",
                        "displayText": f"我決定吃【{restaurant}】這套組合！"
                    }
                },
                {
                    "type": "button", "style": "secondary", "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": "不喜歡？換一組推薦",
                        "data": f"action=reroll&rec_id={rec_id}",
                        "displayText": f"換一組【{restaurant}】的推薦"
                    }
                }
            ]
        }
    }

def save_pending_recommendation(user_id, ai_res):
    """推薦內容落地，回傳 rec_id 供 postback 使用。
    順手清掉 3 天前的舊資料（懶清理）：不依賴 pg_cron，表的大小自然收斂在最近 3 天的推薦量。"""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        supabase.table("pending_recommendations").delete().lt("created_at", cutoff).execute()
    except Exception:
        pass  # 清理失敗不影響主流程
    res = supabase.table("pending_recommendations").insert({
        "user_id": user_id,
        "payload": json.dumps(ai_res, ensure_ascii=False)
    }).execute()
    return res.data[0]["id"]

def get_pending_recommendation(rec_id):
    res = supabase.table("pending_recommendations").select("user_id, payload").eq("id", rec_id).execute()
    if not res.data:
        return None
    row = res.data[0]
    payload = json.loads(row["payload"])
    payload["_user_id"] = row["user_id"]
    return payload

def get_today_str(): return datetime.now(TAIWAN_TZ).strftime("%Y-%m-%d")

def get_user_profile(line_user_id):
    res = supabase.table("profiles").select(
        "id, raw_profile_text, height_cm, weight_kg, age, gender, goal, target_calories, target_protein_g, last_restaurant, last_restaurant_at"
    ).eq("line_user_id", line_user_id).execute()
    return res.data[0] if res.data else None

def delete_last_meal(user_id):
    meals = get_today_meals_list(user_id)
    if not meals: return "今日尚無任何紀錄可刪除。"

    last_meal = meals[-1]
    supabase.table("meal_items").delete().eq("id", last_meal["id"]).execute()
    return f"已成功刪除最近一筆紀錄：【{last_meal['food_name']}】(-{last_meal['calories']} kcal)"

def update_last_meal(user_id, intent_data):
    """更正今日最後一筆紀錄的數值。回傳更正後的資料,無可更正時回 None。"""
    meals = get_today_meals_list(user_id)
    if not meals:
        return None
    last = meals[-1]
    upd = {}
    # 只接受正值:用戶僅更正單一數值時,模型可能對另一欄填 0,不可把原本正確的值覆寫成 0
    cal_v = intent_data.get("calories")
    pro_v = intent_data.get("protein_g")
    if cal_v is not None and float(cal_v) > 0:
        upd["calories"] = int(round(float(cal_v)))
    if pro_v is not None and float(pro_v) > 0:
        upd["protein_g"] = int(round(float(pro_v)))
    if not upd:
        return None
    supabase.table("meal_items").update(upd).eq("id", last["id"]).execute()
    return {
        "food_name": last["food_name"],
        "calories": upd.get("calories", last["calories"]),
        "protein_g": upd.get("protein_g", last["protein_g"]),
    }

def log_meal_to_supabase(user_id, intent_data):
    cals = int(round(float(intent_data.get("calories") or 0)))
    protein = int(round(float(intent_data.get("protein_g") or 0)))
    restaurant, food_name_raw = intent_data.get("restaurant"), intent_data.get("food_name") or "未知餐點"
    full_food_name = f"【{restaurant}】{food_name_raw}" if (restaurant and restaurant != "null") else food_name_raw

    daily_log_id = get_or_create_daily_log_id(user_id)
    supabase.table("meal_items").insert({"daily_log_id": daily_log_id, "meal_type": "snack", "food_name": full_food_name, "calories": cals, "protein_g": protein}).execute()

    new_cals, new_protein = get_today_summary(user_id)
    return full_food_name, cals, protein, new_cals, new_protein

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
        rec_id = data.get("rec_id", [""])[0]
        rec = get_pending_recommendation(rec_id) if rec_id else None
        profile = get_user_profile(line_user_id)

        # 所有權檢查:防止群組情境下按到別人的卡片
        if not rec or not profile or rec.get("_user_id") != profile["id"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="這張推薦卡片已過期，請重新輸入餐廳名稱取得新菜單。", quick_reply=get_quick_reply(profile["id"] if profile else None)))
            return

        items = rec.get("items") or []
        item_names = [i.get("name", "餐點") if isinstance(i, dict) else str(i) for i in items]
        log_title = "、".join(item_names) if item_names else (rec.get("title") or "精準餐點")

        intent_data = {
            "restaurant": rec.get("restaurant"),
            "food_name": log_title,
            "calories": rec.get("total_cal") or 0,
            "protein_g": rec.get("total_protein") or 0
        }
        food, c, p, total_c, total_p = log_meal_to_supabase(profile["id"], intent_data)

        # 用後即刪：同一張卡片再按一次會走「已過期」路徑，防止手滑重複記錄同一餐
        try:
            supabase.table("pending_recommendations").delete().eq("id", rec_id).execute()
        except Exception:
            pass

        target_cal, target_protein = profile.get("target_calories") or 2000, profile.get("target_protein_g") or 150

        meals_now = get_today_meals_list(profile["id"])
        summary_flex = build_today_card(meals_now, cals=total_c, protein=total_p, target_cal=target_cal, target_protein=target_protein, goal=profile.get("goal"), last_logged_info={"food": food, "cal": c, "protein": p})
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"BiteLogic 紀錄成功：{food}", contents=summary_flex, quick_reply=get_quick_reply(profile["id"])))

    elif action == "reroll":
        rec_id = data.get("rec_id", [""])[0]
        rec = get_pending_recommendation(rec_id) if rec_id else None
        profile = get_user_profile(line_user_id)

        if not rec or not profile or rec.get("_user_id") != profile["id"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="這張推薦卡片已過期，請重新輸入餐廳名稱取得新菜單。", quick_reply=get_quick_reply(profile["id"] if profile else None)))
            return

        user_id = profile["id"]
        target_cal, target_protein = profile.get("target_calories"), profile.get("target_protein_g")
        raw_p_text = profile.get("raw_profile_text", "")
        if not target_cal or not target_protein:
            target_cal, target_protein = calculate_precise_targets(profile.get("weight_kg"), profile.get("height_cm"), profile.get("age", 30), profile.get("gender"), profile.get("goal"), raw_p_text, raw_p_text)

        restaurant = rec.get("restaurant") or "外食"
        prev_items = [i.get("name", "") if isinstance(i, dict) else str(i) for i in (rec.get("items") or [])]
        prev_items = [n for n in prev_items if n]

        synthetic_msg = f"{restaurant}推薦"
        menu_context = get_menu_context(synthetic_msg, restaurant)
        today_meals = get_today_meals_list(user_id)
        today_stats = (
            sum(int(m.get("calories") or 0) for m in today_meals),
            sum(int(m.get("protein_g") or 0) for m in today_meals),
        )

        # 額度已滿時不再產生新推薦(與主流程一致)
        if today_stats[0] >= target_cal:
            over_cal = today_stats[0] - target_cal
            status_str = f"已超標 {over_cal} kcal" if over_cal > 0 else "已完全額滿"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 今日熱量額度已達上限囉！（{status_str}）\n\n今天不建議再攝取任何額外熱量，明天再來看新推薦吧！", quick_reply=get_quick_reply(user_id)))
            return

        ai_res = process_ai_in_single_call(raw_p_text, today_stats, (target_cal, target_protein), synthetic_msg, last_restaurant=restaurant, today_meals=today_meals, menu_context=menu_context, avoid_items=prev_items, explicit_store=restaurant)

        if ai_res.get("type") == "recommendation":
            new_rec_id = save_pending_recommendation(user_id, ai_res)
            flex_content = build_flex_card(ai_res, new_rec_id)
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"BiteLogic 新推薦：{ai_res.get('restaurant', '')}口袋菜單", contents=flex_content, quick_reply=get_quick_reply(user_id)))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=ai_res.get("reply_text") or "重新推薦失敗，請再試一次。", quick_reply=get_quick_reply(user_id)))

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

        # 建檔體重即為減脂起點，同步寫入 weight_logs（同日重複建檔以最新值覆蓋）
        if new_profile:
            try:
                supabase.table("weight_logs").upsert(
                    {"user_id": new_profile["id"], "log_date": get_today_str(), "weight_kg": format_num(w)},
                    on_conflict="user_id,log_date"
                ).execute()
            except Exception:
                print("⚠️ 建檔體重寫入 weight_logs 失敗：")
                print(traceback.format_exc())

        welcome_text = "【專屬健康檔案建檔成功】\n\n提示：點下方按鈕試試看，或直接輸入任何想吃的餐廳！"
        if new_profile:
            line_bot_api.reply_message(event.reply_token, [
                TextSendMessage(text=welcome_text),
                FlexSendMessage(alt_text="我的健康檔案", contents=build_profile_flex_card(new_profile), quick_reply=get_quick_reply(new_profile["id"]))
            ])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=welcome_text, quick_reply=get_quick_reply(None)))

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    line_user_id = event.source.user_id
    user_msg = event.message.text.strip()
    profile = get_user_profile(line_user_id)

    if user_msg in ["修改檔案", "重新建檔"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="【重新建立專屬健康檔案】\n\n請直接回覆基本數據：\n【身高 / 體重 / 年齡 / 性別】\n\n範例：173 / 85 / 30 / 男"))
        return

    basic_profile = parse_basic_profile(user_msg, strict=bool(profile))

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

        # 個人檔案指令:顯示完整檔案卡片(含 BMR/TDEE 推導)
        if user_msg in ["個人檔案", "我的檔案", "查看檔案", "查看個人檔案"]:
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="我的健康檔案", contents=build_profile_flex_card(profile), quick_reply=get_quick_reply(user_id)))
            return

        # 體重指令:查詢趨勢
        if user_msg in ["體重紀錄", "體重記錄", "體重趨勢"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=get_weight_history_text(user_id), quick_reply=get_quick_reply(user_id)))
            return

        # 體重指令:記錄(限定「體重 104.5」這類完整格式,避免誤吞一般對話)
        w_match = re.match(r'^體重\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:kg|公斤)?$', user_msg, re.IGNORECASE)
        if w_match:
            w_val = float(w_match.group(1))
            if 20 <= w_val <= 300:
                reply_text = log_weight(user_id, w_val, profile)
            else:
                reply_text = "這個體重數字看起來不太對,請確認後再輸入一次(範例:體重 104.5)。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text, quick_reply=get_quick_reply(user_id)))
            return

        if user_msg == "查看今日卡路里" or any(k in user_msg for k in ["今天吃了啥", "今天吃了什麼", "今天吃了哪些", "吃了啥", "吃了什麼", "飲食紀錄", "紀錄明細"]):
            meals = get_today_meals_list(user_id)
            cals, protein = (
                sum(int(m.get("calories") or 0) for m in meals),
                sum(int(m.get("protein_g") or 0) for m in meals),
            )
            today_flex = build_today_card(meals, cals=cals, protein=protein, target_cal=target_cal, target_protein=target_protein, goal=profile.get("goal"))
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"今日進度({len(meals)}筆)", contents=today_flex, quick_reply=get_quick_reply(user_id)))
            return

        if user_msg in ["刪除上一筆", "刪除紀錄"]:
            del_msg = delete_last_meal(user_id)
            meals = get_today_meals_list(user_id)
            cals, protein = (
                sum(int(m.get("calories") or 0) for m in meals),
                sum(int(m.get("protein_g") or 0) for m in meals),
            )
            summary_flex = build_today_card(meals, cals=cals, protein=protein, target_cal=target_cal, target_protein=target_protein, goal=profile.get("goal"))
            line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=del_msg), FlexSendMessage(alt_text="今日進度", contents=summary_flex, quick_reply=get_quick_reply(user_id))])
            return

        last_restaurant = get_last_restaurant(profile)

        # 「X推薦」格式 = 用戶明確指定店家(如 Quick Reply 按鈕),推薦必須鎖定該店
        explicit_store = None
        es_match = re.match(r'^(.{1,15}?)(?:的)?推薦$', user_msg)
        if es_match:
            candidate = es_match.group(1).strip()
            if candidate and not any(w in candidate for w in ["什麼", "其他", "別的", "怎麼", "如何", "換", "再來", "還有"]):
                explicit_store = candidate

        # 「我吃了X」等回報語氣、以及「記少了/其實是X大卡」等更正語氣 = 紀錄類意圖,不可被判成推薦
        expected_log = bool(
            re.match(r'^\s*(我|本人)?\s*(今天|早上|中午|晚上|早餐|午餐|晚餐|宵夜)?\s*(剛剛?|已經)?\s*(吃|喝)了', user_msg)
            or re.search(r'(記少|記多|記錯|寫錯|算錯|更正|修正|其實(是|有|才)|應該是\s*\d)', user_msg)
        )

        menu_context = get_menu_context(user_msg, last_restaurant)
        today_meals = get_today_meals_list(user_id)
        today_stats = (
            sum(int(m.get("calories") or 0) for m in today_meals),
            sum(int(m.get("protein_g") or 0) for m in today_meals),
        )

        ai_res = process_ai_in_single_call(raw_p_text, today_stats, (target_cal, target_protein), user_msg, last_restaurant=last_restaurant, today_meals=today_meals, menu_context=menu_context, explicit_store=explicit_store, expected_log=expected_log)
        msg_type = ai_res.get("type")

        if msg_type == "log":
            if ai_res.get("is_correction"):
                corrected = update_last_meal(user_id, ai_res)
                if corrected:
                    meals_now = get_today_meals_list(user_id)
                    total_cal_now, total_protein_now = (
                        sum(int(m.get("calories") or 0) for m in meals_now),
                        sum(int(m.get("protein_g") or 0) for m in meals_now),
                    )
                    summary_flex = build_today_card(meals_now, cals=total_cal_now, protein=total_protein_now, target_cal=target_cal, target_protein=target_protein, goal=profile.get("goal"), last_logged_info={"food": corrected["food_name"], "cal": corrected["calories"], "protein": corrected["protein_g"]}, info_label="已更正最近一筆紀錄")
                    line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="更正成功與今日進度", contents=summary_flex, quick_reply=get_quick_reply(user_id)))
                    return
                # 今日無紀錄可更正 -> 往下當成新紀錄寫入
            food, cal, protein, total_cal_now, total_protein_now = log_meal_to_supabase(user_id, ai_res)
            meals_now = get_today_meals_list(user_id)
            summary_flex = build_today_card(meals_now, cals=total_cal_now, protein=total_protein_now, target_cal=target_cal, target_protein=target_protein, goal=profile.get("goal"), last_logged_info={"food": food, "cal": cal, "protein": protein})
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"BiteLogic 紀錄成功：{food}", contents=summary_flex, quick_reply=get_quick_reply(user_id)))
        elif msg_type == "recommendation":
            rec_store = ai_res.get("restaurant")
            if rec_store and rec_store != "null": update_last_restaurant(user_id, rec_store)

            cals, _ = today_stats
            if cals >= target_cal:
                store_display = rec_store if (rec_store and rec_store != "null") else "外食店家"
                over_cal = cals - target_cal
                status_str = f"已超標 {over_cal} kcal" if over_cal > 0 else "已完全額滿"
                reply_text = f"⚠️ 今日熱量額度已達上限囉！（{status_str}）\n\n今天不建議再攝取任何額外熱量。若一定要去【{store_display}】，請僅選擇「無糖茶類、零卡汽水或瓶裝水」，避免影響今日減脂成果！"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text, quick_reply=get_quick_reply(user_id)))
                return

            rec_id = save_pending_recommendation(user_id, ai_res)
            flex_content = build_flex_card(ai_res, rec_id)
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"BiteLogic 推薦：{ai_res.get('restaurant', '')}口袋菜單", contents=flex_content, quick_reply=get_quick_reply(user_id)))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=ai_res.get("reply_text") or "請輸入想吃的餐廳名稱，例如：麥當勞、7-11、八方雲集", quick_reply=get_quick_reply(user_id)))

    except Exception:
        print("❌ 處理訊息失敗系統 Log：")
        print(traceback.format_exc())
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="系統處理失敗，請稍後重試。", quick_reply=get_quick_reply(profile.get("id") if profile else None)))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
