# 測試

不依賴 pytest —— 專案的相依套件清單(`requirements.txt`)就是 Render 上跑的那份,
不想為了測試在部署環境多裝東西。每個檔案都是可直接執行的腳本。

```bash
python3 -m venv .venv                    # 第一次才要
.venv/bin/pip install -r requirements.txt
.venv/bin/python tests/test_week_summary.py
```

腳本會自己塞假的環境變數(`SUPABASE_URL` 之類)才 import `app.py`,
因為 `app.py` 在 import 當下就會建立三個客戶端。這些客戶端的建構式不連網。

測試只涵蓋純函式(算數與 Flex 渲染),不碰資料庫、不呼叫 LINE 與 Gemini。
