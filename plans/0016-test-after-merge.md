---
id: P0016
created: 2026-10-29
area: [後端/測試把關, 後端/派工, 前端/儀表板]
supersedes: P0015
---

# 寫完直接審查，審查通過就合併，完整測試改在合併後對 plan 分支跑，失敗時找出是哪次合併並只退回那個任務

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 如果writer寫完後直接丟給reviewer，合併完後再做機器測試 | 新核准的 plan 改成：writer 寫完直接送審，審查通過就合併進 plan 分支，完整測試改在合併後對 plan 分支跑。平常只測最新的合併，一次涵蓋多個任務；涵蓋多個任務又有新增的失敗時，照合併順序逐一測，找出從哪次合併開始失敗；每次都跟上一個測過的版本比；只退回造成失敗的任務，從最新的 plan 分支往前修；依賴的任務不等測試；最後一次合併測過才算 plan 完成；已經核准的 plan 照原本流程。細節見表格下方「共同契約」 | D1 planApproved 事件記下 testTiming（新核准的一律 afterMerge，舊事件沒有這個欄位就是原流程）；board_core 新增「已合併待測試」狀態，taskState 新增 mergedCommit、mergeFailures、mergeFix，planState 新增 mergeTest，並推導 mergeTestRun 事件與 decide，規則照「共同契約」的「任務狀態」與「reducer 依 outcomes 更新任務」 | 依設定檔判斷走哪個流程：重播事件時設定可能已經改了，同一份 plan 前後會走不同流程；合併後直接算完成、測試失敗再改回來：依賴的任務和 plan 完成都會以為已經測過 | T1 board_core 與 board.py approve 加上 testTiming、已合併待測試、mergeTestRun 與 decide mergeTest 的狀態推導；補 tests/test_board_state_tasks.py、tests/test_board_state_plans.py、tests/test_board_cli.py | | 執行 python -m pytest -q 通過；新增的測試確認：沒有 testTiming 的舊事件推導結果不變；approve 寫出的 planApproved 有 testTiming afterMerge；taskMerged 帶 verify 時任務是已合併待測試、mergedCommit 等於事件的 commit、依賴它的任務出現在 readyTasks、plan 還沒完成；mergeTestRun 的 verified 讓任務完成，最後一個完成後 plan 完成；newFailure、stillFailing 讓任務回排隊、輪數加一、lastTest 與 mergeFailures 記下名稱、mergeFix 為 true、lastReviewNotes 清掉，resumable、sessionId、sessionCli、sessionModel 跟退回前相同，輪數用完時進等你處理 kind tests；unknown 進等你處理 kind mergeTest，decide retry 回到已合併待測試且輪數不變、done 直接完成；advance、split 照事件更新 planState 的 mergeTest |
| R1 | | D2 board_git.py 新增：addMergeTestWorktree(projectDir, planId, commit) 在 `<專案>-worktrees/<plan>-mergetest` 建 detached worktree（建立前先移除舊的）、removeMergeTestWorktree，removePlanWorktrees 也會移除它；firstParentCommits(projectDir, planId) 回傳 plan 分支 first-parent 歷史的 commit（舊到新）；fastForwardTaskToPlan(projectDir, planId, taskId) 在任務 worktree 執行 git merge --ff-only plan 分支，不能快轉時丟出 GitError | 在 plan worktree 直接跑測試：測試期間 Alfred 還會合併新任務，測的內容會變，測試產物留成未追蹤檔還會擋住合併；修正時 revert 合併 commit：其他任務可能已經依賴它，修好後還要再 revert 回來 | T2 board_git 新增合併後測試 worktree、first-parent 歷史與任務分支快轉；補 tests/test_board_git.py | | 執行 python -m pytest -q tests/test_board_git.py 通過；新增的測試確認：worktree 是 detached 在指定的 commit，重建前會移除舊的，removePlanWorktrees 會移除它；firstParentCommits 依合併順序列出每次合併的 commit，不含被合併進來的任務 commit；任務分支已經合併進 plan 分支時快轉到 plan 分支 HEAD，任務分支有不在 plan 分支的 commit 時丟出 GitError |
| R1 | | D3 conductor.py 新增純函式 mergeTestOutcome：輸入比較對象、這次的測試結果、covers、各任務的 mergeFailures、目前的 split，依「共同契約」的「判定」回傳 verdict、newFailures、outcomes、advance、split；比對沿用 compareWithBaseline | 依測試檔名或 diff 猜是哪個任務：共同介面的問題會錯判；失敗時叫 agent 診斷（P0015 的做法）：逐一測就能確定從哪次合併開始失敗，不需要多一個 agent；每次合併各測一次：測試次數等於合併次數，紀錄上測試幾乎都通過，先一起測、失敗才拆開比較省 | T3 conductor 新增合併後測試的判定函式 mergeTestOutcome；補 tests/test_conductor.py | | 執行 python -m pytest -q tests/test_conductor.py 通過；新增的測試逐一涵蓋「判定」的每一種情況：unknown、newFailure 但沒有名稱、涵蓋多個任務的 newFailure、只涵蓋一個任務的 newFailure、passed、preexisting、mergeFailures 還在的 stillFailing、逐一測到 split.commit；pytest 與品質門檻的名稱都照 compareWithBaseline 的規則比 |
| R1 | | D4 conductor.py 接上新流程：新流程的 taskWritten 一律 testing false；taskMerged 帶 verify true；每個 tick 依「共同契約」的「排程」啟動合併後測試（選目標與 covers、沿用 split 的結果、mergetest worktree、計入 maxParallelTests、等基準），測完用 mergeTestOutcome 寫 mergeTestRun；暫停、放棄、Ctrl+C 跟基準一樣結束程式樹、移除 worktree、不寫事件；formatEvent 加上「合併後測試」、通過或失敗與涵蓋的任務 | 測試和審查同時跑：審查只要約 1 分鐘，每輪只省這麼多；依賴的任務等前置測過才開始：依賴鏈上的任務省不到時間，紀錄上測試幾乎都通過 | T4 conductor 接上新流程：寫完直接送審、合併後排程與執行合併後測試、逐一測與沿用結果、暫停與放棄；補 tests/test_conductor.py | T1, T2, T3 | 執行 python -m pytest -q 通過；新增的測試用假 agent 和依檔案內容印出 FAILED 行的假 testCommand 確認：三個獨立任務都合併後只跑一次合併後測試，三個都完成、plan 完成，沒有 testRun 事件；T2 造成新增的失敗時，先一次測出失敗，再逐一測 T1、T2 的合併 commit，T3 的合併 commit 沿用第一次的結果、不重跑，只有 T2 回排隊；依賴 T1 的任務在 T1 是已合併待測試時就開始寫；測試中暫停再繼續，同一批只有一筆 mergeTestRun；放棄 plan 時測試結束、mergetest worktree 移除；maxParallelTests 為 1 時基準與合併後測試不重疊；沒有 testTiming 的 plan 照舊在任務 worktree 跑測試再送審 |
| R1 | | D5 新流程的修正輪與 prompt：mergeFix 的任務開始寫之前先 fastForwardTaskToPlan，失敗記 agentFailed；修正輪照 writerSession 的既有規則接續上一輪的 writer 對話，不重讀整個任務；writer prompt 的測試說明改成審查通過、合併後流程才跑完整測試；有 mergeFailures 時列出失敗名稱、測到的 commit、測試輸出最後 50 行，說明資料夾已更新到最新的 plan 分支（含其他任務的改動），上一輪被審查退回時一起附上審查意見；審查 prompt 不附自動測試結果，說明完整測試在合併後才跑、驗證欄裡的完整測試（例如執行 python -m pytest -q 通過）不是這次審查要確認的、不要自己跑完整測試，修正輪另外列出要修的失敗名稱；舊流程的 prompt 文字不變 | 修正輪從任務原本的分支修：少了其他任務的改動，可能重現不了失敗；修正輪開新對話：要重讀需求和程式，writer 用量變多；審查照舊要求驗證欄的完整測試：新流程審查時還沒有完整測試結果，會一直被退回 | T5 新流程的修正輪快轉與 writer、審查 prompt；補 tests/test_board_prompts.py、tests/test_conductor.py | T4 | 執行 python -m pytest -q 通過；新增的測試確認：新流程 writer prompt 寫明合併後才跑完整測試，舊流程文字不變；修正輪 prompt 有失敗名稱、commit、測試輸出，上一輪被審查退回時也有審查意見；新流程審查 prompt 沒有「自動測試結果」、有驗證欄的說明，修正輪列出失敗名稱；修正輪開始時任務 worktree 的 HEAD 等於當時 plan 分支的 HEAD，審查收到的 diff 只有修正的改動；writer 的 CLI 和模型沒換時，修正輪的 taskStarted 是 resume true、sessionId 跟上一輪相同，Claude 的啟動參數有 --resume、Codex 用 exec resume；換了模型時開新對話；修正合併後的合併後測試通過時任務完成 |
| R1 | | D6 P0010 的沿用基準來源加上 mergeTestRun：有 commit、failures、command 的 mergeTestRun 跟 testBaseline、testRun 一樣可以當來源，reusedFrom 的種類寫 mergeTestRun | 只沿用 testBaseline：新流程沒有任務測試，P0010 的「沿用前一份 plan 最後測過的結果」會失效 | T6 P0010 沿用基準的來源加上 mergeTestRun；補 tests/test_conductor.py | T5 | 執行 python -m pytest -q tests/test_conductor.py 通過；新增的測試確認：前一份 plan 最後一筆 mergeTestRun 測的 commit 跟下一份 plan 的 baseCommit 只差 plans/ 底下的檔案時，下一份核准後立刻有 reusedFrom 種類為 mergeTestRun 的 testBaseline，沒有跑基準測試 |
| R1 | | D7 board_stats.py 的測試統計加上 mergeTestRun：沒有沿用的算一次，passed 為 false 的算失敗，outcomes 裡每個 newFailure、stillFailing 算一次退回；沿用的不算次數，另外記成 reused；board.py stats 的「測試」一行加上「沿用 N」 | 一次合併後測試依涵蓋的任務數計次：看不出比原流程少跑了幾次 | T7 統計加上合併後測試，board.py stats 顯示沿用次數；補 tests/test_board_stats.py、tests/test_board_cli.py | T1 | 執行 python -m pytest -q 通過；新增的測試確認：一筆涵蓋三個任務、通過的 mergeTestRun 算 1 次、退回 0；沿用的那筆不算次數、reused 為 1；一筆有 stillFailing 的算退回 1；只有舊事件時統計結果不變 |
| R1 | | D8 儀表板：總覽 Alfred 卡片在「測試中」那一行下面加「已合併待測試：P0016 T1、T2」，沒有就不顯示；等你處理 kind mergeTest 的 retry 說明是「重新跑合併後測試（不算輪數）」；plan 成效的「測試」一行加上「沿用 N」；board_api 的任務 diff 只在任務是已合併待測試或完成時看合併 commit，修正輪看任務分支 | 把已合併待測試算進「測試中」：任務已經合併、可能還沒輪到測試，跟正在跑任務測試意思不同 | T8 儀表板顯示已合併待測試、mergeTest 的 retry 說明、沿用次數與修正輪的 diff；補 tests/test_web.py、tests/test_board_api.py | T1, T7 | 執行 python -m pytest -q 通過；新增的測試用新流程的事件確認：總覽出現「已合併待測試：P0001 T1」這種文字，沒有這種任務時不顯示；mergeTest 的等你處理顯示 retry 說明；plan 成效顯示沿用次數；修正輪的任務 diff 是任務分支上修正的改動，不是上一次的合併 |
| R1 | | D9 README「測試把關」改寫成新流程（寫完直接送審、合併後在 mergetest worktree 測 plan 分支、平常一次涵蓋多個合併、失敗時逐一測、跟上一個測過的版本比、只退回造成失敗的任務並從最新的 plan 分支修、依賴的任務不等測試、最後測過才算完成、分不出結果時等你處理），原流程保留成「P0016 之前核准的 plan」；第 1 步的沿用來源補上合併後測試；「測試判定」補合併後測試的判定；「指令」的 retry 表補合併後測試無法判定；統計表的「測試」說明沿用次數 | 只改程式不改 README：README 現在寫測試沒過的任務不會送審，會跟新流程不符 | T9 更新 README 的測試把關、測試判定、retry 表與統計說明 | T5, T6, T7, T8 | README「測試把關」看得到新流程的順序、逐一測與只退回造成失敗的任務；「測試判定」看得到合併後測試的判定；retry 表看得到合併後測試無法判定時 retry 會重跑測試、不算輪數；統計表看得到沿用次數 |

## 共同契約

### 新流程的範圍

- board.py approve 寫的 planApproved 多一個欄位 `testTiming: "afterMerge"`。沒有這個欄位的 plan（這份 plan 合併前核准的，包括這份 plan 自己）照原流程，程式行為與事件都不變。
- 「新流程」指 testTiming 為 afterMerge 的 plan。testCommand 有設定、沒有 skipTests 時才跑合併後測試；skipTests 或沒有 testCommand 時，taskMerged 不帶 verify，合併後直接完成。
- 基準照舊：plan 核准後先跑基準，基準記下來之前不派工。

### 任務狀態

- 新增 `TaskStatus.mergedUntested = "已合併待測試"`。
- 新流程的 taskWritten 一律 `testing: false`：寫完直接待審查，不跑任務測試，也不寫 testRun。
- taskMerged 的 data 帶 `verify: true` 時，任務變成已合併待測試，`taskState["mergedCommit"]` 記成 data 的 commit；沒有 verify 時照舊變完成。
- 前置任務是完成或已合併待測試，就算前置滿足（readyTasks）。
- plan 完成條件不變：全部任務都是完成或放棄才完成，已合併待測試不算。
- taskState 新欄位：`mergedCommit`（預設 null）、`mergeFailures`（預設 []，合併後測試退回時記下的失敗名稱）、`mergeFix`（預設 false，下一輪開始寫之前要先快轉）。planState 新欄位 `mergeTest = {"lastTested": null, "split": null}`。

### 排程（conductor）

- 待測任務：這份 plan 狀態是已合併待測試的任務，依 mergedCommit 在 firstParentCommits 裡的位置排序，先合併的在前，找不到的排最後。
- 比較對象：mergeTest.lastTested；沒有，或它的 command 跟目前的 testCommand 不同時，用 plan 的 testBaseline。
- 以下情況不啟動：plan 不是進行中、暫停中、needsTestBaseline 為 true、這份 plan 已經有合併後測試在跑、這份 plan 有任務停在等你處理而且 kind 是 mergeTest、沒有待測任務、測試名額已滿。
- 測試名額是 P0012 的 maxParallelTests，基準、舊流程的任務測試、合併後測試合計。有空位時基準優先，其餘依開始等待的時間；合併後測試從這份 plan 最早一個待測任務的 taskMerged 時間算。
- 平常：目標是 plan 分支 HEAD，covers 是全部待測任務。
- 逐一測（mergeTest.split 有值時）：目標是第一個待測任務的 mergedCommit，covers 只有它。目標就是 split 的 commit 時，直接用 split 那筆的測試結果寫事件（reused true），不跑測試、不佔名額。第一個待測任務排在 split 的 commit 之後或找不到 split 的 commit 時，當成沒有 split，照平常測，事件的 split 填 "clear"。
- 測試在 addMergeTestWorktree 建的 worktree 裡背景執行，跑完寫事件、移除 worktree。covers 在測試開始時決定，測試期間才合併的任務留給下一次。
- 暫停、放棄 plan、Ctrl+C：跟基準一樣結束整個程式樹、移除 worktree、不寫事件，之後重跑。
- job 的 key 用 `<plan>/mergetest`，kind 是 `mergeTest`。

### mergeTestRun 事件

plan 層級的事件（task 為 null），data 欄位：

- commit、command、passed、failures、qualityChecked、qualityFailures、tail（最後 50 行）：意思跟 testBaseline 一樣
- comparedTo：比較對象的 commit
- covers：涵蓋的任務編號，依合併順序
- reused：true 表示沿用 split 那筆的結果，沒有重跑
- verdict、newFailures：compareWithBaseline(比較對象, 這次的結果) 的回傳值
- outcomes：`{任務編號: {"result": "verified" 或 "newFailure" 或 "stillFailing" 或 "split" 或 "unknown", "names": [失敗名稱]}}`
- advance：true 時 mergeTest.lastTested 換成這筆
- split："start"（mergeTest.split 記成這筆）、"clear"（清掉 mergeTest.split）或 null（不變）

這些欄位都由 conductor 寫事件時算好；reducer 只照事件更新，不重算。

### 判定（mergeTestOutcome）

這次的名稱 = failures 加上 qualityFailures。依序判斷：

1. verdict 是 unknown，或 verdict 是 newFailure 但 newFailures 是空的（例如超時、分不出名稱）：covers 全部 unknown；advance false；split 有值時填 "clear"，否則 null。
2. verdict 是 newFailure 而且 covers 超過一個：covers 全部 split；advance false；split 填 "start"。逐一測時 covers 只有一個，不會進這一條。
3. 其他情況：advance true；這次的 commit 是 split 的 commit 時 split 填 "clear"，否則 null。每個 covers 的任務：
   - mergeFailures 裡有名稱還在這次的名稱裡：stillFailing，names 是還在的那些，verdict 是 newFailure 時再加上 newFailures
   - 否則，verdict 是 newFailure：newFailure，names 是 newFailures
   - 否則：verified

### reducer 依 outcomes 更新任務

只更新狀態還是已合併待測試的任務，其他狀態（例如已經放棄）略過。

- verified：完成，mergeFailures 清空
- newFailure、stillFailing：`lastTest = {"source": "mergeTest", "verdict": "newFailure", "newFailures": names, "commit": commit, "tail": tail}`；mergeFailures 換成 names；mergeFix 設成 true；lastReviewNotes 清成 null；resumable 與 session 欄位不變（見「修正輪」）；輪數沒用完時回排隊、round 加一；用完時進等你處理 kind tests，reason 寫「第 N 輪合併後測試有新增的失敗，輪數用完：名稱」
- split：不變
- unknown：進等你處理 kind mergeTest，reason 寫「合併後的完整測試無法判定有沒有新增的失敗（例如超時或分不出失敗名稱），commit 某某」
- decide：kind mergeTest 的 retry 回到已合併待測試（重測，不算輪數）；done 直接完成；abandon 放棄（改動已經在 plan 分支裡，不會被移除）。kind tests 的 retry 照舊（輪數上限加一、回排隊）
- mergeFix 在 taskWritten 時清成 false

### 修正輪

- mergeFix 為 true 的任務開始寫之前，conductor 先執行 fastForwardTaskToPlan；失敗時記 agentFailed（stage write）。
- 修正輪接續這個任務上一輪的 writer 對話，不開新對話：合併後測試的退回（newFailure、stillFailing）不改 resumable、sessionId、sessionCli、sessionModel，taskMerged 與 mergeTestRun 也不改；writer 的 CLI 和模型沒換時，照 writerSession 的既有規則接回（Claude 用 --resume，Codex 用 exec resume）。換了 CLI 或模型時照既有規則開新對話。快轉不算中斷，不設 needsClean。
- 之後照一般流程：寫完送審，通過後合併，taskMerged 帶 verify，下一次合併後測試會涵蓋它；mergeFailures 裡的名稱要在那次測試消失才算 verified。

## 背景

- 2026-10-29 的事件紀錄：30 次任務測試沒有一次判定為新增的失敗（都是通過，或只有基準本來就有的失敗）；35 次審查退回 9 次，這 9 次的測試都通過，退回的原因是邊界情況與需求不符。最近的完整測試約 5.5 到 7.6 分鐘，審查約 0.4 到 1.4 分鐘。現在每一輪寫完都要先等完整測試才能審查；P0003 T1 改了 4 輪，跑了 4 次完整測試。
- 使用者 2026-10-29 指示用這份 plan 取代 P0015。P0015 先合到候選分支，測完、審完才進 plan 分支，並用診斷 agent 找出責任任務，共 13 個任務。這份 plan 接受 plan 分支暫時有失敗，換來不需要候選分支和診斷 agent：一次測多個合併，失敗時照合併順序逐一測，就能確定從哪次合併開始失敗。
- 沿用 P0015 的做法：跟上一個測過的版本比；只退回造成失敗的任務；修正時從包含其他任務成果的最新版本開始。

## 範圍

- 不包含：新流程的基準改成不擋派工（新流程只在第一次合併後測試前需要基準，之後另寫 plan）；P0015 的候選分支、批次、診斷 agent、結構化 pytest 報告與新增跳過項目的偵測；不穩定測試的重跑；自動合併回 main。
- 已經核准的 plan（包括這份 plan 自己）照原流程執行，不在途中換流程。

## 前置與實作限制

- P0010、P0012 先合併回 main，再核准這份 plan：T4 的測試名額用 P0012 的 maxParallelTests，T6 改的是 P0010 的沿用基準。
- 這份 plan 和 P0014 都會改 conductor.py；P0014 在這份 plan 合併後再核准，核准前重新盤點品質數字。
