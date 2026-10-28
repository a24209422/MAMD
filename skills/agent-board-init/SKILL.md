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

只在明確要求初始化時執行；writer 與 reviewer 不藉此重設專案、不呼叫規劃 skill，也不啟動多 agent 流程。不替使用者核准、commit 或 push。
