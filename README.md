# agent-board

你對 Codex 或 Claude 說需求 → 它寫 plan → 你核准 → 寫程式的 agent 寫程式 → 審查的 agent 審查 → 不通過自動退回，最多審 3 次。

- 寫程式、審查各自可以用 Claude 或 Codex 的任意模型（見「換模型」）
- 每份 plan 是一張追溯矩陣（需求 → 做法 → 任務 → 驗證），送出時自動檢查有沒有斷線
- 任務在各自的 git worktree 裡平行進行，合併衝突由寫程式的 agent 自動解、再送審
- 瀏覽器儀表板可以看即時狀態，也能在網頁上核准、退回和處理卡住的任務

## 需求

- Python 3.11 以上（在 3.12、3.13 測過）；主程式使用標準函式庫，TDD 初始化會安裝測試套件
- git
- [Claude Code](https://claude.com/claude-code) 和 [Codex CLI](https://github.com/openai/codex)，都要先登入
- 目前只在 Windows 測過

## 安裝

```
git clone https://github.com/a24209422/MAMD.git
```

下面的 `<agent-board>` 代表 clone 下來的資料夾，例如 `D:\tools\agent-board`。

clone 後，在這個資料夾開啟 Codex 或 Claude Code，即可發現隨 Git 交付的 `agent-board-init` skill。初始化一次完成以下設定，不必另外安裝全域 TDD skill：

- Codex 的 `.agents/skills/` 與 Claude 的 `.claude/skills/` 都有 `agent-board-init`、`agent-board-plan`、完整的 `tdd-crap`。
- 更新 `AGENTS.md` 的 agent-board 區塊，保留專案自己的其他規則。
- Python 專案預設啟用 TDD，使用或建立專案 `.venv`，安裝缺少的 pytest、coverage、radon。
- 把必要的執行程式、技能資源與初始化偏好帶進目標專案，讓另一台電腦可由 clone 重新初始化。

| 使用工具 | 初始化目前專案 | 將需求寫成 plan |
|---|---|---|
| Codex CLI／IDE | `$agent-board-init` | `$agent-board-plan 你的需求` |
| Claude Code | `/agent-board-init` | `/agent-board-plan 你的需求` |

這些是 AI 對話中的 skill 呼叫，不是預先安裝的 PowerShell 命令。初始化與規劃 skill 均設為明確呼叫，writer／reviewer 不會自動切回規劃或重設專案。若技能清單尚未更新，重新開啟 agent。

### 在其他專案首次設定

新專案尚未帶有初始化入口時，先在 clone 的 MAMD 資料夾開啟 agent，呼叫初始化 skill 並指定目標。例如在 Codex 輸入：

```text
$agent-board-init 請初始化 D:\projects\my-app
```

目標專案需為 Git repo，且至少有一個 commit。正常的 Python 專案不需要額外指定 `--tdd`；來源優先辨識 `src/`、Python 套件或根目錄的 `.py`，測試讀取 pytest 設定及既有測試位置。無法判定或既有設定衝突時，agent 才會詢問必要資訊。

初始化後，將下列內容一起提交到你自己的專案：

```text
.agents/skills/                 # Codex 的三個 skill
.claude/skills/                 # Claude 的三個 skill
.agent-board/setup.json        # 可攜的來源、測試位置與測試方式
.agent-board/runtime/          # 目標專案使用的執行程式與資源副本
AGENTS.md
.gitignore
```

MAMD 自己已有完整執行程式，不需要再複製一份 runtime。目標專案的 runtime 副本不包含原 repo 的 `.git`、測試、虛擬環境或私人的設定。

在其他電腦 clone 這份已初始化的專案後，在該專案開 agent，再呼叫同一個 `agent-board-init`。它會從隨專案交付的資源重建 `.venv` 與 `.board/config.json`，不需要原電腦的磁碟路徑或全域 skill。首次安裝套件需要網路；失敗會回報未完成，修正後可重跑。

`.venv/`、`.board/` 與測試快取不進 Git。角色、模型等既有本機設定會保留；初始化不會替你 commit、push、核准 plan 或啟動 Alfred。若 Alfred 已在執行，變更測試設定後須重啟。

### 初始化的進階選項

skill 使用的腳本也可直接執行，以下指令從目標專案根目錄執行：

```powershell
python .agents/skills/agent-board-init/scripts/init.py
python .agents/skills/agent-board-init/scripts/init.py --source app --tests tests
python .agents/skills/agent-board-init/scripts/init.py --python .venv/Scripts/python.exe
```

`--source` 可重複，也可指向單一 `.py`，支援平鋪專案；不接受把整個專案根目錄當成來源。非 Python 專案仍可設定兩個 skill，但要明確提供自己的測試指令：

```powershell
python .agents/skills/agent-board-init/scripts/init.py --test-command "npm test"
```

只有明確要略過自動測試時使用 `--skip-tests`。既有一般測試設定要改成 Python TDD 時使用 `--tdd`；原設定不會在未明確選擇時被覆蓋。這三個選項互斥。自訂同名 skill 會保留並回報衝突，不會被直接覆蓋。

工具更新時，在 MAMD 執行 `git pull`，再從更新後的 MAMD 呼叫初始化 skill、指定原目標專案，即可更新本工具管理的副本。

## 舊版 board.py init（相容保留）

以下低階指令仍可使用；日常設定建議使用上面的 `agent-board-init`。

1. 專案要是 git repo，而且至少有一個 commit
2. 在專案資料夾執行：

   ```
   python <agent-board>\board.py init --test-command "python -m pytest -q"
   ```

   - 把 `docs/AGENTS-snippet.md` 的規則寫進專案的 `AGENTS.md`，前後加標記；規則更新後再跑一次 `init`，會整段換成最新版
   - 規則裡寫 plan 的 agent 要執行的 `board.py` 路徑，會寫成這份 `<agent-board>` 的實際位置；`<agent-board>` 搬家後要再跑一次 `init`
   - 建立 `.claude/skills/agent-board-plan/SKILL.md`，內容是同一份規則：在 Claude Code 打 `/agent-board-plan 你的需求`，就由 Claude 寫 plan；注意：Claude Code 平常也會自己讀 `AGENTS.md`，所以在這個專案裡開的 Claude Code，即使沒打這個指令，也會看到「負責寫 plan、不要自己寫程式」那段規則；要它直接寫程式時請明講。已經有同名、但不是 init 產生的 skill 時不覆蓋
   - `.gitignore` 補上 `.board/`、`plans/_review/`
   - 不會自動 commit，最後會印出要執行的 git 指令
   - 測試產物（例如 `__pycache__/`、`.pytest_cache/`）要自己加進 `.gitignore`，否則會被 commit 進任務分支
   - `AGENTS.md` 以前手動貼過規則的話，要先刪掉那一段再跑

   `--test-command` 使用專案自己的指令，例如 `npm test` 或 `cargo test`，不限定 Python。
   新專案要選擇測試指令、TDD 或明確略過；沒有選擇時會先列出用法，不修改任何檔案。
   已經有測試指令或明確略過設定的專案，重跑普通 `init` 會沿用既有選擇；
   明確傳入 `--test-command` 會更新該指令並保留模型等其他設定，不會覆蓋已啟用的 TDD 品質檢查。
   沒有自動測試的專案使用 `python <agent-board>\board.py init --skip-tests`，
   它會保存 `skipTests: true`，writer 也會收到「未執行測試，不宣稱通過」的說明。
   `--test-command`、`--skip-tests`、`--tdd` 三者互斥。

   Python 專案要讓寫程式的 agent 照 tdd-crap skill 寫程式時，改用：

   ```
   python <agent-board>\board.py init --tdd --source src --tests tests
   ```

   - 此舊版入口仍使用全域 skill，需先執行 `python <agent-board>\install_skill.py`，安裝至 `~/.claude/skills/tdd-crap`；已有同名自訂 skill 時不覆蓋
   - `.board/config.json` 會開啟 `tddCrap`，`testCommand` 設成 tdd-crap 的品質檢查（每個函式的覆蓋率、循環複雜度）
   - 寫程式的 agent 照 tdd-crap 的流程寫（Claude 用 Skill 工具載入，Codex 直接讀 skill 檔），開發時用 `python -m pytest` 只跑相關的測試；完整的品質檢查由 Alfred 跑（見「測試把關」），writer 不必自己跑；審查時另外檢查測試有沒有真的在驗證行為
   - 專案的 Python 要裝好 pytest、coverage、radon；沒裝時 `init` 會印出安裝指令
   - `--source` 可以重複；沒指定時是 `src`、`tests`

3. 初始化會保存測試方式。需要其他選項時，編輯 `.board/config.json`，保留初始化寫入的設定：

   ```json
   { "maxParallel": 3, "maxRounds": 3, "timeoutMinutes": 30, "testCommand": "python -m pytest -q" }
   ```

   寫程式的 agent 是 Claude 時，預設只能執行 `testCommand` 這一個 shell 指令（其他都會被拒絕）。需要其他指令時，加進 `writer.allowedTools`，建議 Bash 和 PowerShell 兩種寫法都列。Claude writer 跑測試時一律用 Bash 工具：Windows 上 PowerShell 的 `& "..."` 呼叫比對不到允許清單，會被自動拒絕：

   ```json
   { "testCommand": "python -m pytest -q", "writer": { "cli": "claude", "allowedTools": ["Bash(npm install*)", "PowerShell(npm install*)"] } }
   ```

   - `maxParallel` 是寫程式、審查各自的總上限，包含 1 個主要 agent；預設 3 代表每角色「1 主要 + 最多 2 子」。任務少時優先使用主要 agent，主要忙碌且仍有可執行任務才啟動子 agent，任務完成後結束該次執行。設 1 就只使用主要 agent
   - `maxParallelTests` 是同時最多跑幾份完整的 `testCommand`，基準、合併後測試和任務測試合計，預設 1（一次只跑一份）。同時跑好幾份時每份都變慢，瀏覽器測試也更容易不穩定；電腦核心多、記憶體夠時才調高。名額滿時其他測試排隊等待（見「測試把關」）。改了要重開 Alfred 才生效
   - 總覽第二層的大卡是 `writer`／`reviewer` 主要 agent，直接顯示自己的任務、Context 與狀態；第三層只放子 agent，小卡顯示「未啟動」代表目前沒有執行程序。主要與子卡都能點進各自的控制台
   - `writer`、`reviewer` 兩段（例如任務卡住後才加 `allowedTools`）改了之後，Alfred（指揮程式，檔名是 `conductor.py`）從下一個新啟動的 agent 開始用；其他設定改了要重開 Alfred 才生效
   - 完整的 `testCommand` 在背景執行，不擋派工、合併和審查，同時跑的份數受 `maxParallelTests` 限制；新核准的 plan 在任務合併後才跑（狀態「已合併待測試」），改版前核准的 plan 在任務寫完後跑（狀態「測試中」），總覽的 Alfred 卡片會列出這些任務（見「測試把關」）
   - plan 開始時的基準測試也在背景執行，跑基準時 Alfred 照常處理其他 plan 的派工、合併和審查（見「測試把關」）
   - agent 交出最終結果後，如果它開的背景程式（例如被轉到背景的測試）讓 CLI 不退出，Alfred 等 10 秒寬限時間就結束它留下的整個程式樹，照常 commit、送測試或寫審查結果，不算中斷
   - Alfred 啟動前會檢查測試方式：沒有非空白的 `testCommand`，也沒有 `skipTests: true` 時，會先停止並提示初始化選項，不會等到 writer 執行測試才卡住
   - `testCommand`／`skipTests` 執行中改動時會提示重啟 Alfred；目前執行仍沿用啟動時的指令與權限。每個 writer（含解衝突）都會收到 Alfred 跑的同一個測試指令：Claude writer 收到 Bash 寫法（`testCommand`、`pythonCommand`），Codex writer 在 Windows 上收到 PowerShell 寫法（`writerTestCommand`、`writerPythonCommand`）
   - 寫程式、審查要用哪個 CLI 和模型，見「換模型」

## 換模型

寫程式（`writer`）和審查（`reviewer`）各自可以選 Claude 或 Codex，以及要用的模型，寫在 `.board/config.json`：

```json
{
  "writer":   { "cli": "codex",  "model": "gpt-6.1-sol", "reasoningEffort": "high" },
  "reviewer": { "cli": "claude", "model": "opus", "reasoningEffort": "high" }
}
```

| 欄位 | 說明 | 沒寫時 |
|---|---|---|
| `cli` | `claude` 或 `codex` | 寫程式 `claude`、審查 `codex` |
| `model` | 交給 CLI 的模型名稱（Claude 用 `--model`，Codex 用 `-m`） | 用 CLI 自己的預設模型 |
| `reasoningEffort` | 模型支援的推理強度（Claude 用 `--effort`，Codex 用 `-c model_reasoning_effort=…`） | 沿用 CLI 設定 |
| `contextWindow` | 這個模型的 Context 大小（正整數 tokens），用於儀表板使用率與自動壓縮門檻 | Claude 200000、Codex 475000 |
| `allowedTools` | 寫程式的 Claude 可以多跑的指令，見「在專案裡啟用」 | 沒有 |
| `command` | CLI 不在 PATH 上時寫完整路徑，例如 `["C:/tools/codex.cmd"]` | PATH 上的 `claude`、`codex` |

- 兩個都不寫時：Claude 寫程式、Codex 審查，用各自的預設模型
- 同時跑的 agent 身分（`writer`、`writer-sub1`…、`reviewer`…）都用所屬角色的設定
- writer 與 reviewer 都啟用原生自動壓縮，包含 writer 的續接、修正與解衝突，以及 reviewer 的重審。Codex 在角色 `contextWindow` 的 60% tokens 觸發；Claude 設定壓縮視窗並使用 60% 門檻，CLI 會先扣除自身保留空間，因此可能比儀表板的 60% 稍早觸發。壓縮在 CLI 可處理的檢查點進行，完成後繼續原任務，不另開對話、不消耗任務重試輪數。
- `contextWindow` 須配合實際模型設定，不能把 Claude 200000、Codex 475000 的預設值當成所有模型的大小。Claude 的壓縮視窗支援 100000 到 1000000 tokens；超出範圍會明確回報啟動失敗。Codex 不會透過放大模型實際視窗來達成門檻。
- 壓縮設定只傳給 MAMD 啟動的程序，會啟用該次 Claude 對話的自動壓縮，並覆寫同名壓縮／停用環境變數，不修改全域 CLI 設定或你另開的終端機。需要支援 `--autocompact` 的 Claude Code 及支援 `model_auto_compact_token_limit`／`model_auto_compact_token_limit_scope` 的 Codex CLI；舊版可能不支援，請先更新。合併這次程式修改後需重啟 Alfred；已在執行的 agent 不會即時變更。
- 原生設定參考：[Claude 自動壓縮](https://code.claude.com/docs/en/model-config#context-window-and-auto-compaction)、[Codex 設定](https://learn.chatgpt.com/docs/config-file/config-reference)。目前儀表板的 Codex usage 是整輪統計，不能作為即時 context 或壓縮成功的唯一證據。
- 儀表板的「模型」分頁可選 `cli`、模型與推理強度，選單直接讀本機 CLI：Codex 使用 app-server 的 `model/list`，Claude 使用 SDK 初始化回傳的模型資料。不傳送對話、不產生推理任務，也不需要額外的 API key
- 模型名稱、版本與順序跟著 CLI；Claude 別名另顯示目前對應的完整模型 ID。只列出該模型支援的推理強度，儲存前也會檢查；切換 CLI 或模型時，推理強度會重設為沿用 CLI 設定
- 清單快取五分鐘，可按「重新整理模型」立即重讀；CLI 未安裝、未登入、不支援查詢或查詢逾時時會顯示錯誤。清單反映 CLI 提供的選項，實際使用權限仍由 CLI／供應商決定
- 角色有自訂 `command` 時，查詢會使用同一個指令；要換 CLI 得先改設定檔。其他欄位仍需改 `.board/config.json`
- Alfred 每一輪都會重讀 `writer`、`reviewer`：改了之後從下一個新啟動的 agent 開始用，正在跑的不受影響。其他設定（`maxParallel`、`maxParallelTests`、`testCommand` 等）改了要重開 Alfred
- 換了寫程式的 CLI 或模型之後，進行中的任務下一輪會開新對話，接不回上一輪
- 權限：
  - Claude 寫程式：只能執行 `testCommand` 和 `allowedTools` 列的指令
  - Codex 寫程式：在 `workspace-write` 沙箱裡，只能寫入這個任務的 worktree，但不能限制它執行哪些指令
  - Claude 審查：會改檔案、執行指令的內建工具（Edit、Write、NotebookEdit、Bash、PowerShell）都被拿掉；你在 Claude Code 設定裡另外放行的工具（例如 MCP 工具）仍可能可用
  - Codex 審查：`read-only` 沙箱
- 寫 plan 的 agent 是你自己開的，模型在開的時候選：`codex -m <模型>`，或 `claude --model <模型>` 再打 `/agent-board-plan`
- 舊版的 `claudeCommand`、`codexCommand`、`claudeAllowedTools`、`claudeContextWindow`、`codexContextWindow` 已經不用了，Alfred 看到會拒絕啟動並說明要改成什麼

## 使用

1. 開一個終端機跑 Alfred（`conductor.py`），它會即時印出每個事件：

   ```
   python <agent-board>\conductor.py <專案資料夾>
   ```

   一個專案同時只能跑一個 Alfred，第二個會拒絕啟動。

2. 在專案資料夾開 `codex` 說你的需求；或開 `claude`，打 `/agent-board-plan 你的需求`。寫完 plan 會自己送出。
   - 送出通過檢查時會自動 commit 這份 plan 檔，而且只 commit 這一個檔案，主資料夾其他已暫存或沒暫存的改動都不動；plan 檔跟目前的 commit 一樣時不另外 commit
   - commit 失敗（例如 git hook 擋下）時不會送出，會印出 git 的錯誤和要自己執行的 `git add`、`git commit` 指令（Windows 是 PowerShell 寫法，其他系統是 sh 寫法；commit 訊息寫在 `.board/commit-message-<plan 編號>.txt`，用 `git commit -F` 讀），自己 commit 之後再送出就會通過
   - 送出後 plan 檔又被改過、跟目前的 commit 不同時不能核准，要重新送出
   - plan 裡標題和追溯矩陣以外的文字（例如表格下方的規則表、契約、對照值）會整段附給每個任務寫程式、解衝突、審查的 agent，接在這個任務的表格列後面；超過 30000 個字元時只附前 30000 個字元並註明
3. 看線路圖：`python <agent-board>\board.py --project <專案資料夾> index`，打開 `plans/_review/index.html`
4. 核准：`python <agent-board>\board.py --project <專案資料夾> approve P0007`，或在儀表板上按「核准」
5. 之後看 Alfred 的終端機，或跑 `status`。完成後 `plan/P0007` 分支裡就是所有任務的成果，要不要合併到你的分支由你決定。跑 `stats P0007` 可以看這份 plan 的平均輪數、第一次審查就通過的比例等成效（見「指令」）。

### 測試把關

有設定 `testCommand`（而且沒有 `skipTests`）時，Alfred 會跑完整測試把關。驗證分兩層：writer 開發時只跑跟改動相關的測試，幾十秒內看到結果、同一輪自己修；完整的 `testCommand`（tdd-crap 專案是品質檢查）只由 Alfred 跑，writer 不自己跑，也不用背景執行跑測試。

新核准的 plan 走「合併後測試」：寫完直接送審，審查通過就合併，完整測試在合併後對 plan 分支跑，失敗時找出是哪次合併、只退回那個任務。改版前核准的 plan（核准事件沒有 `testTiming`）照原流程：寫完先測試，測試沒過不送審，見下面「原流程」。

基準、合併後測試和任務測試都是完整測試，合計同時最多跑 `maxParallelTests` 份（預設 1）。名額滿時，新的測試不會開始，排隊等前面的跑完：要基準的 plan 先不派工，合併後測試的任務停在「已合併待測試」，原流程的任務停在「測試中」等待，等待時不寫事件。有空位時先跑基準，任務測試依進入「測試中」的先後開始。沿用之前結果的基準不跑測試，不佔名額。

1. plan 核准後、派出第一個任務前，Alfred 先在 baseCommit 跑一次 `testCommand` 當基準。基準在暫時的 worktree（`<專案>-worktrees/P0007-baseline`）裡跑，跑完就移除；基準記下來之前這份 plan 不派工。基準在背景執行，不擋其他 plan。基準跑到一半時放棄這份 plan 或執行 `stop`，Alfred 會在下一輪就結束測試的整個程式樹並移除基準 worktree，不必等整套測試跑完；Ctrl+C 結束 Alfred 時也會一併結束基準。`stop` 之後 `resume`（或重開 Alfred）會重跑基準。基準在跑之前，Alfred 會先找之前記下的結果能不能沿用，來源是之前的基準，或任務寫完後跑的任務測試（原流程的「測試中」；改版前的紀錄沒有記被測的 commit、完整的失敗名單和測試指令，不能沿用），不論來源 plan 已經完成或放棄。被測的 commit 跟這份 plan 的 baseCommit 之間只差 `plans/` 底下的檔案（例如 submit 自動 commit 的 plan 檔），而且 `testCommand` 相同，就直接沿用那次的結果當基準，不建 worktree、不重跑，事件會標出沿用哪份 plan、哪個任務的結果。所以前一份 plan 最後測過的程式碼沒有再改動時，合併回 main 後馬上核准的下一份 plan 會沿用那次的任務測試結果；合併時混進其他程式改動，或最後的組合沒有測過時，一樣要重跑。另一份 plan 正在跑程式碼相同的基準時，先等它跑完再沿用，同時需要的基準只跑一次
2. 任務寫完直接送審，審查通過就合併進 plan 分支，狀態變成「已合併待測試」，列在總覽 Alfred 卡片的「已合併待測試」那一行。依賴它的任務這時就能開始寫，不等測試
3. Alfred 在暫時的 worktree（`<專案>-worktrees/P0007-mergetest`）測 plan 分支最新的 commit，一次涵蓋所有還沒測過的合併，跟上一個測過的版本比（第一次跟基準比，規則見下面「測試判定」）。每份 plan 同時最多一個合併後測試；測試期間才合併的任務留給下一次。暫停、放棄這份 plan、重開 Alfred 時，跑到一半的測試會結束、worktree 會移除，之後重跑
4. 沒有新增的失敗：涵蓋到的任務全部完成。最後一個任務測過，plan 才算完成
5. 有新增的失敗：只涵蓋一次合併時，退回那次合併的任務，算一輪。涵蓋好幾次合併時先不退回，照合併順序逐一測每次合併的 commit、每次跟前一個比，失敗從哪次合併開始出現，就只退回那個任務；最後一次合併的 commit 已經測過，直接沿用結果、不重跑。兩個任務各自沒問題、合在一起才壞時，會算在後合併的那個任務
6. 被退回的任務：下一輪開始前，任務分支先快轉到最新的 plan 分支（包含其他任務已經合併的改動），並接續上一輪的 writer 對話（CLI 和模型沒換時）。它會收到失敗的測試名稱、測到的 commit 和測試輸出最後 50 行；修好之後照常審查、合併，由下一次合併後測試確認，上次的失敗要消失才算完成。修正輪又被審查退回時，審查意見和還要修的失敗都會附上。輪數用完時停在「等你處理」，見「指令」的 `decide ... retry` 表
7. 分不出結果（超時、輸出分不出失敗名稱）：不自動退回，涵蓋到的任務停在「等你處理」，這份 plan 先不跑合併後測試，等你決定：`retry` 重測、不算輪數；`done` 直接完成；`abandon` 放棄（改動已經在 plan 分支裡，不會移除）
8. 審查的 agent 不會收到自動測試結果；它會被告知完整測試在合併後才跑、驗證欄裡的完整測試不是審查要確認的項目，修正輪另外列出要修的失敗

- 對任務選 `decide ... done`（例如合併衝突你自己處理完）時照原流程直接完成，不再跑合併後測試
- `testCommand` 改掉並重開 Alfred 後，進行中的 plan 會用新指令重跑一次基準，之後的合併後測試改跟新的基準比
- 基準、合併後測試的 worktree 因故沒移除時，下次建立前會先移除，`cleanup` 也會一併清掉
- `skipTests` 或沒有 `testCommand` 時不跑基準也不跑完整測試，審查通過合併後直接完成；任務已經是「已合併待測試」才改成這樣並重開 Alfred 時，這些任務也直接完成，事件記一筆「略過合併後測試」

#### 原流程（改版前核准的 plan）

1. 每個任務寫完後，狀態先變成「測試中」，`testCommand` 在任務的 worktree 背景跑，結果跟基準比（規則見下面「測試判定」）；結果出來才變成待審查或退回。測試名額滿時任務會停在「測試中」等待，有空位才開始跑（見上面 `maxParallelTests` 的說明）。測試中的任務會列在總覽 Alfred 卡片的「測試中」那一行，等待中的也一樣列在這裡。暫停或重開 Alfred 時，跑到一半的測試會結束，繼續或重開後重跑
2. 有新增的失敗：不叫審查的 agent，直接退回寫程式的 agent，算一輪。下一輪它會收到新增失敗的測試名稱和測試輸出最後 50 行，不附更早的審查意見。輪數用完時停在「等你處理」，見「指令」的 `decide ... retry` 表
3. 失敗都是基準本來就有的：照常送審，審查的 agent 會被告知這些失敗基準就有，不必因此退回

#### 測試判定

失敗名稱有兩種，都從測試完整輸出裡取：

- pytest：以 `FAILED ` 或 `ERROR ` 開頭的行（「 - 」之前的部分），也就是 pytest 結尾 short test summary 的格式
- 品質門檻（tdd-crap 專案的品質檢查）：品質檢查每個函式印一行 `PASS` 或 `FAIL`，每個 `FAIL`（超標）的函式就是一個失敗名稱，格式是 `品質門檻 檔案::函式`，例如 `品質門檻 conductor.py::compareWithBaseline`。名稱不含行號：函式上面多一行，行號就變了，含行號就跟基準比不起來。品質檢查最後一行 `FAILED: ...report.json` 因為冒號緊接在 `FAILED` 後面，不算名稱

品質檢查只在 pytest 全部通過時才計算覆蓋率、印出 `PASS`／`FAIL` 行；pytest 失敗或超時時只有 pytest 的失敗名稱，品質沒有計算。兩種名稱分開跟基準比，比法一樣（名稱不在基準裡就是新增），再合併：任一種有新增就退回。基準或任務測試有一方品質沒有計算時，品質超標不算新增，因為沒有東西可比。

| 基準 | 任務測試 | 接下來 |
|---|---|---|
| 任何 | 通過 | 送審 |
| 通過 | 失敗（含超時） | 退回寫程式的 agent |
| 失敗，有失敗名稱 | 失敗，有不在基準裡的名稱，或一個名稱都沒有 | 退回寫程式的 agent |
| 失敗，有失敗名稱 | 失敗，名稱全都在基準裡 | 送審，審查的 agent 會看到基準就有的失敗 |
| pytest 全過，只有品質超標 | pytest 全過，品質超標的函式全都在基準裡 | 送審，審查的 agent 會看到基準就有的品質超標 |
| pytest 全過，只有品質超標 | pytest 失敗，或有基準沒有的品質超標 | 退回寫程式的 agent |
| pytest 失敗（品質沒有計算） | pytest 全過，只有品質超標 | 送審，品質超標不算新增；審查的 agent 會被告知基準就失敗、無法比對 |
| 失敗，沒有失敗名稱（輸出分不出名稱，或超時） | 失敗 | 送審，審查的 agent 會被告知基準就失敗、無法比對 |

- pytest 的 `-r` 要保留 `f`（失敗）和 `E`（錯誤），short test summary 才會列出 `FAILED`／`ERROR` 測試名稱，才分得出新增的失敗。pytest 沒設 `-r` 時預設就是 `fE`；在 `addopts` 自己設了 `-r`（例如只有 `-rs`）就會蓋掉預設，要寫成 `-rfEs` 這種同時含 `f`、`E` 的寫法（本專案的 `pytest.ini` 就是 `-rfEs`）
- pytest 以外的測試工具分不出名稱：基準通過時，任務測試失敗一律退回；基準失敗時只能送審並說明無法比對

合併後測試用同一套比法，比較對象換成上一個測過的版本（第一次是基準）。上一個版本已經有的失敗（包括之前退回別的任務的失敗）不算新增：

| 合併後測試 | 涵蓋 | 接下來 |
|---|---|---|
| 通過，或失敗名稱全都在上一個版本裡 | 任何 | 涵蓋到的任務完成；上次退回這個任務的失敗名稱還在時，再退回它 |
| 有不在上一個版本裡的名稱 | 一次合併 | 退回那次合併的任務 |
| 有不在上一個版本裡的名稱 | 好幾次合併 | 照合併順序逐一測，找出從哪次合併開始失敗 |
| 失敗但一個名稱都分不出來（含超時），或上一個版本失敗又沒有名稱 | 任何 | 涵蓋到的任務停在「等你處理」 |

## 儀表板

在瀏覽器看即時狀態，也可以在網頁上核准、退回、放棄、`decide`、停止和繼續，以及在「模型」分頁換寫程式、審查的 CLI 和模型（見「換模型」）。「任務」頁選了核准過的 plan 時，底下的「成效」區塊顯示和 `stats P0007` 同一組指標；待核准或草稿的 plan 沒有這個區塊。

1. 在你自己開的終端機執行（不能讓 agent 啟動：網址裡的 token 等於核准權限）：

   ```
   python <agent-board>\server.py <專案資料夾> [--port 8765]
   ```

2. 用它印出的網址開瀏覽器（`http://127.0.0.1:8765/#token=...`）。沒帶 token 的網址只能看，不能操作
3. server 重開後 token 會換，要用新印出的網址重開
4. 可以和 Alfred 同時開；同時看兩個專案就用不同的 `--port`

不花用量的展示：`python <agent-board>\tests\fakes\make_demo.py <新的資料夾>`，照它印出的步驟做。

## 指令（在專案資料夾執行時可以省略 `--project`）

| 指令 | 做什麼 |
|---|---|
| `status [P0007]` | 看進度 |
| `stats [P0007]` | 看成效：不帶 plan 時每份核准過的 plan 一列加上總計，帶 plan 時列出全部指標和每個任務的輪數 |
| `index` | 產生總覽頁與線路圖 |
| `init [--test-command "指令" / --skip-tests / --tdd]` | 在專案啟用 agent-board 並明確選擇測試方式；TDD 可搭配 --source、--tests |
| `approve P0007` | 核准（有橘、黃問題時會先問你） |
| `reject P0007 --reason "..."` | 退回 plan |
| `abandon P0007` | 放棄 plan，正在跑的 agent 會被結束 |
| `decide P0007 T2 retry --note "..."` | 等你處理的任務：再跑一輪 |
| `decide P0007 T2 done` | 等你處理的任務：你已在 worktree 改好，直接合併 |
| `decide P0007 T2 abandon` | 等你處理的任務：放棄 |
| `stop`／`resume` | 全部停止／繼續 |
| `cleanup P0007` | 刪除結束的 plan 的 worktree 與任務分支（`plan/P0007` 保留） |

`approve`、`reject`、`abandon`、`decide`、`stop`、`resume`、`cleanup` 只能由你在終端機執行，agent 執行會被拒絕。

`stats` 只讀 `.board/events.jsonl` 和推導出的狀態，不另外記錄資料，舊的 plan 也算得出來：

| 指標 | 算法 |
|---|---|
| 任務 | 完成、放棄、全部 |
| 平均輪數 | 每個任務事件裡最大輪數的平均；還沒開始的任務不算 |
| 寫完次數 | 寫程式的 agent 寫完的次數 |
| 審查 | 審查通過、不通過的次數 |
| 第一次審查就通過 | 第一個審查結果是通過的任務 ÷ 有審查結果的任務 |
| 測試 | 跑測試的次數、失敗次數、因新增的失敗退回的次數（見「測試把關」）；合併後測試一次涵蓋好幾個任務也只算一次，逐一測時沿用同一個 commit 結果的不算次數，另外記成「沿用」 |
| 寫完後被退回 | （審查不通過＋測試退回）÷ 寫完次數 |
| 你的決定 | `decide` 的次數，依 retry、done、abandon 分開 |
| 還在等你 | 目前「等你處理」的任務數 |
| 等你的時間 | 每次 `decide` 距離同一任務前一個事件的時間加總 |
| agent 失敗、合併衝突 | agent 失敗、合併衝突的次數 |
| plan 退回 | `reject` 的次數 |
| 耗時 | 核准到完成；還沒完成（含放棄）時算到現在，並標「進行中」 |
| 模型 | 寫程式和審查用過的 CLI、模型、推理強度 |

`decide ... retry` 的實際效果，依任務停下來的原因而不同：

| 停下來的原因 | `retry` 的效果 |
|---|---|
| 審查 3 次不通過 | 輪數上限 +1，寫程式的 agent 帶著最新審查意見與你的 `--note` 再改一輪 |
| 測試有新增的失敗、輪數用完（見「測試把關」） | 輪數上限 +1，寫程式的 agent 帶著新增失敗的測試名稱、測試輸出最後 50 行與你的 `--note` 再改一輪 |
| 寫程式的 agent 回報卡住、沒有改動、commit 失敗、處理結果出錯 | 回到排隊，寫程式的 agent 接手（你的 `--note` 會交給它） |
| agent 連續失敗 | 回到失敗的那個階段重跑 |
| 審查結果格式不符 | 重新審查（`--note` 不會交給審查的 agent） |
| 合併衝突（自動解衝突 2 次後仍衝突） | 再交給寫程式的 agent 解一次（你的 `--note` 會交給它） |
| 合併失敗 | 重試合併 |
| 合併後測試分不出結果（超時、分不出失敗名稱） | 重新跑合併後測試，不算輪數 |

### 合併衝突

兩個同時進行的任務改到同一個檔案時，後合併的那個會衝突。Alfred 會自動處理：

1. 把最新的 `plan/P0007` 合併進那個任務的 worktree，衝突標記留在檔案裡
2. 叫回原本寫這個任務的 agent 解掉衝突、跑測試（算新的一輪，但輪數上限也跟著加 1，不吃掉審查退回的次數）
3. 審查的 agent 再審查一次，確認這個任務的改動還在、也沒有弄壞其他任務
4. 審查通過後再合併

同一個任務自動解過 2 次還是衝突，就會停在「等你處理」。這時可以：

- `decide P0007 T2 retry --note "..."`：再交給寫程式的 agent 解一次
- 自己解：到 `<專案>-worktrees/P0007` 執行 `git merge task/P0007-T2`，解完衝突並 commit，再執行 `decide P0007 T2 done`

寫程式的 agent 解衝突時回報卡住、或連續失敗而停下時，還沒結束的合併在任務的 worktree（`<專案>-worktrees/P0007-T2`）。你可以直接在那裡把衝突標記清掉（不用 commit），再執行 `decide P0007 T2 done`；標記沒清乾淨的話，任務會再停下來，不會被合併。

要減少衝突，plan 裡改到同一個檔案的任務要寫前置（`docs/AGENTS-snippet.md` 已經要求寫 plan 的 agent 這樣寫）。

## 檔案位置

- `<專案>/.board/events.jsonl`：所有事件
- `<專案>/.board/reviews/`：審查意見
- `<專案>/.board/logs/`：每次啟動 agent 的完整輸出
- `<專案上一層>/<專案名稱>-worktrees/`：plan 與任務的 worktree

## 測試

先安裝測試用的套件（Playwright），再跑測試：

```
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

網頁畫面的測試用 Playwright 控制電腦上的 Edge（或 Chrome），整段測試只開一次瀏覽器，每個測試用自己的乾淨分頁，等畫面真的畫好再檢查；用的是電腦上已裝的瀏覽器，不需要另外執行 `playwright install`。找不到 Playwright（沒裝 `requirements-dev.txt`）或找不到 Edge／Chrome 時，網頁畫面的測試會跳過；跳過的原因會列在測試結果的 short test summary。JavaScript 語法檢查用 node，找不到也會跳過。

## 授權

MIT，見 `LICENSE`。
