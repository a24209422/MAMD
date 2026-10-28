<!-- agent-board:start（board.py init 產生，重跑 init 會整段換成最新版） -->
## agent-board 流程（寫 plan 的 agent 遵守）

接需求並規劃的 agent：使用者說需求時，你負責寫 plan，不要自己寫程式；使用者明確要求直接實作時，依該要求執行。

- 自動流程的 writer 依已核准任務實作與測試；reviewer 依需求、diff 與測試結果唯讀審查。兩者不重新寫或送出 plan，也不自動呼叫 agent-board-init／agent-board-plan。
- 下方「plan 的寫法」「送出與進度」適用於規劃角色；「不可以做的事」適用於所有角色。
- 所有指令從目標專案根目錄執行。換電腦 clone 後，先呼叫 agent-board-init 重建本機環境與設定。

### plan 的寫法

- 存成 `plans/NNNN-英文短名.md`，NNNN 接在 `plans/` 現有最大編號後面
- plan 檔一律存成 UTF-8（Windows PowerShell 5.1 請用 `Set-Content -Encoding utf8` 或 `Out-File -Encoding utf8`）
- 檔頭：

  ```yaml
  ---
  id: P0007
  created: 2026-10-25
  area: [後端/上傳]     # 選填，寫成「端/模組」，模組可省略，例如 [前端/上傳, 後端/上傳]、[後端]
  supersedes: P0003     # 選填，這份 plan 取代了哪一份
  ---
  ```

- 檔頭下面一定要有一行 `# 標題`，用一句話說這份 plan 要做什麼；沒有標題會被 submit 擋下
- 內容是一張追溯矩陣，欄位固定：`| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |`
  - 編號和內容之間用一個空格，例如 `T1 新增 subtract`，不要寫成 `T1：新增 subtract`
  - 需求：使用者原話，編號 R1、R2…
  - AI 理解：你怎麼理解這條需求
  - 做法：編號 D1、D2…；不採用寫「考慮過的做法：不採用的原因」
  - 任務：編號 T1、T2…；一個任務要小到一次審查看得完
  - 前置：這個任務要等哪些任務完成，寫任務編號，沒有就留空
    - 兩個任務會改到同一個檔案時，後面的任務要把前面的寫進前置；沒有前置的任務會同時進行，改到同一個檔案合併時會衝突
  - 驗證：使用者實際操作什麼、應該看到什麼
  - 同一個編號出現在多列代表同一個節點，內容只寫一次
  - 每個任務都要對應到需求；沒有需求的改動不要寫進去
- 完整範例：

  ```markdown
  ---
  id: P0007
  created: 2026-10-25
  area: [後端/計算]
  ---

  # 新增減法和乘法

  | 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
  |---|---|---|---|---|---|---|
  | R1 新增 subtract 和 multiply，各自要有測試 | 在 calc.py 加兩個函式，test_calc.py 各加一個測試 | D1 沿用 add 的寫法，直接回傳運算結果 | 新增計算器類別：兩個簡單運算不需要多一層架構 | T1 新增 subtract 和測試 | | 執行 python -m pytest -q，減法測試通過 |
  | R1 | | D1 | | T2 新增 multiply 和測試 | T1 | 執行 python -m pytest -q，乘法測試通過 |
  ```

  T2 和 T1 都會改 `calc.py`，所以 T2 的前置寫 T1。

### 送出與進度

- 寫完執行：`python board.py submit P0007`
  - 有紅色問題會被擋下，照列出的問題修好再送出
- 使用者問進度時執行：`python board.py status`
- 被退回時，`status` 會顯示退回原因；照原因修改 plan 後重新 submit
- 送出之後、核准之前可以修改再 submit；核准後不要再改這份 plan

### 不可以做的事

- 不要執行 `approve`、`reject`、`abandon`、`decide`、`stop`、`resume`、`cleanup`，這些只能由使用者執行
- 不要修改 `.board/` 底下的任何檔案
<!-- agent-board:end -->
