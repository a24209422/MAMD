---
id: P0010
created: 2026-10-28
area: [後端/測試把關]
---

# 基準測試在程式碼相同時沿用之前的結果，同時需要的基準只跑一次

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 幫我寫沿用基準測試的 plan | 每份 plan 核准後都在 baseCommit 跑一次完整測試當基準，約 12 分鐘，跑完才派工；但兩份 plan 的 baseCommit 常常只差 plans/ 底下的 plan 檔（submit 會自動 commit plan 檔），程式碼與測試完全相同，結果不會不同；要在這種情況沿用已經記下的基準，不再重跑；兩份 plan 同時需要同一份基準時也只跑一次 | D1 board_git.py 新增 changedPaths(projectDir, fromCommit, toCommit)，用 git diff --name-only 回傳兩個 commit 之間改過的路徑；commit 不存在或 git 失敗時丟出 GitError | 用 git diff --quiet 加 pathspec 排除 plans/：排除規則寫在指令裡不好測，也看不出改了哪些檔；比對兩個 commit 的 tree hash：只要 plan 檔不同 tree 就不同，等於只能比完全相同的 commit | T1 board_git 新增 changedPaths，補 tests/test_board_git.py | | 執行 python -m pytest -q tests/test_board_git.py 通過；新增的測試確認只改 plans/ 下檔案時回傳那些路徑、相同 commit 回傳空清單、有改程式時包含該檔、commit 不存在時丟出 GitError |
| R1 | | D2 conductor.py 的 runTestBaselines 在啟動基準 job 之前先找可以沿用的基準：從所有 plan（不論狀態）已記下的 testBaseline 裡，挑 command 和目前 testCommand 相同、而且 changedPaths(那份基準的 commit, 這份 plan 的 baseCommit) 每一條都在 plans/ 底下的，取最新的一筆；找到就替這份 plan 寫一筆 testBaseline，內容照抄 passed、failures、tail，commit 填這份 plan 自己的 baseCommit，另外加 reusedFrom 記下來源的 plan 與 commit，不建立基準 worktree、不跑測試；另一份 plan 的基準 job 正在跑、而且它的 baseCommit 也符合上述條件時，這一輪先不啟動，等那份跑完下一輪再沿用；changedPaths 失敗時照原本流程自己跑；Alfred 印出的事件和儀表板的事件文字標出「沿用 Pxxxx 的基準」 | 只沿用 commit 完全相同的基準：submit 會 commit plan 檔，每份 plan 的 baseCommit 幾乎都不同，幾乎不會命中；把 README、docs/ 也視為不影響測試：tests/test_board_setup.py 等會複製或讀取專案檔案，要逐一確認，先只放行 plans/；同時需要時兩份各自跑：兩份完整測試同時跑會互搶 CPU，瀏覽器測試更容易不穩定，也白費一份時間；用快取檔記基準：結果已經在事件紀錄裡，多一份檔案要處理同步與失效 | T2 conductor 沿用基準：runTestBaselines 先找可沿用的 testBaseline 並寫入 reusedFrom，符合條件的基準 job 正在跑時等它；事件文字標出沿用來源；補 tests/test_conductor.py | T1 | 執行 python -m pytest -q 通過；新增的測試確認：兩份 plan 先後核准、中間只多了 plan 檔的 commit，第二份的 testBaseline 有 reusedFrom、沒有建立基準 worktree、測試指令只執行一次；中間有程式碼改動時第二份照常自己跑；testCommand 不同時不沿用；兩份同時核准時只有一個基準 job，跑完後兩份都有 testBaseline 並開始派工；沿用來源的 plan 已放棄仍可沿用；沿用的基準照樣用來判定新增的失敗 |
| R1 | | D3 README「測試把關」第 1 步補上：兩份 plan 的 baseCommit 之間只差 plans/ 底下的檔案、testCommand 也相同時，沿用已記下的基準，不再重跑；同時需要的基準只跑一次 | 只改程式不改 README：使用者會以為每份 plan 都要等 12 分鐘基準 | T3 更新 README「測試把關」第 1 步，說明沿用基準的條件 | T2 | README「測試把關」第 1 步看得到沿用基準的條件，以及同時需要時只跑一次 |

## 背景

- 2026-10-28 實際紀錄：P0005 的 baseCommit 064939f 與 P0007 的 4db833c 之間只差 plans/0007-interruptible-test-baseline.md，兩份基準各跑了約 11 到 12 分鐘；照這份 plan 的規則，P0007 可以直接沿用。
- tests/ 只讀取 repo 的 web/、server.py 等檔案，不讀 repo 的 plans/；測試裡用到的 plan 都寫在暫時的測試專案裡。

## 範圍

- 效益有限：plan 之間有合併程式碼時（例如 P0007 合併回 main 之後才核准的 P0009）一樣要重跑；主要省下的是連續核准好幾份 plan、中間沒有合併程式的情況。
- 不包含：依改動選擇要跑的測試、讓完整測試變快、Alfred 判定新增失敗前重跑失敗的測試。
- 這份 plan 會改 conductor.py，跟待核准的 P0008 重疊；兩份不要同時進行，一份合併回 main 後再核准另一份。
