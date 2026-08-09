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
