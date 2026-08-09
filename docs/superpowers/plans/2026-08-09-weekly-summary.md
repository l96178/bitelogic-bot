# 近 7 天總結 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓用戶輸入「本週總結」就看到一張近 7 天飲食卡片，重點是累積熱量赤字。

**Architecture:** 三個新函式加進單檔的 `app.py`，嚴格切成三層 —— `get_week_stats` 只查資料庫、`summarize_week_days` 只算數學、`build_week_flex_card` / `_week_chart_block` 只渲染 Flex JSON。中間那層拆出來是為了能用假資料測試（設計文件的驗收前四項都在這層）。指令路由掛在既有指令區，在所有 AI 路徑之前，不動 AI 流程也不動資料表。

**Tech Stack:** Python 3 / Flask / line-bot-sdk v2（Flex Message）/ supabase-py。測試是不依賴 pytest 的純 `assert` 腳本，用專案 `.venv` 的直譯器執行。

## Global Constraints

以下規則來自設計文件 `docs/superpowers/specs/2026-08-09-weekly-summary-design.md`，每一項任務都適用：

- **只計算「有記錄的天數」。** `should_intake = logged_days × target_calories`，分母永遠不是 7。假赤字比沒有赤字更糟。
- **「有記錄」＝該日至少有一筆 `meal_items`。** 只有 `daily_logs` 沒有 `meal_items` 的日子視為沒記錄。
- **滾動 7 天含今天**，即 `today-6` 到 `today`，以台北時間 `TAIWAN_TZ` 為準。文案一律寫「近 7 天」，不可寫「本週」。
- **不動資料表、不動既有卡片、不動 AI 流程、不動其他攔截器。**
- **不加 AI 講評、不加推播、不顯示體重與常吃店家。**
- **所有 Flex `text` 必須非空**（`sanitize_flex` 是兜底，不是藉口）。
- 所有卡片一律走 `flex_message(...)` 這個出口，不可直接 `FlexSendMessage`。
- header 用 `CARD_HEADER_BG` / `CARD_HEADER_SUB`，深底 emoji 用 🦴，白底（`alt_text`）用 🐾。
- 文案不使用千分位逗號，與既有卡片一致（設計文件示意圖裡的 `10,850` 只是示意）。
- 進度條的綠／紅是語意色，不可為了美觀更動。

---

## File Structure

| 檔案 | 責任 | 動作 |
|---|---|---|
| `app.py` | 整個應用（單檔架構，沿用不重構） | 修改：新增 4 個函式、改 `_progress_bar_block`、改 `get_quick_reply`、改 `BOT_CAPABILITIES`、`handle_message` 加一條指令路由 |
| `tests/test_week_summary.py` | 近 7 天總結的純函式測試（不連資料庫） | 新增 |
| `tests/README.md` | 怎麼跑測試 | 新增 |
| `.gitignore` | 已含 `.venv/`，不需修改 | 不動 |

`app.py` 已經一千六百多行，但本專案就是單檔部署到 Render，這次不做拆檔 —— 拆檔會動到 import 結構與部署設定，風險遠大於本功能本身。新函式依既有分區擺放：`_week_chart_block` / `build_week_flex_card` 放在 `build_weight_flex_card` 之後（卡片區），`get_week_stats` / `summarize_week_days` 放在 `get_weight_rows` 之前（資料區）。

## 與設計文件的兩處落差（已決定，實作照這裡走）

1. **直條圖顏色也跟著目標調換。** 設計文件只寫了進度條依 `goal` 換色，直條圖寫死綠／紅。照字面做會出現同一張卡片上「進度條藍色代表沒吃到、直條圖綠色也代表沒吃到」的矛盾。因此 `_week_chart_block` 收下與進度條同一組 `in_color` / `over_color`；減脂時算出來就是設計文件寫的綠／紅／灰，不違背原意。
2. **不用千分位逗號。** 設計文件示意圖寫 `10,850`，但 `_progress_bar_block` 是既有函式、輸出的是無逗號的原始數字。只在赤字那行加逗號會讓同一張卡片兩種格式。全卡片統一不加。

---

### Task 1: 修好進度條的百分比夾值（順帶建立測試環境）

`_progress_bar_block` 把顯示用的百分比夾在 100，超標時文字顯示「(100%)」而不是真實的 116% —— 剛好把最需要被看見的數字抹掉。今日卡片現在就有這個問題，週總結會重用同一個函式，先修。

這一項同時建立測試環境：專案目前沒有 `.venv` 也沒有 `tests/`，而 `app.py` 在 import 時就會建立 LINE／Gemini／Supabase 客戶端，所以測試腳本必須先塞假的環境變數才能 import。這些設定屬於「第一個測試需要的東西」，一併做完。

**Files:**
- Create: `tests/test_week_summary.py`
- Create: `tests/README.md`
- Modify: `app.py:350-368`（`_progress_bar_block`）

**Interfaces:**
- Consumes: 無
- Produces: `_progress_bar_block(label, current, target, unit, bar_color, over_color="#EF4444") -> dict`（簽章不變，行為改變：文字百分比不再夾上限）；測試檔的兩個 helper `iter_texts(node) -> generator[dict]`、`find_text(node, needle) -> str | None` 供後續任務重用

- [ ] **Step 1: 確認虛擬環境可用**

系統 python 沒有 `requests` / `flask` / `supabase`，不建環境就 import 不了 `app.py`。撰寫本計畫時已經建好 `.venv`（`.gitignore` 早就忽略它，不會進 repo），先驗證：

```bash
cd /Users/xusongyi/Desktop/BiteLogic
.venv/bin/python -c "import flask, linebot, supabase, google.genai; print('deps ok')" 2>/dev/null
```

Expected: 印出 `deps ok`。若 `.venv` 不存在或缺套件，重建一次：

```bash
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
```

註：這個環境是 Python 3.9，import 時會噴 urllib3 與 google-auth 的 `FutureWarning`，與本功能無關，`2>/dev/null` 濾掉即可。程式碼不可使用 3.10 以後才有的語法（如 `match`、`X | Y` 型別標註）。

- [ ] **Step 2: 寫下失敗的測試**

Create `tests/test_week_summary.py`：

```python
"""近 7 天總結的純函式測試。

不連資料庫：所有測試只碰「拿到資料之後」的那一層（summarize_week_days、
_week_chart_block、build_week_flex_card、_progress_bar_block）。

app.py 在 import 時就會建立 LINE / Gemini / Supabase 客戶端，所以這裡先塞
假的環境變數。三個客戶端的建構式都不連網，只要有值就建得起來。

執行：.venv/bin/python tests/test_week_summary.py
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
    """走訪 Flex 樹，吐出所有 type=text 的節點。"""
    if isinstance(node, dict):
        if node.get("type") == "text":
            yield node
        for v in node.values():
            yield from iter_texts(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_texts(v)


def find_text(node, needle):
    """回傳第一個含有 needle 的 text 字串，找不到回 None。"""
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
    """超標時顯示真實百分比，不是被夾住的 100%。"""
    block = app._progress_bar_block("熱量攝取", 2320, 2000, "kcal", "#27AE60")
    label = find_text(block, "2320 / 2000")
    assert label is not None, "找不到進度條文字"
    assert "(116%)" in label, label


@case
def test_progress_bar_width_capped_at_100():
    """條寬仍要夾在 100%，否則超標會撐爆版面。"""
    block = app._progress_bar_block("熱量攝取", 4000, 2000, "kcal", "#27AE60")
    bar = block["contents"][1]["contents"][0]
    assert bar["width"] == "100%", bar["width"]
    assert bar["backgroundColor"] == "#EF4444", bar["backgroundColor"]


@case
def test_progress_bar_width_floor():
    """幾乎沒吃時仍留最低寬度，讓人看得出那是一條進度條。"""
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
            # 攔 Exception 而非只攔 AssertionError：函式還沒實作時是 AttributeError，
            # 只攔 AssertionError 會讓第一個未實作的測試把整輪跑掉。
            failed += 1
            print(f"❌ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - failed} / {len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: 執行測試確認它失敗**

Run: `.venv/bin/python tests/test_week_summary.py`
Expected: `test_progress_bar_shows_real_pct_when_over` 失敗（目前輸出 `2320 / 2000 kcal (100%)`），其餘三項通過。

- [ ] **Step 4: 改 `_progress_bar_block`**

把 `app.py:350-368` 整個函式換成：

```python
def _progress_bar_block(label, current, target, unit, bar_color, over_color="#EF4444"):
    # 顯示用的百分比不夾上限：夾住會讓超標的 116% 印成 100%，剛好抹掉最該被看見的數字。
    # 條寬另外夾 —— 那是版面限制，不是資訊。
    pct = max(0, int(current * 100 / target)) if target > 0 else 0
    width = min(100, max(3, pct))
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
                "contents": [{"type": "box", "layout": "vertical", "backgroundColor": over_color if pct >= 100 else bar_color, "height": "8px", "width": f"{width}%", "cornerRadius": "4px", "contents": []}]
            }
        ]
    }
```

- [ ] **Step 5: 執行測試確認全部通過**

Run: `.venv/bin/python tests/test_week_summary.py`
Expected: `4 / 4 passed`

- [ ] **Step 6: 寫測試說明**

Create `tests/README.md`：

```markdown
# 測試

不依賴 pytest —— 專案的相依套件清單（`requirements.txt`）就是 Render 上跑的那份，
不想為了測試在部署環境多裝東西。每個檔案都是可直接執行的腳本。

```bash
python3 -m venv .venv                    # 第一次才要
.venv/bin/pip install -r requirements.txt
.venv/bin/python tests/test_week_summary.py
```

腳本會自己塞假的環境變數（`SUPABASE_URL` 之類）才 import `app.py`，
因為 `app.py` 在 import 當下就會建立三個客戶端。這些客戶端的建構式不連網。

測試只涵蓋純函式（算數與 Flex 渲染），不碰資料庫、不呼叫 LINE 與 Gemini。
```

- [ ] **Step 7: Commit**

```bash
git add tests/test_week_summary.py tests/README.md app.py
git commit -m "fix: 進度條超標時顯示真實百分比，並建立測試腳本"
```

---

### Task 2: 近 7 天的資料彙總

拆成兩個函式：`get_week_stats` 只負責兩次查詢並把資料整成「由舊到新固定 7 筆」的列表，`summarize_week_days` 只做算術。設計文件的驗收 1 落在後者，用假資料就能驗。

**Files:**
- Modify: `app.py`（在 `def get_weight_rows` 之前插入兩個新函式，約 `app.py:682`）
- Test: `tests/test_week_summary.py`

**Interfaces:**
- Consumes: `get_today_str()`、`TAIWAN_TZ`、`supabase`
- Produces:
  - `summarize_week_days(days, target_cal, target_protein) -> dict`
    - `days`：由舊到新的 7 筆 `{"date": "YYYY-MM-DD", "calories": int, "protein": int, "logged": bool}`
    - 回傳 key：`days`、`logged_days`、`on_target_days`、`actual_intake`、`should_intake`、`diff`、`avg_cal`、`avg_protein`、`target_cal`、`target_protein`、`start_date`、`end_date`
  - `get_week_stats(user_id, target_cal, target_protein) -> dict`（同上結構）

- [ ] **Step 1: 寫下失敗的測試**

在 `tests/test_week_summary.py` 的 `# ---------- runner ----------` 之前插入：

```python
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
```

- [ ] **Step 2: 執行測試確認它失敗**

Run: `.venv/bin/python tests/test_week_summary.py`
Expected: 前四項通過，六個新測試都印出 `AttributeError: module 'app' has no attribute 'summarize_week_days'`。

- [ ] **Step 3: 實作兩個函式**

在 `app.py` 的 `def get_weight_rows(user_id, limit=8):` 之前插入：

```python
WEEK_DAYS = 7
# 沒有 target 時的保底值，與 handle_message 內的既有 fallback 一致。
DEFAULT_TARGET_CAL, DEFAULT_TARGET_PROTEIN = 2000, 150


def summarize_week_days(days, target_cal, target_protein):
    """把逐日資料算成近 7 天的彙總。純算術，不查資料庫（所以能用假資料測）。

    days 由舊到新，每筆 {date, calories, protein, logged}。

    分母一律是「有記錄的天數」而不是 7：沒記錄的那幾天用戶還是有吃，只是沒記，
    用 7 天算應攝取會得到灌水的赤字，讓減脂的人以為自己瘦了。
    一個假的赤字比沒有赤字更糟。"""
    target_cal = target_cal or DEFAULT_TARGET_CAL
    target_protein = target_protein or DEFAULT_TARGET_PROTEIN

    logged = [d for d in days if d["logged"]]
    logged_days = len(logged)
    actual = sum(int(d["calories"]) for d in logged)
    protein_sum = sum(int(d["protein"]) for d in logged)
    should = logged_days * target_cal

    return {
        "days": days,
        "logged_days": logged_days,
        "on_target_days": sum(1 for d in logged if d["calories"] <= target_cal),
        "actual_intake": actual,
        "should_intake": should,
        "diff": actual - should,
        "avg_cal": round(actual / logged_days) if logged_days else 0,
        "avg_protein": round(protein_sum / logged_days) if logged_days else 0,
        "target_cal": target_cal,
        "target_protein": target_protein,
        "start_date": days[0]["date"],
        "end_date": days[-1]["date"],
    }


def get_week_stats(user_id, target_cal, target_protein):
    """近 7 天（含今天）的飲食彙總。兩次查詢，不做任何渲染。

    「有記錄」的定義是該日至少有一筆 meal_items —— 只開了 daily_logs 卻沒寫入
    任何一筆的日子（例如查了今日進度就離開）不算，否則會被當成「那天吃 0 卡」。"""
    today = datetime.now(TAIWAN_TZ).date()
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(WEEK_DAYS - 1, -1, -1)]

    totals = {d: {"calories": 0, "protein": 0, "logged": False} for d in dates}

    logs = supabase.table("daily_logs").select("id, log_date").eq("user_id", user_id) \
        .gte("log_date", dates[0]).lte("log_date", dates[-1]).execute()
    id_to_date = {r["id"]: r["log_date"] for r in (logs.data or [])}

    if id_to_date:
        meals = supabase.table("meal_items").select("daily_log_id, calories, protein_g") \
            .in_("daily_log_id", list(id_to_date)).execute()
        for m in (meals.data or []):
            slot = totals.get(id_to_date.get(m["daily_log_id"]))
            if slot is None:
                continue
            slot["calories"] += int(m.get("calories") or 0)
            slot["protein"] += int(m.get("protein_g") or 0)
            slot["logged"] = True

    days = [{"date": d, **totals[d]} for d in dates]
    return summarize_week_days(days, target_cal, target_protein)
```

- [ ] **Step 4: 執行測試確認全部通過**

Run: `.venv/bin/python tests/test_week_summary.py`
Expected: `10 / 10 passed`

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_week_summary.py
git commit -m "feat: 近 7 天飲食彙總（只以有記錄的天數為分母）"
```

---

### Task 3: 逐日直條圖

沿用體重趨勢卡的手法（`filler` + `flex` 比例撐柱高），不需要新技術。與體重圖不同的是縱軸從 0 起算 —— 熱量的 0 是有意義的基準。

**Files:**
- Modify: `app.py`（在 `def build_weight_flex_card(rows):` 之前插入，約 `app.py:720`）
- Test: `tests/test_week_summary.py`

**Interfaces:**
- Consumes: `summarize_week_days` 產出的 `days` 結構
- Produces: `_week_chart_block(days, target_cal, in_color, over_color) -> list[dict]`，回傳兩個 Flex box（柱子列、日期標籤列）

- [ ] **Step 1: 寫下失敗的測試**

在 `tests/test_week_summary.py` 的 `# ---------- runner ----------` 之前插入：

```python
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
```

- [ ] **Step 2: 執行測試確認它失敗**

Run: `.venv/bin/python tests/test_week_summary.py`
Expected: 五個新測試都因 `module 'app' has no attribute '_week_chart_block'` 失敗。

- [ ] **Step 3: 實作 `_week_chart_block`**

在 `app.py` 的 `def build_weight_flex_card(rows):` 之前插入：

```python
CHART_GREY = "#9CA3AF"       # 沒有記錄的那一天
CHART_MIN_FLEX = 8           # 有記錄但熱量極低時的最低柱高
CHART_UNLOGGED_FLEX = 6      # 沒記錄的柱高，刻意比 CHART_MIN_FLEX 更矮


def _week_chart_block(days, target_cal, in_color, over_color):
    """逐日熱量直條圖。手法與 _weight_chart_block 相同：filler + flex 比例撐高度。

    與體重圖不同，縱軸從 0 起算 —— 「今天吃了多少」的 0 是真的 0，
    不像體重那樣需要放大幾公斤的差異。柱高以區間內最高的單日熱量為分母。

    沒記錄的日子畫一根灰色矮柱而不是留白：留白會被讀成「那天吃 0 卡」，
    那正是這張卡片最想避免的誤解。"""
    peak = max([d["calories"] for d in days if d["logged"]] or [0])

    cols, labels = [], []
    for d in days:
        if not d["logged"]:
            flex, color = CHART_UNLOGGED_FLEX, CHART_GREY
        else:
            pct = int(round(d["calories"] / peak * 100)) if peak > 0 else CHART_MIN_FLEX
            flex = min(100, max(CHART_MIN_FLEX, pct))
            color = over_color if d["calories"] > target_cal else in_color
        cols.append({
            "type": "box", "layout": "vertical", "contents": [
                {"type": "filler", "flex": max(1, 100 - flex)},
                {"type": "box", "layout": "vertical", "flex": flex, "backgroundColor": color,
                 "cornerRadius": "sm", "contents": [{"type": "filler"}]}
            ]
        })
        labels.append({"type": "text", "text": d["date"][5:].replace("-", "/"),
                       "size": "xxs", "color": CHART_GREY, "align": "center", "flex": 1})

    return [
        {"type": "box", "layout": "horizontal", "height": "96px", "spacing": "xs", "margin": "lg", "contents": cols},
        {"type": "box", "layout": "horizontal", "spacing": "xs", "margin": "sm", "contents": labels},
    ]
```

- [ ] **Step 4: 執行測試確認全部通過**

Run: `.venv/bin/python tests/test_week_summary.py`
Expected: `15 / 15 passed`

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_week_summary.py
git commit -m "feat: 近 7 天逐日熱量直條圖"
```

---

### Task 4: 近 7 天總結卡片

純渲染，不查資料庫 —— 所有數字都由 `summarize_week_days` 的輸出提供。

**Files:**
- Modify: `app.py`（緊接在 `_week_chart_block` 之後，`build_weight_flex_card` 之前）
- Test: `tests/test_week_summary.py`

**Interfaces:**
- Consumes: `summarize_week_days` 的回傳 dict、`_week_chart_block`、`_progress_bar_block`、`_kv_row`、`CARD_HEADER_BG`、`CARD_HEADER_SUB`、`GOAL_MAP_TO_DISP`
- Produces: `build_week_flex_card(stats, goal) -> dict`（Flex bubble）

- [ ] **Step 1: 寫下失敗的測試**

在 `tests/test_week_summary.py` 的 `# ---------- runner ----------` 之前插入：

```python
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
def test_card_muscle_gain_flips_colors():
    """增肌的人「沒吃到」才是問題：進度條藍→綠，赤字標紅。"""
    card = week_card([1800, 1900, 1700, None, None, None, None], goal="muscle_gain")
    bar = find_bar_colors(card)
    assert bar == "#3B82F6", bar
    for t in iter_texts(card):
        if "600 kcal" in t["text"] and "累積" not in t["text"]:
            assert t["color"] == "#EF4444", t
            break
    else:
        raise AssertionError("找不到赤字數字")


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
```

- [ ] **Step 2: 執行測試確認它失敗**

Run: `.venv/bin/python tests/test_week_summary.py`
Expected: 十個新測試都因 `module 'app' has no attribute 'build_week_flex_card'` 失敗。

- [ ] **Step 3: 實作 `build_week_flex_card`**

在 `app.py` 的 `_week_chart_block` 之後插入：

```python
def build_week_flex_card(stats, goal):
    """近 7 天總結卡片。純渲染，數字全部由 stats 提供，這裡不做任何查詢或推算。"""
    goal_disp = GOAL_MAP_TO_DISP.get(goal, goal) or "減脂"
    # 增肌的人「沒吃到」才是問題，好壞方向與減脂相反。這一組顏色同時餵給進度條與直條圖，
    # 否則同一張卡片上會出現「藍色代表沒吃到、綠色也代表沒吃到」的矛盾。
    gaining = goal == "muscle_gain"
    in_color, over_color = ("#3B82F6", "#27AE60") if gaining else ("#27AE60", "#EF4444")

    diff = stats["diff"]
    if diff == 0:
        diff_label, diff_value, diff_color = "累積差額", "持平", "#6B7280"
    elif diff < 0:
        diff_label, diff_value = "累積赤字", f"{abs(diff)} kcal"
        diff_color = "#EF4444" if gaining else "#27AE60"
    else:
        diff_label, diff_value = "累積盈餘", f"{diff} kcal"
        diff_color = "#27AE60" if gaining else "#EF4444"

    missing = [d["date"][5:].replace("-", "/") for d in stats["days"] if not d["logged"]]
    note = f"{stats['logged_days']} / {WEEK_DAYS} 天有記錄"
    if missing:
        note += f"，{'、'.join(missing)} 未列入計算"
    note += "。"

    body = [
        {"type": "text", "text": f"熱量達標 {stats['on_target_days']} / {stats['logged_days']} 天",
         "size": "sm", "weight": "bold", "color": "#1F2937"},
        *_week_chart_block(stats["days"], stats["target_cal"], in_color, over_color),
        {"type": "separator", "margin": "lg"},
        _progress_bar_block("累積攝取", stats["actual_intake"], stats["should_intake"], "kcal", in_color, over_color),
        {
            "type": "box", "layout": "horizontal", "margin": "md", "contents": [
                {"type": "text", "text": diff_label, "size": "sm", "weight": "bold", "color": "#374151"},
                {"type": "text", "text": diff_value, "size": "sm", "weight": "bold", "color": diff_color, "align": "end"}
            ]
        },
        {"type": "separator", "margin": "lg"},
        _kv_row("平均每日", f"{stats['avg_cal']} kcal（目標 {stats['target_cal']}）"),
        _kv_row("平均蛋白質", f"{stats['avg_protein']} g（目標 {stats['target_protein']}）"),
        {"type": "separator", "margin": "lg"},
        {"type": "text", "text": note, "size": "xs", "color": "#9CA3AF", "margin": "md", "wrap": True},
    ]

    start = stats["start_date"][5:].replace("-", "/")
    end = stats["end_date"][5:].replace("-", "/")
    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": CARD_HEADER_BG, "paddingAll": "lg",
            "contents": [
                {"type": "text", "text": "🦴 近 7 天總結", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"{start}～{end} ｜ {goal_disp}", "color": CARD_HEADER_SUB, "size": "xs", "margin": "xs"}
            ]
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "lg", "contents": body}
    }
```

- [ ] **Step 4: 執行測試確認全部通過**

Run: `.venv/bin/python tests/test_week_summary.py`
Expected: `25 / 25 passed`

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_week_summary.py
git commit -m "feat: 近 7 天總結卡片"
```

---

### Task 5: 指令入口、快速選單與能力清單

把功能接上去。三處都是既有結構的小改，測試只涵蓋 `get_quick_reply`（`user_id=None` 時不查資料庫），指令路由與 `BOT_CAPABILITIES` 在 Task 6 手動驗。

**Files:**
- Modify: `app.py:76`（`BOT_CAPABILITIES`）
- Modify: `app.py:446-477`（`get_quick_reply`）
- Modify: `app.py:1618-1625` 之後（`handle_message` 的指令區，接在 `體重紀錄` 查詢那段之後）
- Test: `tests/test_week_summary.py`

**Interfaces:**
- Consumes: `get_week_stats`、`build_week_flex_card`、`flex_message`、`get_quick_reply`
- Produces: 無新函式

- [ ] **Step 1: 寫下失敗的測試**

在 `tests/test_week_summary.py` 的 `# ---------- runner ----------` 之前插入：

```python
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
```

- [ ] **Step 2: 執行測試確認它失敗**

Run: `.venv/bin/python tests/test_week_summary.py`
Expected: 兩個新測試失敗（快速選單沒有「本週總結」、`BOT_CAPABILITIES` 的「週報」還在尚未開放那半段）。

- [ ] **Step 3: 加快速選單按鈕**

`app.py:446` 的 `base_items` 改成：

```python
    base_items = [
        QuickReplyButton(action=MessageAction(label="今日卡路里", text="查看今日卡路里")),
        QuickReplyButton(action=MessageAction(label="本週總結", text="本週總結")),
        QuickReplyButton(action=MessageAction(label="個人檔案", text="個人檔案"))
    ]
```

- [ ] **Step 4: 更新能力清單**

`app.py:76` 整行換成：

```python
BOT_CAPABILITIES = "我是 Coco-你的專屬AI飲食顧問。目前具備的功能:餐廳口袋菜單推薦、飲食紀錄、刪除今日任何一筆紀錄、查詢今日進度與明細、個人檔案、體重紀錄、近 7 天總結（輸入「本週總結」）。紀錄寫入後不能修改，只能刪除後重新輸入。尚未開放:修改紀錄、月報、拍照辨識、主動提醒推播。"
```

- [ ] **Step 5: 加指令路由**

在 `handle_message` 裡，緊接在「體重指令:查詢趨勢」那個 `if` 區塊的 `return` 之後（`app.py:1625` 附近）、「體重指令:記錄」的 `w_match` 之前插入：

```python
        # 近 7 天總結:滾動視窗，含今天。不叫「本週」是因為週一查只會有一天資料，
        # 但指令仍收「本週總結」—— 用戶就是這樣講的。
        if user_msg in ["本週總結", "本周總結", "週報", "周報", "這週如何", "這周如何"]:
            stats = get_week_stats(user_id, target_cal, target_protein)
            n = stats["logged_days"]
            if n == 0:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="近 7 天還沒有任何記錄。", quick_reply=get_quick_reply(user_id)))
            elif n < 3:
                # 一兩天看不出趨勢，畫圖只會給出過度自信的結論。
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"近 7 天只有 {n} 天有記錄，還看不出趨勢。\n再記個幾天，這裡就會出現完整的總結。",
                    quick_reply=get_quick_reply(user_id)))
            else:
                line_bot_api.reply_message(event.reply_token, flex_message(
                    alt_text=f"🐾 近 7 天總結（{n} 天有記錄）",
                    contents=build_week_flex_card(stats, profile.get("goal")),
                    quick_reply=get_quick_reply(user_id)))
            return
```

- [ ] **Step 6: 執行測試確認全部通過**

Run: `.venv/bin/python tests/test_week_summary.py`
Expected: `27 / 27 passed`

- [ ] **Step 7: 確認語法與 import 正常**

Run: `.venv/bin/python -m py_compile app.py && echo "compile ok"`
Expected: `compile ok`

- [ ] **Step 8: Commit**

```bash
git add app.py tests/test_week_summary.py
git commit -m "feat: 接上「本週總結」指令、快速選單按鈕與能力清單"
```

---

### Task 6: 對照設計文件逐項驗收並推上 GitHub

設計文件列了 6 條驗收，前四條由測試涵蓋，第五、六條也已進測試。這一項是把它們對齊確認、跑一次完整測試、再推上去（推送會觸發 Render 重新部署，所以放在最後）。

**Files:**
- Modify: `docs/superpowers/specs/2026-08-09-weekly-summary-design.md:4`（狀態改為「已實作」）

**Interfaces:**
- Consumes: 前五項任務的全部產出
- Produces: 無

- [ ] **Step 1: 跑完整測試**

Run: `.venv/bin/python tests/test_week_summary.py`
Expected: `27 / 27 passed`

- [ ] **Step 2: 逐條對照設計文件的驗收**

把每一條對到測試名稱，任何一條對不上就補測試而不是改驗收標準：

| 驗收 | 對應測試 |
|---|---|
| 1. 6 天記錄：6 彩柱 + 1 灰柱，分母 6，赤字＝6 天應攝取減實際 | `test_summarize_denominator_is_logged_days`、`test_chart_colors_by_day_state`、`test_card_completeness_line_names_missing_days` |
| 2. 0 天記錄：回文字不畫卡片 | `test_summarize_no_logged_days` + Step 4 手動驗 |
| 3. 2 天記錄：回文字說明資料不足 | Step 4 手動驗 |
| 4. 單日超標：柱紅、進度條顯示真實百分比 | `test_chart_colors_by_day_state`、`test_card_shows_real_pct_when_over` |
| 5. `muscle_gain`：進度條藍→綠、赤字標紅 | `test_card_muscle_gain_flips_colors` |
| 6. 所有 Flex `text` 非空 | `test_card_all_texts_non_empty`、`test_card_survives_sanitize_flex` |

- [ ] **Step 3: 確認今日卡片沒有被進度條的改動弄壞**

`_progress_bar_block` 是共用函式，今日卡片也吃這個改動。跑一段檢查：

```bash
.venv/bin/python - <<'PY'
import os
for k, v in {"GEMINI_API_KEY": "t", "LINE_CHANNEL_ACCESS_TOKEN": "t", "LINE_CHANNEL_SECRET": "t",
             "SUPABASE_URL": "https://t.supabase.co", "SUPABASE_KEY": "t"}.items():
    os.environ.setdefault(k, v)
import app, json
meals = [{"id": 1, "food_name": "【7-11】雞胸肉沙拉", "calories": 320, "protein_g": 30}]
card = app.build_today_card(meals, 2320, 160, 2000, 150, "fat_loss")
dumped = json.dumps(card, ensure_ascii=False)
assert "(116%)" in dumped, "今日卡片的百分比沒有解除夾值"
assert app.sanitize_flex(card) == card
print("今日卡片 ok：超標顯示 116%")
PY
```

Expected: `今日卡片 ok：超標顯示 116%`

- [ ] **Step 4: 在 LINE 上人工驗這三種資料狀態**

程式驗不到的是真機排版。請用戶（或自己的測試帳號）依序做：

1. **完整資料**：對有 3 天以上記錄的帳號輸入「本週總結」→ 應出現卡片，直條圖柱數為 7、日期標籤不折行、灰柱明顯比彩柱矮。
2. **資料不足**：對只有 1～2 天記錄的帳號輸入「週報」→ 應回文字「近 7 天只有 N 天有記錄…」，不出卡片。
3. **完全沒記錄**：新帳號建檔後直接輸入「這週如何」→ 應回「近 7 天還沒有任何記錄。」
4. 每一則回覆下方的快速選單都要看得到「本週總結」按鈕。
5. 問 Coco「你會做週報嗎？」→ 應說得出這項功能存在，而不是說尚未開放。

- [ ] **Step 5: 更新設計文件狀態**

把 `docs/superpowers/specs/2026-08-09-weekly-summary-design.md` 第 4 行的 `狀態：待實作` 改成 `狀態：已實作（2026-08-09）`。

- [ ] **Step 6: Commit 並推上 GitHub**

推送會觸發 Render 重新部署，所以確定前面每一步都通過再做。

```bash
git add docs/superpowers/specs/2026-08-09-weekly-summary-design.md docs/superpowers/plans/2026-08-09-weekly-summary.md
git commit -m "docs: 近 7 天總結設計文件與實作計畫"
git push -u origin main
```

- [ ] **Step 7: 確認部署**

```bash
git log --oneline -3
git status -sb
```

Expected: 顯示 `## main...origin/main`（沒有 ahead），工作區乾淨。接著在 Render 後台確認這次 deploy 成功，再到 LINE 上重跑 Step 4 的第 1 項。
