---
id: P0008
created: 2026-10-28
area: [後端/測試把關, 後端/Agent]
---

# writer 只跑相關測試、完整品質檢查交給 conductor，agent 交出結果後就結束它的程式樹

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 分層驗證 | writer 開發時只跑跟改動相關的測試，幾十秒內看到結果、同一輪自己修；完整測試（tdd-crap 專案是品質檢查）只由 conductor 在任務寫完後的「測試中」跑一次當把關，不再要求 writer 自己跑完整檢查；完整檢查約 11 分鐘，超過 Claude Code Bash 工具 10 分鐘的上限，會被自動轉到背景 | D1 board_prompts.py 調整 writer 的測試指引：有 testCommand 時，testExecutionLines 改成「開發時用測試指令前綴追加參數，只跑跟這次改動相關的測試；交付後流程會跑完整測試，不要自己跑完整測試，也不要用背景執行跑測試」；tdd-crap 專案的 tddWriteLines 改成「Red、Green、回歸都用 python -m pytest 跑相關測試；tdd-crap 第 6 步的品質檢查由流程在交付後執行，不必自己跑」，並說明品質檢查沒通過被退回時去讀 worktree 的 .quality/report.json；testFailureLines 在 tdd-crap 專案也提示讀 .quality/report.json；skipTests 與沒有 testCommand 的文字不變；Claude 與 Codex 兩種 writer 都套用 | 從 writer 的允許清單移除完整測試指令：非 tdd-crap 專案的相關測試也是用同一個前綴追加參數，移除會一起被擋；請 writer 改跑品質檢查但只量相關檔案：允許清單要求照原樣用完整前綴，--source 會含所有來源，只跑部分測試時沒被呼叫的函式一律 0% 而失敗；保留 writer 跑完整檢查、只延長工具逾時：Bash 工具上限是 Claude Code 的設定，conductor 管不到 | T1 調整 writer prompt 的測試指引：只跑相關測試、完整測試由流程跑、不用背景執行，品質檢查被退回時讀 .quality/report.json；補 tests/test_board_prompts.py | | 執行 python -m pytest -q 通過；buildWritePrompt 在 tdd-crap 的 Claude 與 Codex 兩種 writer、只有 testCommand、skipTests 四種設定下，prompt 文字各自符合 D1；tests/test_board_setup.py 既有的 prompt 檢查照樣通過；實際跑一個任務時，writer 的 log 裡沒有執行完整品質檢查，也沒有 run_in_background 的測試 |
| R2 conductor收到結果就結束程式樹 | agent 交出最終結果後，conductor 不再等 CLI 程式自己結束：Claude writer 回報完成後，背景還在跑的測試讓 claude.exe 一直不退出，任務停在「寫程式中」十幾分鐘，總覽還誤報「卡住」；改成收到結果就照常進入下一步，並結束 agent 留下的整個程式樹 | D2 board_agents.py 的 AgentJob 記下第一次解析到最終結果的時間（Claude 的 result 事件，由 parseClaudeLine 寫進 activity["result"]）；poll 看到已有結果、程式還沒結束時，先等一小段寬限時間（新增常數，讓 CLI 正常收尾、寫完 session）；超過寬限才結束整個程式樹，並標記成「交出結果後由流程結束」，不設 killed，所以 finishWrite、finishReview 照常讀結果、commit、送測試或寫審查結果，不會變成 taskInterrupted；結果是 is_error 時照原本流程寫 agentFailed；真正的暫停、放棄、逾時仍走原本的 kill 與 killed 判斷；寫程式和審查共用 AgentJob，兩者都適用 | 等程式自己結束（現況）：背景任務可能跑十幾分鐘，期間任務卡在寫程式中；收到結果後不結束程式樹、放著不管：背景程式還在改 worktree，conductor 一邊 commit 一邊有東西在動檔案，還會留下沒人管的程式；收到結果立刻結束、不給寬限：CLI 可能還在寫 session 檔，下一輪 --resume 接不回對話；等背景任務結束看有沒有第二個 result：不確定 CLI 在 -p 模式會不會再讓模型回覆，而且 writer 已經交卷 | T2 AgentJob 收到最終結果後，寬限時間內沒結束就結束程式樹並標記，finishWrite、finishReview 照常處理；補 tests/test_board_agents.py、tests/fakes/fake_agent.py 和 tests/test_conductor.py | | 執行 python -m pytest -q 通過；新增的測試用假 agent 印出 result 後再開一個會睡很久的子程式：寬限時間過後 poll 回報結束，子程式已被結束，接著有 taskWritten（或審查的 reviewPassed／reviewFailed），沒有 taskInterrupted 和 agentFailed；印出 result 後立刻自己退出的假 agent 行為不變；result 是 is_error 時寫 agentFailed；沒有 result 卻逾時仍記成超時；寫程式中暫停仍記 taskInterrupted；下一輪 writer 仍以 --resume 接回同一個 session |
| R1 | | D3 README 的 testCommand 說明與「測試把關」補上分層：writer 只跑相關測試，完整測試（tdd-crap 是品質檢查）由 Alfred 在「測試中」跑；以及 agent 交出結果後，Alfred 等一小段寬限時間就結束它留下的程式，任務照常往下走 | 只改程式不改 README：使用者看 README 會以為 writer 每輪都跑完整檢查 | T3 更新 README 的 testCommand 說明與「測試把關」，說明分層驗證與交出結果後結束程式樹 | T1, T2 | README 看得到 writer 只跑相關測試、完整測試由 Alfred 在「測試中」跑 |
| R2 | | D3 | | T3 | | README 看得到 agent 交出結果後，Alfred 等寬限時間過了就結束它留下的程式，任務照常往下走 |

## 背景

- 2026-10-28 P0007 T1 第 1 輪：writer 21:28:47 已交出結果，但它啟動的完整品質檢查（被 Claude Code 自動轉到背景，之後又明確用背景執行跑了一次）讓 claude.exe 不退出，任務停在「寫程式中」，總覽跳出「卡住」。
- 同一天量測：完整測試純 pytest 約 7 分半到 11 分（視 CPU 負載），加上 coverage 約 11 分半；不碰瀏覽器與 conductor 整合測試的測試檔，大多在 20 秒內跑完，所以「只跑相關測試」可行。
- 依據 Obsidian 筆記「Harness 工程」與「Harness 工程的實作應用」：讓 agent 有快速的回饋（眼睛），規則寫成機械約束而不是靠 agent 記得。

## 範圍

- 不包含：不穩定測試的重跑規則、讓完整測試變快（瀏覽器測試共用、依改動選測試）、限制同時跑的完整測試數量；這些之後另寫 plan。
- Codex 只套用 R1 的 prompt 調整；R2 依 Claude 的 result 事件判斷，目前沒觀察到 Codex 交出結果後不退出，不改 Codex 的結束判斷。
- 這份 plan 會改到 conductor.py，跟進行中的 P0007 T1 重疊；P0007 合併回 main 之後再核准，避免兩份 plan 分支在 conductor.py 衝突。
