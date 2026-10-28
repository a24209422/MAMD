---
id: P0007
created: 2026-10-28
area: [後端/測試把關]
---

# 基準測試改在背景執行，放棄 plan 或停止 Alfred 時能立刻中斷

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 放棄或停止時能中斷基準測試 | 基準測試現在在 tick 裡同步執行，跑完前 conductor 讀不到 planAbandoned、paused 事件，abandon、stop 都要等整套測試跑完才生效；要改成基準測試跑到一半時，放棄這份 plan、board.py stop 或 Ctrl+C 結束 Alfred，都會在下一輪 tick 結束測試的整個程式樹 | D1 基準測試沿用 P0004 的 BackgroundTestJob 在背景執行緒跑：runTestBaselines 對進行中、需要基準、還沒有基準 job 的 plan 建立 baseline worktree 後啟動 job，以 kind "baseline"、和任務不衝突的 key 放進 self.jobs，info 記下 plan、baseCommit、worktree；collectFinishedJobs 看到 baseline job 結束時，由主執行緒照原本內容寫 testBaseline 事件，再移除 baseline worktree；啟動或處理結果出錯時沿用 notifyOnce 提示、下一輪重試，不寫 needsYou；stopJobs 遇到 baseline job 時結束測試程式樹、移除 baseline worktree，不寫任何事件；plan 不是進行中（例如已放棄）時，tick 一開始就結束它的 baseline job；暫停時跟其他 job 一樣結束，繼續後因為還沒有基準，會從頭重跑；needsTestBaseline 前「基準記下來前不派工、不送審」的規則不變 | 同步執行時在 communicate 的等待中輪詢事件檔：要把讀事件、判斷狀態塞進 runTestCommand，跟 tick 的邏輯重複；用 multiprocessing 另開行程跑基準：要多一套行程間溝通，背景執行緒等 subprocess 就夠，任務測試也是這樣做；中斷時保留已跑出的部分輸出當基準：測試沒跑完，失敗名稱不完整，拿來比對會誤判新增的失敗 | T1 conductor 的基準測試改用背景 job：runTestBaselines 啟動 baseline job，collectFinishedJobs 寫 testBaseline 並移除 worktree，stopJobs 和 plan 不再進行中時結束程式樹並移除 worktree；補 tests/test_conductor.py | | 執行 python -m pytest -q 通過；新增的測試用會睡幾秒的假 testCommand 確認：基準跑著時放棄 plan，下一輪 tick 後測試程式已結束、沒有 testBaseline、baseline worktree 已移除、之後沒有 taskStarted；基準跑著時 stop，測試程式結束、沒有 testBaseline 和 taskInterrupted，resume 後重跑基準並寫出一筆 testBaseline，接著才有 taskStarted；基準跑著時另一份已經有基準的 plan 照樣派工；基準跑著時 Ctrl+C（stopJobs 全部），測試程式結束、worktree 已移除；既有的基準測試（在 baseCommit 跑、失敗仍派工、換 testCommand 重跑、worktree 建立失敗只提示一次、基準記下來前不派工不送審）照樣通過 |
| R1 | | D2 README「使用」第 1 步刪掉「plan 開始時的基準測試仍然同步執行，跑基準時 Alfred 不會排新工作」，改成基準測試也在背景執行，不擋其他 plan；「測試把關」第 1 步補上基準跑到一半時放棄 plan 或 stop 會立刻結束，stop 後 resume 會重跑基準 | 只改程式不改 README：README 目前寫跑基準時 Alfred 不排新工作，會跟實際行為不符 | T2 更新 README 的 testCommand 說明和「測試把關」第 1 步，說明基準在背景執行、放棄與停止會中斷、resume 後重跑 | T1 | README「使用」第 1 步不再寫基準測試會擋住 Alfred；「測試把關」第 1 步看得到放棄、stop 會中斷基準，以及 resume 後重跑的說明 |

## 背景

- 使用者放棄 P0005 時，Alfred 正在跑它的基準測試（tdd-crap 的 quality.py 加 coverage，跑完整套要好幾分鐘）；planAbandoned 已經寫進事件，但測試一直跑到結束才停，期間 Alfred 不處理任何其他事。
- P0004 當時以「每份 plan 只跑一次，而且基準記下來之前這份 plan 本來就不能派工」為由，把基準留在同步執行；這份 plan 改變那個決定，理由是基準可能跑很久，期間必須能放棄和停止。

## 範圍

- 不包含：總覽或控制台顯示「基準測試中」的狀態、讓測試本身變快、改變基準與任務測試的比對規則。
