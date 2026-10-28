---
id: P0004
created: 2026-10-28
area: [後端/測試把關, 前端/總覽]
---

# 任務測試改在背景執行、不再擋住 Alfred，跑測試時任務顯示「測試中」

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 「我怎麼好幾次看到writer寫完後過一會才會開始審查？」「把測試加進去」 | writer 寫完後，conductor 在 finishWrite 裡同步執行 testCommand（本專案一次要 3～4.5 分鐘），這段時間整個 Alfred 不派工、不合併、不送審，兩個任務差不多同時寫完時測試要排隊（P0002 T5 第 2 輪等了 6 分多鐘）；而且這段時間任務狀態已經是「待審查」，畫面看起來像卡住。要改成：任務測試在背景跑、不擋其他工作，多個任務的測試可以同時跑；跑測試期間任務顯示「測試中」 | D1 board_core 的 TaskStatus 新增 testing ＝「測試中」；taskWritten 事件的 data 帶 testing（布林），onTaskWritten 在 testing 為 true 時把狀態設成測試中，否則照舊設成待審查（舊事件沒有這個欄位，推導出的狀態跟改之前一樣）；onTestRun 在 verdict 不是 newFailure、而且目前狀態是測試中時改成待審查，newFailure 的處理不變；board_api 的 busyTaskOf 不改，跑測試時 writer 顯示閒置、可以接下一個任務 | 不加新狀態、只在畫面上用「待審查而且沒有 lastTest」推測測試中：重播事件時分不出測試是跑完了還是還沒跑，conductor 也要靠同一個推測決定能不能送審 | T1 board_core 新增「測試中」狀態：taskWritten 帶 testing 時進入測試中，testRun 不是 newFailure 時轉成待審查；補 tests/test_board_state_tasks.py | | 執行 python -m pytest -q 通過，新增的測試確認：taskWritten 帶 testing: true 後狀態是「測試中」，接著 verdict 為 passed 或 preexisting 的 testRun 後是「待審查」，newFailure 時照舊回到排隊並加一輪；沒有 testing 欄位的舊 taskWritten 推導出「待審查」，跟改之前一樣 |
| R1 | | D2 conductor 的 finishWrite 不再呼叫 runTests，改在 taskWritten 的 data 寫 testing＝有沒有 testCommand；tick 在 collectFinishedJobs 之後新增 startTests：沒暫停時，對狀態是測試中、不在 self.jobs 的任務，在任務 worktree 用背景執行緒呼叫既有的 runTestCommand（逾時、結束整個程式樹的行為不變），以 kind "test" 放進 self.jobs；測試不佔 writer、reviewer 的 lane，不限同時數量；collectFinishedJobs 看到 test job 結束時，在主執行緒照原本規則對照基準算 verdict、寫 testRun，事件只由主執行緒寫；stopJobs（暫停、Ctrl+C、任務或 plan 放棄）遇到 test job 時結束測試的整個程式樹，不寫 taskInterrupted，狀態留在測試中，繼續或重開 Alfred 後 startTests 會重跑；recoverInterrupted 不處理測試中；plan 開始時的基準測試維持同步；刪掉 runTestCommand 上「測試在這裡同步執行」的註解 | 用 multiprocessing 或另開一個測試 conductor：要多一套行程間溝通，背景執行緒等 subprocess 就夠；限制同時跑的測試數量：同時進行的任務數已經被 writer 數量限制住；基準測試也改背景：每份 plan 只跑一次，而且基準記下來之前這份 plan 本來就不能派工；改用 pytest-xdist 平行跑來加速：要多裝套件，測試裡有 git worktree 和事件檔，平行執行是否安全要另外確認 | T2 conductor 改成在背景跑任務測試：finishWrite 只寫帶 testing 的 taskWritten，新增 startTests 和 test job，collectFinishedJobs 寫 testRun，stopJobs 結束測試程式樹；補 tests/test_conductor.py | T1 | 執行 python -m pytest -q 通過，新增的測試用會睡幾秒的假 testCommand 確認：兩個任務的測試期間重疊（第二個任務的測試在第一個的 testRun 之前就開始）；測試跑著的時候另一個任務的 taskStarted 或 reviewStarted 出現在這個任務的 testRun 之前；測試中暫停再繼續，該輪只有一個 testRun、沒有 taskInterrupted；測試中放棄任務，測試程式被結束、沒有 testRun；測試中重開 Alfred 會重跑測試並寫出 testRun；既有的測試把關、逾時、輸出管線的測試照樣通過 |
| R1 | | D3 儀表板總覽的 Alfred 卡片「目前任務」在「進行中 N 份 plan」下面列出狀態是「測試中」的任務，格式「測試中：P0004 T1」，沒有就不顯示；資料用現有的 state.tasks，不改 board_api | 在寫程式或審查的角色卡片顯示：測試不是 agent 在做的事，算在 writer 身上會讓它看起來還在忙 | T3 總覽 Alfred 卡片列出測試中的任務；補 tests/test_web.py | T1 | 執行 python -m pytest -q 通過；使用者用 plan 分支 worktree 裡的 server.py 執行 python server.py D:\mamd-v1 --port 8766，開它印出的網址，有任務在跑測試時總覽的 Alfred 卡片出現「測試中：P0004 T2」這種文字，測試跑完就消失 |
| R1 | | D4 README「使用」第 1 步的 testCommand 說明改成：任務測試在背景執行，不擋派工、合併和審查，多個任務可以同時跑；plan 開始時的基準測試仍然同步執行，跑基準時 Alfred 不排新工作；「測試把關」第 2 步加上任務寫完後狀態是「測試中」，測試結果出來才變成待審查或退回，暫停或重開 Alfred 時跑到一半的測試會重跑 | 只改程式不改 README：README 目前寫「跑測試時 Alfred 不會排新工作」，會跟實際行為不符 | T4 更新 README 的 testCommand 說明和「測試把關」，說明背景測試、「測試中」狀態和總覽的顯示 | T2, T3 | README「使用」第 1 步不再寫任務測試會擋住 Alfred，「測試把關」第 2 步看得到「測試中」和重跑的說明 |

## 範圍

- 需求出自 2026-10-28 的對話：使用者問為什麼 writer 寫完後過一會才開始審查，Claude 查 `.board/events.jsonl` 發現是同步執行的任務測試，提了「顯示測試中」「測試改背景執行」「讓測試變快」三個方向；使用者回覆「寫 P0004 plan，把測試加進去，codex不用改」。
- 不包含：同一次對話提到的 Codex reviewer context 一直顯示「計算中」的問題（使用者說 codex 不用改）；基準測試改背景；讓測試本身變快。
