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
