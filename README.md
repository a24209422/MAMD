# agent-board

你對 Codex 或 Claude 說需求 → 它寫 plan → 你核准 → 寫程式的 agent 寫程式 → 審查的 agent 審查 → 不通過自動退回，最多審 3 次。

- 寫程式、審查各自可以用 Claude 或 Codex 的任意模型（見「換模型」）
- 每份 plan 是一張追溯矩陣（需求 → 做法 → 任務 → 驗證），送出時自動檢查有沒有斷線
- 任務在各自的 git worktree 裡平行進行，合併衝突由寫程式的 agent 自動解、再送審
- 瀏覽器儀表板可以看即時狀態，也能在網頁上核准、退回和處理卡住的任務

## 需求

- Python 3（在 3.13 測過），只用標準函式庫，不用 `pip install`
- git
- [Claude Code](https://claude.com/claude-code) 和 [Codex CLI](https://github.com/openai/codex)，都要先登入
- 目前只在 Windows 測過

## 安裝

```
git clone https://github.com/a24209422/agent-board.git
```

下面的 `<agent-board>` 代表 clone 下來的資料夾，例如 `D:\tools\agent-board`。

要讓寫程式的 agent 照 TDD 寫程式（`init --tdd`，只適用 Python 專案）時，另外安裝附的 tdd-crap skill：

```
python <agent-board>\install_skill.py
```

- 安裝到 `~/.claude/skills/tdd-crap`；已經有同名但不是這裡裝的 skill 時不覆蓋
- 更新：在 `<agent-board>` 執行 `git pull`，再執行一次 `install_skill.py`

### 讓 Claude Code 幫你裝

在要使用的專案資料夾開 Claude Code，貼上：

```text
請照 https://github.com/a24209422/agent-board 的 README 安裝 agent-board，並在目前的專案執行 init
```

## 在專案裡啟用（每個專案一次）

1. 專案要是 git repo，而且至少有一個 commit
2. 在專案資料夾執行：

   ```
   python <agent-board>\board.py init
   ```

   - 把 `docs/AGENTS-snippet.md` 的規則寫進專案的 `AGENTS.md`，前後加標記；規則更新後再跑一次 `init`，會整段換成最新版
   - 規則裡寫 plan 的 agent 要執行的 `board.py` 路徑，會寫成這份 `<agent-board>` 的實際位置；`<agent-board>` 搬家後要再跑一次 `init`
   - 建立 `.claude/skills/agent-board-plan/SKILL.md`，內容是同一份規則：在 Claude Code 打 `/agent-board-plan 你的需求`，就由 Claude 寫 plan；注意：Claude Code 平常也會自己讀 `AGENTS.md`，所以在這個專案裡開的 Claude Code，即使沒打這個指令，也會看到「負責寫 plan、不要自己寫程式」那段規則；要它直接寫程式時請明講。已經有同名、但不是 init 產生的 skill 時不覆蓋
   - `.gitignore` 補上 `.board/`、`plans/_review/`
   - 不會自動 commit，最後會印出要執行的 git 指令
   - 測試產物（例如 `__pycache__/`、`.pytest_cache/`）要自己加進 `.gitignore`，否則會被 commit 進任務分支
   - `AGENTS.md` 以前手動貼過規則的話，要先刪掉那一段再跑

   Python 專案要讓寫程式的 agent 照 tdd-crap skill 寫程式時，改用：

   ```
   python <agent-board>\board.py init --tdd --source src --tests tests
   ```

   - 要先安裝 tdd-crap skill（見「安裝」）
   - `.board/config.json` 會開啟 `tddCrap`，`testCommand` 設成 tdd-crap 的品質檢查（每個函式的覆蓋率、循環複雜度）
   - 寫程式的 agent 照 tdd-crap 的流程寫（Claude 用 Skill 工具載入，Codex 直接讀 skill 檔），可以執行品質檢查和 `python -m pytest`；審查時另外檢查測試有沒有真的在驗證行為
   - 專案的 Python 要裝好 pytest、coverage、radon；沒裝時 `init` 會印出安裝指令
   - `--source` 可以重複；沒指定時是 `src`、`tests`

3. 需要的話建立 `.board/config.json`（全部選填）：

   ```json
   { "maxParallel": 3, "maxRounds": 3, "timeoutMinutes": 30, "testCommand": "python -m pytest -q" }
   ```

   寫程式的 agent 是 Claude 時，預設只能執行 `testCommand` 這一個 shell 指令（其他都會被拒絕）。需要其他指令時，加進 `writer.allowedTools`，Bash 和 PowerShell 兩種寫法都要列（Windows 上 Claude 用的是 PowerShell）：

   ```json
   { "testCommand": "python -m pytest -q", "writer": { "cli": "claude", "allowedTools": ["Bash(npm install*)", "PowerShell(npm install*)"] } }
   ```

   - `maxParallel` 是寫程式、審查各自同時跑的上限：設 1 代表同時最多 1 個寫程式加 1 個審查
   - `writer`、`reviewer` 兩段（例如任務卡住後才加 `allowedTools`）改了之後，conductor 從下一個新啟動的 agent 開始用；其他設定改了要重開 conductor 才生效
   - `testCommand` 在 conductor 裡同步執行，跑測試時 conductor 不會排新工作，測試要夠快
   - 寫程式、審查要用哪個 CLI 和模型，見「換模型」

## 換模型

寫程式（`writer`）和審查（`reviewer`）各自可以選 Claude 或 Codex，以及要用的模型，寫在 `.board/config.json`：

```json
{
  "writer":   { "cli": "codex",  "model": "gpt-6.1-sol" },
  "reviewer": { "cli": "claude", "model": "opus" }
}
```

| 欄位 | 說明 | 沒寫時 |
|---|---|---|
| `cli` | `claude` 或 `codex` | 寫程式 `claude`、審查 `codex` |
| `model` | 交給 CLI 的模型名稱（Claude 用 `--model`，Codex 用 `-m`） | 用 CLI 自己的預設模型 |
| `contextWindow` | 這個模型的 Context 大小（tokens），只影響儀表板的使用率 | Claude 200000、Codex 475000 |
| `allowedTools` | 寫程式的 Claude 可以多跑的指令，見「在專案裡啟用」 | 沒有 |
| `command` | CLI 不在 PATH 上時寫完整路徑，例如 `["C:/tools/codex.cmd"]` | PATH 上的 `claude`、`codex` |

- 兩個都不寫時：Claude 寫程式、Codex 審查，用各自的預設模型
- 同時跑的 agent 身分（`writer`、`writer-sub1`…、`reviewer`…）都用所屬角色的設定
- 也可以在儀表板的「模型」分頁改 `cli` 和 `model`，其他欄位還是要改設定檔。儀表板只接受英數字開頭、由英數字和 `. _ - : / [ ]` 組成的模型名稱；角色有自訂 `command` 時，要換 CLI 得先改設定檔
- conductor 每一輪都會重讀 `writer`、`reviewer`：改了之後從下一個新啟動的 agent 開始用，正在跑的不受影響。其他設定（`maxParallel`、`testCommand` 等）改了要重開 conductor
- 換了寫程式的 CLI 或模型之後，進行中的任務下一輪會開新對話，接不回上一輪
- 權限：
  - Claude 寫程式：只能執行 `testCommand` 和 `allowedTools` 列的指令
  - Codex 寫程式：在 `workspace-write` 沙箱裡，只能寫入這個任務的 worktree，但不能限制它執行哪些指令
  - Claude 審查：會改檔案、執行指令的內建工具（Edit、Write、NotebookEdit、Bash、PowerShell）都被拿掉；你在 Claude Code 設定裡另外放行的工具（例如 MCP 工具）仍可能可用
  - Codex 審查：`read-only` 沙箱
- 寫 plan 的 agent 是你自己開的，模型在開的時候選：`codex -m <模型>`，或 `claude --model <模型>` 再打 `/agent-board-plan`
- 舊版的 `claudeCommand`、`codexCommand`、`claudeAllowedTools`、`claudeContextWindow`、`codexContextWindow` 已經不用了，conductor 看到會拒絕啟動並說明要改成什麼

## 使用

1. 開一個終端機跑 conductor，它會即時印出每個事件：

   ```
   python <agent-board>\conductor.py <專案資料夾>
   ```

   一個專案同時只能跑一個 conductor，第二個會拒絕啟動。

2. 在專案資料夾開 `codex` 說你的需求；或開 `claude`，打 `/agent-board-plan 你的需求`。寫完 plan 會自己送出。
3. 看線路圖：`python <agent-board>\board.py --project <專案資料夾> index`，打開 `plans/_review/index.html`
4. 核准：`python <agent-board>\board.py --project <專案資料夾> approve P0007`，或在儀表板上按「核准」
5. 之後看 conductor 的終端機，或跑 `status`。完成後 `plan/P0007` 分支裡就是所有任務的成果，要不要合併到你的分支由你決定。

## 儀表板

在瀏覽器看即時狀態，也可以在網頁上核准、退回、放棄、`decide`、停止和繼續，以及在「模型」分頁換寫程式、審查的 CLI 和模型（見「換模型」）。

1. 在你自己開的終端機執行（不能讓 agent 啟動：網址裡的 token 等於核准權限）：

   ```
   python <agent-board>\server.py <專案資料夾> [--port 8765]
   ```

2. 用它印出的網址開瀏覽器（`http://127.0.0.1:8765/#token=...`）。沒帶 token 的網址只能看，不能操作
3. server 重開後 token 會換，要用新印出的網址重開
4. 可以和 conductor 同時開；同時看兩個專案就用不同的 `--port`

不花用量的展示：`python <agent-board>\tests\fakes\make_demo.py <新的資料夾>`，照它印出的步驟做。

## 指令（在專案資料夾執行時可以省略 `--project`）

| 指令 | 做什麼 |
|---|---|
| `status [P0007]` | 看進度 |
| `index` | 產生總覽頁與線路圖 |
| `init [--tdd --source src --tests tests]` | 在專案啟用 agent-board，見「在專案裡啟用」 |
| `approve P0007` | 核准（有橘、黃問題時會先問你） |
| `reject P0007 --reason "..."` | 退回 plan |
| `abandon P0007` | 放棄 plan，正在跑的 agent 會被結束 |
| `decide P0007 T2 retry --note "..."` | 等你處理的任務：再跑一輪 |
| `decide P0007 T2 done` | 等你處理的任務：你已在 worktree 改好，直接合併 |
| `decide P0007 T2 abandon` | 等你處理的任務：放棄 |
| `stop`／`resume` | 全部停止／繼續 |
| `cleanup P0007` | 刪除結束的 plan 的 worktree 與任務分支（`plan/P0007` 保留） |

`approve`、`reject`、`abandon`、`decide`、`stop`、`resume`、`cleanup` 只能由你在終端機執行，agent 執行會被拒絕。

`decide ... retry` 的實際效果，依任務停下來的原因而不同：

| 停下來的原因 | `retry` 的效果 |
|---|---|
| 審查 3 次不通過 | 輪數上限 +1，寫程式的 agent 帶著最新審查意見與你的 `--note` 再改一輪 |
| 寫程式的 agent 回報卡住、沒有改動、commit 失敗、處理結果出錯 | 回到排隊，寫程式的 agent 接手（你的 `--note` 會交給它） |
| agent 連續失敗 | 回到失敗的那個階段重跑 |
| 審查結果格式不符 | 重新審查（`--note` 不會交給審查的 agent） |
| 合併衝突（自動解衝突 2 次後仍衝突） | 再交給寫程式的 agent 解一次（你的 `--note` 會交給它） |
| 合併失敗 | 重試合併 |

### 合併衝突

兩個同時進行的任務改到同一個檔案時，後合併的那個會衝突。conductor 會自動處理：

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

```
python -m pytest -q
```

網頁畫面的測試用 Edge（或 Chrome）的無頭模式，JavaScript 語法檢查用 node；找不到就跳過。

## 授權

MIT，見 `LICENSE`。
