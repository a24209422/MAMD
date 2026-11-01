---
name: agent-board-init
description: 初始化目前專案的 agent-board，同時設定規劃與 Python TDD skill；用於首次設定、clone 換電腦或明確要求更新初始化設定。
disable-model-invocation: true
---

<!-- agent-board:init-skill -->

在使用者指定的目標專案根目錄，執行本 skill 隨附的 `scripts/init.py`。以你實際讀取此 SKILL.md 的目錄定位腳本，不假定 MAMD 安裝在固定路徑。優先使用可執行的專案 Python 3.11 以上，否則使用可用的系統 Python；不要假定 PATH 上名為 python 的命令一定可用，也不要為此修改系統設定。

```text
"<可用的 Python>" "<此 skill 目錄>/scripts/init.py" --project "<目標專案根目錄>"
```

這會設定 Codex 與 Claude 的 `agent-board-plan`、完整 `tdd-crap`、專案規則與可攜執行程式。Python 專案預設啟用 TDD；流程會使用或建立目標專案的 `.venv`，並安裝缺少的 pytest、coverage、radon。使用者指定其他專案 Python 時傳入 `--python`。

來源與測試位置由既有設定、pytest 設定與目錄判定。無法判定才詢問，再用 `--source`（可重複）、`--tests` 指定；不建立示意程式碼或改成略過測試來掩蓋錯誤。非 Python 專案先確認使用者的測試指令，再傳入 `--test-command`；只有使用者明確要求略過時使用 `--skip-tests`。既有一般測試設定改用 TDD 時，先確認使用者的選擇，再傳入 `--tdd`。

回報腳本實際結果、測試方式及需要納入 Git 的檔案。若失敗，依錯誤補齊資訊後重試；不可宣稱完成。遇到自訂同名 skill 時保留原檔，交由使用者決定如何處理。

## 換電腦交接

plan 與任務進度在本機 `.board/`，不進 Git；換電腦要靠交接檔 `.agent-board/progress.json`。下面的 board 指令依 `AGENTS.md` 所寫的路徑執行，一般專案是 `python "./.agent-board/runtime/board.py"`。這些步驟由使用者執行，agent 只說明，不替使用者 commit 或 push。

- 來源電腦：停止 Alfred → 把各 worktree 的成果都 commit → 在主專案資料夾執行 `export-state` → 依輸出 commit 交接檔與 plan 檔 → push 目前的分支與輸出列出的 plan／task 分支。
- 目的電腦：第一次 clone 後執行本 skill 初始化，會接納交接檔並回報「已接納交接進度」；之後只要 `git pull`，下次執行 `status`、開看板或啟動 Alfred 時就自動接納，不必重跑初始化。

回報時一併說明：進度以最近一次成功匯出且已 commit 的交接檔為準；Git 不搬移執行中的程序、終端機與 agent 對話；第一版不支援兩台電腦同時寫入。收到新交接檔時 Alfred 正在執行會提示停止 Alfred；本機與交接檔分歧時，由使用者執行 `state-sync` 選擇。

只在明確要求初始化時執行；writer 與 reviewer 不藉此重設專案、不呼叫規劃 skill，也不啟動多 agent 流程。不替使用者核准、commit 或 push。
