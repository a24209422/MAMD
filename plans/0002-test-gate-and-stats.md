---
id: P0002
created: 2026-10-28
area: [後端/測試把關, 後端/統計, 前端/任務]
---

# 測試出現新增的失敗就直接退回 writer，並從事件紀錄算出每份 plan 的成效

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 「第 1 項用 (b)」：測試沒過就不送審；plan 開始時先跑一次測試當基準，只擋新增的失敗 | 有設定 testCommand 時，plan 核准後先在 baseCommit 跑一次測試當基準；之後任務測試失敗就跟基準比，有新增的失敗就直接退回 writer、不叫 reviewer，算一輪；基準本來就有的失敗不擋，照常送審。「新增」靠 pytest short test summary 裡的 FAILED／ERROR 測試名稱分辨，分不出名稱時只比通過或失敗，完整規則見下文「測試判定」；skipTests 或沒有 testCommand 時行為不變 | D1 基準在暫時的 detached worktree（`<專案>-worktrees/<plan>-baseline`）checkout baseCommit 後執行，跑完就移除，已存在就先移除再建；結果記成 plan 層級的 testBaseline 事件，board_core 記進 plan 狀態；有 testCommand 的 plan 在基準記錄前不派工；基準記錄的指令和目前的 testCommand 不同時重跑；cleanup 一併移除殘留的基準 worktree | 在 plan worktree 跑基準：測試產物會留在 plan worktree，之後合併任務可能被未追蹤檔案擋下；每次任務測試失敗時再到 plan 分支跑一次：每次失敗多一次完整測試，Alfred 同步等待更久 | T1 新增基準測試：board_git 建立與移除暫時 worktree，conductor 派工前跑基準並寫 testBaseline 事件，board_core 記進 plan 狀態，加上事件標籤；補 board_git、conductor、plan 狀態的測試 | | 執行 python -m pytest -q 通過，新增的測試確認：testBaseline 出現在該 plan 第一個 taskStarted 之前；跑完後 `<專案>-worktrees` 底下和 git worktree list 都沒有基準 worktree；testCommand 改掉並重開 Alfred 後會再跑一次基準；skipTests 的專案不跑基準、照常派工 |
| R1 | | D2 任務測試跑完後，conductor 依「測試判定」算出 verdict（passed、newFailure、preexisting、unknown）和 newFailures，寫進 testRun 事件；board_core 只照事件裡的 verdict 改狀態：newFailure 和審查不通過一樣 round 加 1 回到排隊，輪數用完停在「等你處理」（kind 是 tests），decide retry 時輪數上限加 1；沒有 verdict 的舊 testRun 事件照舊只記錄 | 改 reviewer 規則，要它「測試失敗一定判不通過」：還是要付一次審查，也分不出基準就有的失敗；在 board_core 推導狀態時才算 verdict：要讀基準和完整輸出，舊事件重播的結果會跟當時不同 | T2 任務測試跟基準比對並決定是否退回：conductor 算出 verdict 寫進 testRun，board_core 處理 newFailure 的退回、輪數用完和 decide retry；更新既有 testRun 相關測試 | T1 | 執行 python -m pytest -q 通過，新增的測試確認：基準通過、任務測試失敗時，下一個事件是第 2 輪的 taskStarted，中間沒有 reviewStarted；三輪都有新增的失敗時任務停在「等你處理」，原因寫明測試失敗，decide retry 後再跑一輪；基準就失敗、失敗名稱相同時照常送審；沒有 verdict 的舊 testRun 事件推導出的狀態跟改之前一樣 |
| R1 | | D3 退回的那一輪，write prompt 附上新增失敗的測試名稱和測試輸出最後 50 行，不再附上更早的審查意見；verdict 是 preexisting 時，review prompt 列出基準就有的失敗，是 unknown 時說明基準就失敗、無法比對，兩種情況 reviewer 都不必因為這些失敗退回 | 退回時另寫一份審查意見檔到 .board/reviews/：會混進審查紀錄，儀表板會把它當成 reviewer 的結果 | T3 退回 writer 的 prompt 附上新增的失敗和測試輸出，審查 prompt 說明基準就有的失敗；補 board_prompts 測試和假 agent 收到的 prompt 檢查 | T2 | 執行 python -m pytest -q 通過，新增的測試從假 agent 記錄的呼叫確認：被測試退回後第 2 輪的 write prompt 有新增失敗的測試名稱和測試輸出，沒有更早的審查意見；基準就失敗的任務，review prompt 有「基準就有」的說明 |
| R1 | | D4 README 說明基準、測試退回和「測試判定」規則，decide 表加上「測試有新增的失敗、輪數用完」的 retry 效果，註明 pytest 的 -r 要保留 f、E 才分得出新增的失敗；本專案 pytest.ini 的 addopts 從 -rs 改成 -rfEs | 只寫進 docs/AGENTS-snippet.md：那份是給寫 plan 的 agent 看的，使用者讀不到 | T4 更新 README 的測試把關說明和 decide 表，pytest.ini 改成 -rfEs | T3 | 在專案資料夾執行 python -m pytest -q，有測試失敗時結尾的 short test summary 列出 FAILED 測試名稱，跳過原因也照樣列出；README 的「使用」和 decide 表看得到基準與測試退回的說明 |
| R2 「2 寫成 plan」：用 events.jsonl 算指標 | 從現有的 .board/events.jsonl 算出每份 plan 的成效數字，不另外記錄資料；舊事件（例如 P0001）也算得出來，R1 新增的測試退回也算進去；在終端機用 board.py stats 看，儀表板「任務」頁看目前選的 plan；指標定義見下文「統計指標」 | D5 新增 board_stats.py，純函式：輸入事件清單和推導出的狀態，輸出每份 plan 和全部 plan 的指標；只讀事件，不讀 log 或 git | conductor 執行時另外累計數字寫檔：舊事件算不到，兩份資料也可能對不起來；讀 .board/logs/ 算 token 用量：兩種 CLI 的格式不同，不在這次範圍 | T5 新增 board_stats.py 依事件計算「統計指標」，補 tests/test_board_stats.py（涵蓋沒有 verdict 的舊 testRun、解衝突、中斷、放棄和還在等你的任務） | | 執行 python -m pytest -q 通過；測試照 P0001 的事件順序建一份假事件，算出的數字跟下文「統計指標」的 P0001 對照值一致 |
| R2 | | D6 board.py 新增唯讀的 stats 指令，輸出風格沿用 status：不帶 plan 時每份核准過的 plan 一列加上總計，帶 plan 時列出全部指標和每個任務的輪數；不列入只有使用者能執行的指令 | 輸出 JSON 或 CSV：目前只需要人看，之後要匯出再加 | T6 board.py 新增 stats 指令並補 CLI 測試，README 的指令表和說明加上 stats | T4, T5 | 在 D:\mamd-v1 用 plan 分支的 board.py 執行 python board.py stats P0001，數字跟下文 P0001 對照值一致；執行 python board.py stats 列出 P0001 一列和總計 |
| R2 | | D7 board_api 的 planView 加上 stats；「任務」頁在選中的 plan 底下加一個「成效」區塊，顯示同一組指標，還沒核准的 plan 不顯示；不另開分頁 | 另開「統計」分頁：指標都以 plan 為單位，放在 plan 旁邊不用再選一次 | T7 儀表板「任務」頁顯示選中 plan 的成效，補 board_api 和網頁測試，README 儀表板段落加一句 | T6 | 使用者用 plan 分支 worktree 裡的 server.py 執行 python server.py D:\mamd-v1 --port 8766，開它印出的網址，在「任務」頁選 P0001，「成效」區塊的數字跟 board.py stats P0001 一致；選待核准或草稿的 plan 時沒有「成效」區塊 |

## 需求來源與範圍

- 需求出自 2026-10-28 的對話：Claude 依 Obsidian 筆記「Harness 工程的實作應用」提了四項建議，使用者回覆「1 和 2 寫成 plan，第 1 項用 (b)」。
- 第 1 項的建議原文：「測試失敗時，把測試輸出的最後幾行當成必改，直接退回 writer，算一輪，不叫 reviewer」；(b) 的原文：「plan 開始時先跑一次測試當基準，只擋新增的失敗」。
- 第 2 項的建議原文：「可以加一個 `board.py stats`，或在儀表板加一個分頁，列出每份 plan 的平均輪數、一次通過率、人工介入次數、測試失敗率，加上 writer 和 reviewer 用的模型」。這份 plan 兩個都做，儀表板放在「任務」頁，不另開分頁；不要儀表板時退回並刪掉 T7。
- 工具來源是 D:\MAMD-stable；實作目標與這份 plan 所在的專案是 D:\mamd-v1。驗收畫面和指令時用 plan 分支 worktree 裡的程式。
- 不包含：建議的第 3、4 項；基準結果顯示在儀表板；token 用量；跨 plan 的趨勢圖；pytest 以外測試工具的失敗名稱解析。

## 測試判定

失敗名稱：測試完整輸出（不是只有最後 50 行）裡以 `FAILED ` 或 `ERROR ` 開頭的行，取後面到「 - 」之前的部分，也就是 pytest short test summary 的格式。

| 基準 | 任務測試 | verdict | 接下來 |
|---|---|---|---|
| 沒有 testCommand 或 skipTests | 不跑 | 沒有 testRun | 照常送審（跟現在一樣） |
| 任何 | 通過 | passed | 送審 |
| 通過 | 失敗（含超時） | newFailure | 退回 writer |
| 失敗，有失敗名稱 | 失敗，有不在基準裡的名稱，或一個名稱都沒有 | newFailure | 退回 writer |
| 失敗，有失敗名稱 | 失敗，名稱全都在基準裡 | preexisting | 送審，review prompt 列出基準就有的失敗 |
| 失敗，沒有失敗名稱（輸出分不出名稱，或超時） | 失敗 | unknown | 送審，review prompt 說明基準就失敗、無法比對 |

## 事件契約

- `testBaseline`：plan 層級（task 留空）。data 有 passed、failures（失敗名稱清單）、command、commit（baseCommit）、tail（輸出最後 50 行）。
- `testRun` 新增 verdict（passed、newFailure、preexisting、unknown）和 newFailures（不在基準裡的失敗名稱）。沒有這兩個欄位的舊事件照舊只記錄，不改狀態。
- verdict 是 newFailure 而且輪數用完時，任務停在「等你處理」，needs 的 kind 是 tests；decide retry 和審查輪數用完一樣：輪數上限加 1、round 加 1、回到排隊。

## 統計指標

全部從 events.jsonl 和推導出的狀態計算，以 plan 為單位，另外算一份全部 plan 的總計。

| 指標 | 算法 | P0001 對照值 |
|---|---|---|
| 任務 | 完成（taskMerged）、放棄、全部 | 5／0／5 |
| 平均輪數 | 每個任務事件裡最大 round 的平均 | 1.2 |
| 寫完次數 | taskWritten 次數 | 6 |
| 審查 | reviewPassed、reviewFailed 次數 | 6 次（通過 5、不通過 1） |
| 第一次審查就通過 | 第一個審查結果是 reviewPassed 的任務 ÷ 有審查結果的任務 | 4/5 |
| 測試 | testRun 次數、失敗次數、測試退回（verdict 是 newFailure）次數 | 1 次，失敗 0，退回 0 |
| 寫完後被退回 | （reviewFailed＋測試退回）÷ 寫完次數，筆記裡「假完成率」的近似值 | 1/6 |
| 你的決定 | userDecision 次數，依 retry、done、abandon 分開 | 1（retry 1） |
| 還在等你 | 目前狀態是「等你處理」的任務數 | 0 |
| 等你的時間 | 每次 userDecision 距離同一任務前一個事件的時間加總 | 9 分 52 秒 |
| agent 失敗、合併衝突 | agentFailed、taskConflicted 次數 | 0、0 |
| plan 退回 | planRejected 次數 | 0 |
| 耗時 | planApproved 到 planCompleted；還沒完成時算到現在，並標「進行中」 | 1 小時 13 分 29 秒 |
| 模型 | taskStarted、reviewStarted 裡出現過的 cli、model、reasoningEffort 組合 | writer：claude opus xhigh；reviewer：codex gpt-6.1-sol xhigh |
