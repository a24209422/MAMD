---
id: P0010
created: 2026-10-28
area: [後端/測試把關]
---

# 程式碼和之前測過的版本相同時，沿用之前的基準或任務測試結果當基準，同時需要的基準只跑一次

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 幫我寫沿用基準測試的 plan | 每份 plan 核准後都在 baseCommit 跑一次完整測試當基準，約 12 分鐘，跑完才派工；但兩份 plan 的 baseCommit 常常只差 plans/ 底下的 plan 檔（submit 會自動 commit plan 檔），程式碼與測試完全相同，結果不會不同；要在這種情況沿用已經記下的基準，不再重跑；兩份 plan 同時需要同一份基準時也只跑一次 | D1 board_git.py 新增 changedPaths(projectDir, fromCommit, toCommit)，用 git diff --name-only 回傳兩個 commit 之間改過的路徑；commit 不存在或 git 失敗時丟出 GitError | 用 git diff --quiet 加 pathspec 排除 plans/：排除規則寫在指令裡不好測，也看不出改了哪些檔；比對兩個 commit 的 tree hash：只要 plan 檔不同 tree 就不同，等於只能比完全相同的 commit | T1 board_git 新增 changedPaths，補 tests/test_board_git.py | | 執行 python -m pytest -q tests/test_board_git.py 通過；新增的測試確認只改 plans/ 下檔案時回傳那些路徑、相同 commit 回傳空清單、有改程式時包含該檔、commit 不存在時丟出 GitError |
| R2 把這部分加進 P0010 | 審查通過的任務都跑過 Alfred 的完整測試；前一份 plan 的最後一個任務如果已經測過最終組合，合併回 main 後馬上核准的下一份 plan，baseCommit 和那次被測的 commit 之間常常也只差 plans/（例如 P0007 T2 測的 3650c39 與 P0009 的 baseCommit ef395b6 只差兩份 plan 檔），也要能沿用那次的測試結果當基準；但沒有前置關係的任務各自在自己的分支測試、合併後的組合沒人測過，所以只在程式碼真的相同時沿用；基準要用完整的失敗名單比對，現在的 testRun 沒有記 | D2 conductor.py 的任務測試多記兩個欄位：startTests 啟動測試時記下任務 worktree 的 HEAD commit，finishTests 寫 testRun 時加上 commit 與完整的 failures（runTestCommand 已經回傳，只是沒寫進事件）；既有欄位 passed、tail、verdict、newFailures 不變，舊的 testRun 事件沒有這兩個欄位也照常重播 | 只記 commit、不記 failures：沿用成基準時分不出哪些失敗本來就有，基準就有失敗的專案會把它們都當成新增；從 tail 再解析失敗名稱：tail 只有最後 50 行，失敗多時會漏 | T2 testRun 多記被測的 commit 與完整 failures；補 tests/test_conductor.py | | 執行 python -m pytest -q tests/test_conductor.py 通過；新增的測試確認任務測試寫出的 testRun 有 commit（等於任務 worktree 當時的 HEAD）和 failures（失敗時列出名稱、通過時是空清單），既有的 verdict 判定照樣通過 |
| R1 | | D3 conductor.py 的 runTestBaselines 在啟動基準 job 之前先找可以沿用的來源：從事件紀錄裡所有 testBaseline，以及有 commit 和 failures 欄位的 testRun（不論 plan 的狀態），挑 command 和目前 testCommand 相同（testRun 用寫入當時的 testCommand 判斷，沒有記的話跳過）、而且 changedPaths(來源的 commit, 這份 plan 的 baseCommit) 每一條都在 plans/ 底下的，取最新的一筆；找到就替這份 plan 寫一筆 testBaseline，passed、failures、tail 照抄來源，commit 填這份 plan 自己的 baseCommit，另外加 reusedFrom 記下來源的種類（baseline 或 testRun）、plan、task 與 commit，不建立基準 worktree、不跑測試；另一份 plan 的基準 job 正在跑、而且它的 baseCommit 也符合上述條件時，這一輪先不啟動，等那份跑完下一輪再沿用；changedPaths 失敗時照原本流程自己跑；Alfred 印出的事件和儀表板的事件文字標出沿用來源，例如「沿用 P0007 T2 的測試結果」 | 只沿用 commit 完全相同的基準：submit 會 commit plan 檔，每份 plan 的 baseCommit 幾乎都不同，幾乎不會命中；只要審查通過就沿用最後一次任務測試：沒有前置關係的任務各自測試，合併後的組合沒人測過，plan 合併回 main 時也可能混進別的程式碼；把 README、docs/ 也視為不影響測試：tests/test_board_setup.py 等會複製或讀取專案檔案，要逐一確認，先只放行 plans/；同時需要時兩份各自跑：兩份完整測試同時跑會互搶 CPU，瀏覽器測試更容易不穩定，也白費一份時間；用快取檔記基準：結果已經在事件紀錄裡，多一份檔案要處理同步與失效 | T3 conductor 沿用基準：runTestBaselines 從 testBaseline 和有 commit、failures 的 testRun 找可沿用的來源並寫入 reusedFrom，符合條件的基準 job 正在跑時等它；事件文字標出沿用來源；補 tests/test_conductor.py | T1, T2 | 執行 python -m pytest -q 通過；新增的測試確認：兩份 plan 先後核准、中間只多了 plan 檔的 commit，第二份的 testBaseline 有 reusedFrom、沒有建立基準 worktree、測試指令只執行一次；前一份 plan 的任務測試過的 commit 與下一份 baseCommit 只差 plans/ 時，沿用那次 testRun，failures 照抄；中間有程式碼改動時照常自己跑；兩個沒有前置關係的任務各自測過、合併後才核准下一份時不沿用；testCommand 不同時不沿用；沒有 commit、failures 的舊 testRun 不拿來沿用；兩份同時核准時只有一個基準 job，跑完後兩份都有 testBaseline 並開始派工；沿用來源的 plan 已放棄仍可沿用；沿用的基準照樣用來判定新增的失敗 |
| R2 | | D3 | | T3 | | |
| R1 | | D4 README「測試把關」第 1 步補上：基準在跑之前，會先找程式碼相同（只差 plans/ 底下的檔案）、testCommand 也相同的之前基準或任務測試結果，找到就沿用不再重跑；同時需要的基準只跑一次 | 只改程式不改 README：使用者會以為每份 plan 都要等 12 分鐘基準 | T4 更新 README「測試把關」第 1 步，說明沿用的條件與來源 | T3 | README「測試把關」第 1 步看得到沿用的條件、可以沿用之前的基準或任務測試結果，以及同時需要時只跑一次 |
| R2 | | D4 | | T4 | | README 看得到前一份 plan 最後測過的程式碼沒有再改動時，下一份 plan 會沿用那次的任務測試結果 |

## 背景

- 2026-10-28 實際紀錄：P0005 的 baseCommit 064939f 與 P0007 的 4db833c 之間只差 plans/0007-interruptible-test-baseline.md；P0007 T2 測的 3650c39 與 P0009 的 baseCommit ef395b6 之間只差 plans/0008、plans/0009 兩份 plan 檔；這幾次基準各跑了約 11 到 12 分鐘，照這份 plan 的規則都可以沿用。
- tests/ 只讀取 repo 的 web/、server.py 等檔案，不讀 repo 的 plans/；測試裡用到的 plan 都寫在暫時的測試專案裡。
- 現在的 testRun 只記 passed、tail、verdict、newFailures，沒有被測的 commit，也沒有完整的失敗名單，所以 R2 要先補這兩個欄位。

## 範圍

- 主要省下的情況：連續核准好幾份 plan、中間沒有合併程式；以及前一份 plan 的最後一個任務已經測過最終組合，合併回 main 後馬上核准下一份。plan 合併回 main 時混進其他程式改動，或最後的組合沒被測過時，一樣要重跑。
- 不包含：依改動選擇要跑的測試、讓完整測試變快、Alfred 判定新增失敗前重跑失敗的測試。
- 這份 plan 會改 conductor.py，跟待核准的 P0008 重疊；兩份不要同時進行，一份合併回 main 後再核准另一份。
