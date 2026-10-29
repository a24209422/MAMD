---
id: P0018
created: 2026-10-30
area: [後端/常駐 bot]
---

# 新增常駐 bot 模組：有名字、身分與長期記憶的 bot，同一個 bot 可以在多段對話之間共用記憶

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 如果我想要做這個（有名字、有身分、有記憶的常駐 agent，同一個 bot 可以開多個對話，分別處理不同專案），應該怎麼做，我想要一個 mvp | 做一個命令列 MVP：建立 bot（名字、角色、身分設定），列出與查看 bot，和某個 bot 在指定名稱的對話裡聊天；同一段對話接得上前文；bot 自己決定哪些事要長期記住，記憶存在 bot 的資料夾，同一個 bot 的所有對話都讀得到；第一版只支援 Claude Code CLI、只對話，不含工具、群組、排程與儀表板畫面 | D1 新增 modules/bot_house/：store.py 負責 bot 資料夾的建立、讀取與列出，cli.py 與 __main__.py 提供 `python -m modules.bot_house`；資料預設放在使用者家目錄的 .bot-house，可用 --home 或環境變數 BOT_HOUSE_HOME 指定；每個 bot 一個資料夾 bots/<名字>/，內含 profile.md（身分設定，範本見下方）、memory.md、chats.json、bot.json（cli、model、reasoningEffort，預設沿用 CLI 自己的預設）與 logs/；名字只接受英文字母、數字、- 和 _，長度 1～40，重複時拒絕；指令 `new <名字> --role <角色>`、`list`、`show <名字>`（印出身分設定、記憶條數與對話清單） | 放在專案的 .board/ 或 repo 裡：bot 的記憶是個人資料，換專案也要用得到，不該進 git；用資料庫：MVP 用檔案就能直接打開來看和改，之後 Dr. Eggbot 也容易讀 | T1 建立 bot_house 模組骨架與 bot 資料夾，提供 new、list、show 指令與名字檢查，補 tests/test_bot_house_store.py | | 執行 python -m pytest -q 通過；執行 `python -m modules.bot_house --home <暫存資料夾> new matcha --role 工程主管`，資料夾裡出現 profile.md、memory.md、chats.json、bot.json；`list` 列出 matcha；再建一次同名 bot 會被拒絕；名字含空白或斜線會被拒絕 |
| R1 | | D2 新增 modules/bot_house/runner.py，用 Claude Code CLI 執行一則訊息：參數組法沿用根目錄 board_agents.py 的 resolveCommand、modelArgs、reasoningArgs、parseClaudeLine、claudeFinalResult；以 `-p`、`--output-format stream-json`、`--json-schema` 要求回覆 `{"reply": 字串, "remember": 字串陣列}`；每次呼叫都用 `--append-system-prompt` 帶入 profile.md、當下的 memory.md 與「記憶規則」，所以其他對話剛寫入的記憶下一則訊息就生效；對話的第一則訊息用 `--session-id` 搭配新的 uuid，之後用 `--resume`，對話名稱對應的 session 記在 chats.json；比照 reviewer 用 `--disallowedTools` 拿掉 Edit、Write、NotebookEdit、Bash、PowerShell，第一版只對話、不動檔案；每輪把時間、使用者訊息、回覆與 remember 附加到 logs/<對話名稱>.jsonl；指令 `chat <名字> --chat <對話名稱> [訊息]`：有給訊息就回答一次後結束，沒給就進入互動模式，輸入空白行結束；CLI 失敗、輸出不符 schema 或額度用完時印出錯誤，不更新 chats.json 也不寫記錄 | 讓 bot 用 Edit 工具自己改 memory.md：要開放寫檔權限，Claude Code 路徑權限規則的比對方式也不確定；改用 schema 回傳要記的內容、由程式寫入比較好控制；把身分寫成 bot 資料夾的 CLAUDE.md 讓 CLI 自己讀：不確定 resume 時會不會重讀，每次 append 才確定拿到最新的記憶；直接呼叫 Anthropic API：要另外管理 API key，CLI 沿用已登入的訂閱 | T2 和 bot 對話：chat 指令、Claude CLI 呼叫、session 接續與對話記錄，用假 CLI 補 tests/test_bot_house_runner.py | T1 | 執行 python -m pytest -q 通過；用假 CLI 的測試確認：同一段對話的第二則訊息用 `--resume` 接同一個 session，不同對話用不同 session；append 的系統提示含 profile 與 memory；禁用的工具都在參數裡；CLI 失敗時不寫 chats.json 與記錄 |
| R1 | | D3 store.py 收到 remember 時附加到 memory.md，每條一行「- 內容（YYYY-MM-DD，來自對話 <名稱>）」；去掉前後空白後內容相同的不重複寫入；memory.md 超過 8000 字元時，chat 結束前提示「記憶已經很長，之後可以用 Dr. Eggbot 整理」；附在系統提示的記憶規則：只記長期有效的偏好、事實與約定，不記一次性的任務細節，沒有要記的就回傳空陣列 | 每輪另外呼叫一次模型整理記憶：用量加倍；向量資料庫：MVP 的記憶量小，整份帶進 prompt 就夠 | T3 長期記憶：remember 寫入 memory.md、去除重複、長度提示，補 tests/test_bot_house_memory.py | T2 | 執行 python -m pytest -q 通過；實際操作：在 matcha 的「專案A」對話說「之後回覆一律用條列」，再到「專案B」對話問任何問題，回覆是條列；memory.md 看得到這一條和它來自的對話 |
| R1 | | D4 新增 modules/bot_house/README.md：用途、資料放在哪裡、各個指令、身分設定範本、記憶規則、限制（只支援 Claude、沒有工具、群組、排程與畫面），以及刪除 bot 的方法（直接刪掉資料夾）；主 README 加一行指向它 | 只寫在主 README：模組的說明會越來越長，放在模組裡比照 system_monitor 的做法 | T4 撰寫 bot_house 模組的 README，主 README 加上連結 | T3 | 照 modules/bot_house/README.md 的步驟，可以從零建立一個 bot、在兩段對話裡聊天，並在 memory.md 看到它記住的事 |
| R2 三件都寫：後兩件做成 MAMD 的 modules/ 模組（像 system_monitor 那樣） | 程式與說明集中在 modules/bot_house，主系統（conductor、server、儀表板）不改；測試檔放在 tests/ 底下，Alfred 的測試指令才會收集到 | D1 | | T1 | | 執行 git diff --stat 檢查整份 plan 的改動，只在 modules/bot_house/、tests/ 與主 README 的一行連結 |

## 身分設定範本

`new` 建立的 profile.md 固定有下列段落，`new` 只填入名字與角色，其他段落先寫「（待補）」，之後可以自己編輯，或交給 P0019 的 Dr. Eggbot 產生：

```text
# <名字>
角色：<一句話>

## 負責的事
## 不負責的事
## 交付格式
## 怎麼確認自己做對了
## 規則
## 固定流程
```

「規則」與「固定流程」是給 Dr. Eggbot 套用建議時附加內容用的，一開始是空的。

## 背景

- 需求來自使用者在對話中，針對 Peter Yang 訪談 Peng Zheng 與 Lauren Tan 那一集的提問。Peng 的設計重點是：bot 是有名字、有身分、有記憶的常駐 agent；能力（工具、skill）共用，記憶與偏好每個 bot 各自保存；同一個 bot 可以開多段對話處理不同專案。
- 這是 P0019（Dr. Eggbot）的基礎：Eggbot 要讀 bot 的身分設定、記憶與對話記錄。
- pytest.ini 與 Alfred 的測試指令只收集 tests/ 與 modules/system_monitor/tests，所以這份 plan 的測試放在 tests/test_bot_house_*.py。
- 新模組的 .py 不在目前品質檢查的 --source 清單裡，品質門檻不會檢查到；合併後想要涵蓋，由使用者重跑 agent-board-init 更新設定。

## 範圍

- 不包含：群組對話、bot 之間互相傳話、routine（排程）、工具與共用 skill、儀表板頁面、Codex CLI、刪除與改名指令。
- 不修改 conductor.py、server.py、web/；只從 board_agents.py 引用參數組法的函式，不改動它。
