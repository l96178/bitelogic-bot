"""近 7 天總結的純函式測試。

不連資料庫:所有測試只碰「拿到資料之後」的那一層(summarize_week_days、
_week_chart_block、build_week_flex_card、_progress_bar_block)。

app.py 在 import 時就會建立 LINE / Gemini / Supabase 客戶端,所以這裡先塞
假的環境變數。三個客戶端的建構式都不連網,只要有值就建得起來。

執行:.venv/bin/python tests/test_week_summary.py
"""
import os
import sys

for _k, _v in {
    "GEMINI_API_KEY": "test",
    "LINE_CHANNEL_ACCESS_TOKEN": "test",
    "LINE_CHANNEL_SECRET": "test",
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_KEY": "test",
}.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


# ---------- helpers ----------

def iter_texts(node):
    """走訪 Flex 樹,吐出所有 type=text 的節點。"""
    if isinstance(node, dict):
        if node.get("type") == "text":
            yield node
        for v in node.values():
            yield from iter_texts(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_texts(v)


def find_text(node, needle):
    """回傳第一個含有 needle 的 text 字串,找不到回 None。"""
    for t in iter_texts(node):
        if needle in str(t.get("text", "")):
            return t["text"]
    return None


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---------- _progress_bar_block ----------

@case
def test_progress_bar_shows_real_pct_when_over():
    """超標時顯示真實百分比,不是被夾住的 100%。"""
    block = app._progress_bar_block("熱量攝取", 2320, 2000, "kcal", "#27AE60")
    label = find_text(block, "2320 / 2000")
    assert label is not None, "找不到進度條文字"
    assert "(116%)" in label, label


@case
def test_progress_bar_width_capped_at_100():
    """條寬仍要夾在 100%,否則超標會撐爆版面。"""
    block = app._progress_bar_block("熱量攝取", 4000, 2000, "kcal", "#27AE60")
    bar = block["contents"][1]["contents"][0]
    assert bar["width"] == "100%", bar["width"]
    assert bar["backgroundColor"] == "#EF4444", bar["backgroundColor"]


@case
def test_progress_bar_width_floor():
    """幾乎沒吃時仍留最低寬度,讓人看得出那是一條進度條。"""
    block = app._progress_bar_block("熱量攝取", 10, 2000, "kcal", "#27AE60")
    bar = block["contents"][1]["contents"][0]
    assert bar["width"] == "3%", bar["width"]
    assert find_text(block, "(0%)") is not None


@case
def test_progress_bar_zero_target():
    """target 為 0 不可除以零。"""
    block = app._progress_bar_block("熱量攝取", 500, 0, "kcal", "#27AE60")
    assert find_text(block, "(0%)") is not None


@case
def test_progress_bar_at_target_is_not_over_color():
    """剛好打在目標上是達標（卡片的達標定義是 <= target），不是超標；
    上色門檻若用 >= 100 會讓灰字寫著「持平／達標」的同時把柱子塗成超標紅。"""
    block = app._progress_bar_block("熱量攝取", 2000, 2000, "kcal", "#27AE60")
    bar = block["contents"][1]["contents"][0]
    assert bar["backgroundColor"] == "#27AE60", bar["backgroundColor"]


# ---------- summarize_week_days ----------

def fake_days(cals):
    """cals 為 7 個值，None 代表當天沒有記錄。蛋白質固定給熱量的 1/20，方便算平均。"""
    out = []
    for i, c in enumerate(cals):
        date = f"2026-08-{i + 1:02d}"
        if c is None:
            out.append({"date": date, "calories": 0, "protein": 0, "logged": False})
        else:
            out.append({"date": date, "calories": c, "protein": c // 20, "logged": True})
    return out


@case
def test_summarize_denominator_is_logged_days():
    """應攝取用「有記錄的天數」當分母，不是 7 —— 否則會算出灌水的假赤字。"""
    days = fake_days([1800, 1900, 2100, 1700, 1850, None, 1500])
    s = app.summarize_week_days(days, 2000, 160)
    assert s["logged_days"] == 6, s["logged_days"]
    assert s["should_intake"] == 12000, s["should_intake"]
    assert s["actual_intake"] == 10850, s["actual_intake"]
    assert s["diff"] == -1150, s["diff"]


@case
def test_summarize_on_target_days():
    """單日熱量 <= 目標即為達標；沒記錄的那天不算在分母裡。"""
    days = fake_days([1800, 1900, 2100, 1700, 1850, None, 1500])
    s = app.summarize_week_days(days, 2000, 160)
    assert s["on_target_days"] == 5, s["on_target_days"]


@case
def test_summarize_averages_use_logged_days():
    days = fake_days([1800, 2200, None, None, None, None, None])
    s = app.summarize_week_days(days, 2000, 160)
    assert s["avg_cal"] == 2000, s["avg_cal"]
    assert s["avg_protein"] == 100, s["avg_protein"]


@case
def test_summarize_no_logged_days():
    """完全沒記錄不可除以零。"""
    s = app.summarize_week_days(fake_days([None] * 7), 2000, 160)
    assert s["logged_days"] == 0
    assert s["should_intake"] == 0
    assert s["actual_intake"] == 0
    assert s["diff"] == 0
    assert s["avg_cal"] == 0
    assert s["avg_protein"] == 0


@case
def test_summarize_target_fallback():
    """target 為 None 時沿用既有 fallback 2000 / 150。"""
    s = app.summarize_week_days(fake_days([1800] + [None] * 6), None, None)
    assert s["target_cal"] == 2000, s["target_cal"]
    assert s["target_protein"] == 150, s["target_protein"]
    assert s["should_intake"] == 2000, s["should_intake"]


@case
def test_summarize_date_bounds():
    s = app.summarize_week_days(fake_days([1800] * 7), 2000, 160)
    assert s["start_date"] == "2026-08-01", s["start_date"]
    assert s["end_date"] == "2026-08-07", s["end_date"]


# ---------- _week_chart_block ----------

GREY = "#9CA3AF"


def chart_bars(blocks):
    """從圖表 block 取出每一根柱子的 (顏色, flex 高度)。"""
    cols = blocks[0]["contents"]
    out = []
    for col in cols:
        bar = col["contents"][1]
        out.append((bar["backgroundColor"], bar["flex"]))
    return out


@case
def test_chart_colors_by_day_state():
    """在目標內綠、超標紅、沒記錄灰。"""
    days = fake_days([1800, 2400, None, 2000, 1500, 2001, 1000])
    bars = chart_bars(app._week_chart_block(days, 2000, "#27AE60", "#EF4444"))
    colors = [c for c, _ in bars]
    assert colors == ["#27AE60", "#EF4444", GREY, "#27AE60", "#27AE60", "#EF4444", "#27AE60"], colors


@case
def test_chart_colors_follow_goal():
    """增肌時顏色跟著進度條一起換，同一張卡片不能出現兩套語意。"""
    days = fake_days([1800, 2400, None] + [None] * 4)
    bars = chart_bars(app._week_chart_block(days, 2000, "#3B82F6", "#27AE60"))
    assert [c for c, _ in bars][:3] == ["#3B82F6", "#27AE60", GREY], bars


@case
def test_chart_unlogged_bar_is_short_not_zero():
    """沒記錄的柱子給最低高度 —— 完全不畫會被讀成「那天吃 0 卡」。"""
    days = fake_days([2000, None] + [None] * 5)
    bars = chart_bars(app._week_chart_block(days, 2000, "#27AE60", "#EF4444"))
    assert bars[0][1] == 100, bars[0]
    assert 0 < bars[1][1] < 20, bars[1]


@case
def test_chart_all_zero_no_division_by_zero():
    """所有日熱量皆為 0 時不可除以零。"""
    days = fake_days([0] * 7)
    bars = chart_bars(app._week_chart_block(days, 2000, "#27AE60", "#EF4444"))
    assert len(bars) == 7
    assert all(h > 0 for _, h in bars), bars


@case
def test_chart_labels_are_month_day():
    days = fake_days([1800] * 7)
    labels = [t["text"] for t in iter_texts(app._week_chart_block(days, 2000, "#27AE60", "#EF4444")[1])]
    assert labels[0] == "08/01", labels
    assert labels[-1] == "08/07", labels


@case
def test_chart_caption_present_normal_week():
    """[1200, 1250, 1300] 對 2000 大卡目標而言天天吃不夠，但柱子會被拉到接近滿格 ——
    caption 要把換算基準（區間最高單日）印出來，讀者才不會誤會柱子代表「吃很多」。"""
    days = fake_days([1200, 1250, 1300, None, None, None, None])
    blocks = app._week_chart_block(days, 2000, "#27AE60", "#EF4444")
    caption = blocks[-1]["text"]
    assert caption.strip(), "caption 不可為空字串，空字串會讓 LINE 把整則訊息擋成 400"
    assert "1300" in caption, caption


@case
def test_chart_caption_present_all_zero_week():
    """peak 為 0（沒有任何一天記到熱量）時，caption 不能印出「以 0 kcal 為滿格」這種
    沒有意義的敘述，但也絕不能是空字串。"""
    days = fake_days([0] * 7)
    blocks = app._week_chart_block(days, 2000, "#27AE60", "#EF4444")
    caption = blocks[-1]["text"]
    assert caption.strip(), "caption 不可為空字串"
    assert "0 kcal 為滿格" not in caption, caption


# ---------- build_week_flex_card ----------

def week_card(cals, goal="fat_loss", target_cal=2000, target_protein=160):
    stats = app.summarize_week_days(fake_days(cals), target_cal, target_protein)
    return app.build_week_flex_card(stats, goal)


@case
def test_card_header_and_on_target_line():
    card = week_card([1800, 1900, 2100, 1700, 1850, None, 1500])
    assert find_text(card["header"], "近 7 天總結") is not None
    assert find_text(card["header"], "08/01") is not None
    assert find_text(card["header"], "減脂") is not None
    assert find_text(card, "熱量達標 5 / 6 天") is not None


@case
def test_card_deficit_green_for_fat_loss():
    """減脂的人有赤字是好消息，標綠。"""
    card = week_card([1800, 1900, 2100, 1700, 1850, None, 1500])
    for t in iter_texts(card):
        if "1150 kcal" in t["text"]:
            assert t["color"] == "#27AE60", t
            break
    else:
        raise AssertionError("找不到赤字數字")
    assert find_text(card, "累積赤字") is not None


@case
def test_card_surplus_red_for_fat_loss():
    card = week_card([2500, 2500, 2500, None, None, None, None])
    assert find_text(card, "累積盈餘") is not None
    for t in iter_texts(card):
        if "1500 kcal" in t["text"]:
            assert t["color"] == "#EF4444", t
            break
    else:
        raise AssertionError("找不到盈餘數字")


@case
def test_card_diff_zero_is_neutral():
    """實際攝取剛好等於應攝取:累積差額／持平，中性灰，不偏向任一目標。"""
    card = week_card([2000] + [None] * 6)
    assert find_text(card, "累積差額") is not None
    for t in iter_texts(card):
        if t["text"] == "持平":
            assert t["color"] == "#6B7280", t
            break
    else:
        raise AssertionError("找不到「持平」")


@case
def test_card_muscle_gain_flips_colors():
    """增肌的人「沒吃到」才是問題：進度條藍→綠，赤字標紅。"""
    card = week_card([1800, 1900, 1700, None, None, None, None], goal="muscle_gain")
    bar = find_bar_colors(card)
    assert bar == "#3B82F6", bar
    for t in iter_texts(card):
        if "600 kcal" in t["text"]:
            assert t["color"] == "#EF4444", t
            break
    else:
        raise AssertionError("找不到赤字數字")


@case
def test_card_muscle_gain_over_target_bar_is_green():
    """增肌時吃超過目標是好事，驗收 5 的另一半：進度條轉綠，不是停在藍。"""
    card = week_card([2400, 2400, 2400, None, None, None, None], goal="muscle_gain")
    bar = find_bar_colors(card)
    assert bar == "#27AE60", bar


def find_bar_colors(card):
    """取出累積攝取進度條那一條的顏色。"""
    for node in iter_boxes(card):
        if node.get("height") == "8px" and node.get("backgroundColor") not in (None, "#E5E7EB"):
            return node["backgroundColor"]
    return None


def iter_boxes(node):
    if isinstance(node, dict):
        if node.get("type") == "box":
            yield node
        for v in node.values():
            yield from iter_boxes(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_boxes(v)


def find_chart_colors(card):
    """取出卡片內直條圖每一根柱子的顏色（依日期順序），高度 96px 的那個橫列才是圖表，
    跟高度 8px 的進度條區分開來。"""
    for node in iter_boxes(card):
        if node.get("height") == "96px":
            return [col["contents"][1]["backgroundColor"] for col in node["contents"]]
    return None


@case
def test_card_chart_colors_match_progress_bar_fat_loss():
    """圖表柱色要跟進度條共用同一組配色:減脂時目標內綠、超標紅。
    直接鎖住 build_week_flex_card 呼叫 _week_chart_block 的參數順序，
    避免 in_color/over_color 被誤傳成 over_color/in_color 卻沒有測試發現。"""
    card = week_card([1800, 2400, 1900, None, None, None, None])
    colors = find_chart_colors(card)
    assert colors == ["#27AE60", "#EF4444", "#27AE60", GREY, GREY, GREY, GREY], colors


@case
def test_card_chart_colors_match_progress_bar_muscle_gain():
    """增肌時圖表柱色要跟進度條一起換成藍/綠，不能沿用減脂那一套。"""
    card = week_card([1800, 2400, 1900, None, None, None, None], goal="muscle_gain")
    colors = find_chart_colors(card)
    assert colors == ["#3B82F6", "#27AE60", "#3B82F6", GREY, GREY, GREY, GREY], colors


@case
def test_card_shows_real_pct_when_over():
    """驗收 4：單日超標時進度條文字顯示真實百分比。"""
    card = week_card([2400, 2400, 2400, None, None, None, None])
    label = find_text(card, "7200 / 6000")
    assert label is not None and "(120%)" in label, label


@case
def test_card_averages():
    card = week_card([1800, 2200, None, None, None, None, None])
    assert find_text(card, "2000 kcal（目標 2000）") is not None
    assert find_text(card, "100 g（目標 160）") is not None


@case
def test_card_completeness_line_names_missing_days():
    card = week_card([1800, 1900, 2100, 1700, 1850, None, 1500])
    note = find_text(card, "天有記錄")
    assert note is not None and "6 / 7" in note, note
    assert "08/06" in note, note


@case
def test_card_completeness_line_when_full():
    card = week_card([1800] * 7)
    note = find_text(card, "天有記錄")
    assert note is not None and "未列入" not in note, note
    assert "尚未結束" not in note, note


def fake_days_custom(dates_cals):
    """跟 fake_days 一樣，但日期由呼叫端指定 —— 用來組出最後一天剛好等於
    get_today_str() 的 fixture，藉此驅動「今天尚未結束」那條備註而不必凍結時鐘。"""
    out = []
    for date, c in dates_cals:
        if c is None:
            out.append({"date": date, "calories": 0, "protein": 0, "logged": False})
        else:
            out.append({"date": date, "calories": c, "protein": c // 20, "logged": True})
    return out


@case
def test_card_completeness_note_flags_partial_today():
    """今天只要記過一餐就整天算入 should_intake（見 summarize_week_days 的算法，
    這是刻意保留、不能改的行為），但畫面要說清楚今天還沒過完，
    不然使用者會把「還沒吃完的一天」誤讀成「已經確定的赤字」。"""
    today = app.get_today_str()
    days = fake_days_custom([
        ("2026-08-01", 1800), ("2026-08-02", 1900), ("2026-08-03", 2100),
        ("2026-08-04", 1700), ("2026-08-05", 1850), ("2026-08-06", 1500),
        (today, 400),
    ])
    stats = app.summarize_week_days(days, 2000, 160)
    card = app.build_week_flex_card(stats, "fat_loss")
    note = find_text(card, "天有記錄")
    assert note is not None and "尚未結束但已計入計算" in note, note
    assert today[5:].replace("-", "/") in note, note


@case
def test_card_completeness_note_no_clause_when_last_day_not_today():
    """既有 fixture 的最後一天固定是 2026-08-07，跟今天（由環境決定）對不上，
    不該出現「尚未結束」那句 —— 確認新增的分支沒有誤觸發在既有測資上。"""
    today = app.get_today_str()
    assert today != "2026-08-07", "fixture 日期意外撞到今天，這個測試失去意義"
    card = week_card([1800, 1900, 2100, 1700, 1850, None, 1500])
    note = find_text(card, "天有記錄")
    assert note is not None and "尚未結束" not in note, note


@case
def test_card_all_texts_non_empty():
    """驗收 6：Flex 的 text 一律非空，空字串會讓整則訊息被 LINE 擋成 400。"""
    for cals in ([1800] * 7, [1800, 1900, 2100, None, None, None, 1500], [0, 0, 0, None, None, None, None]):
        card = week_card(cals)
        for t in iter_texts(card):
            assert str(t.get("text", "")).strip(), f"{cals} → 空字串 text: {t}"


@case
def test_card_survives_sanitize_flex():
    """走一遍實際送出前的清理，確保結構沒有壞掉。"""
    card = week_card([1800, 1900, None, None, None, None, 1500])
    assert app.sanitize_flex(card) == card


# ---------- 入口 ----------

@case
def test_quick_reply_has_week_button():
    """user_id=None 時 get_quick_reply 不查資料庫，可離線驗。"""
    labels = [b.action.label for b in app.get_quick_reply().items]
    assert "本週總結" in labels, labels
    assert len(labels) <= 13, labels          # LINE 上限
    assert all(len(l) <= 20 for l in labels), labels


@case
def test_capabilities_mentions_week_summary():
    assert "本週總結" in app.BOT_CAPABILITIES, app.BOT_CAPABILITIES
    assert "週報" not in app.BOT_CAPABILITIES.split("尚未開放")[1], app.BOT_CAPABILITIES


# ---------- runner ----------

def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"✅ {fn.__name__}")
        except Exception as e:
            # 攔 Exception 而非只攔 AssertionError:函式還沒實作時是 AttributeError,
            # 只攔 AssertionError 會讓第一個未實作的測試把整輪跑掉。
            failed += 1
            print(f"❌ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - failed} / {len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
