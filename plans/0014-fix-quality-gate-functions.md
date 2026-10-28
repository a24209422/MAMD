---
id: P0014
created: 2026-10-29
area: [後端/conductor, 後端/CLI, 後端/RTM, 後端/儀表板, 後端/Agent, 後端/統計]
---

# 把品質門檻超標的 35 個函式修到門檻內：複雜度超標的重構，覆蓋率不足的補測試

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 兩個都做，分兩份 plan | tdd-crap 的門檻是每個函式覆蓋率至少 80%、循環複雜度最多 10；目前有 35 個函式超標（13 個複雜度、22 個覆蓋率），讓 testCommand 一律失敗；這份是「修 35 個函式」那一份，比對規則另寫 P0013；使用者選了走 Alfred 流程 | D1 所有任務共用的規則：複雜度超標的函式只做不改行為的重構（抽出小函式、用對照表取代長串 if），既有測試的斷言不改；重構前先確認既有測試涵蓋要動的分支，不夠的先補測試再重構；覆蓋率不足的函式補真的在驗證行為的測試；平台專屬分支（例如非 Windows 的 os.killpg、isHuman 的 POSIX 分支）用 monkeypatch 模擬，不用 `# pragma: no cover` 排除；程式進入點（main、runForever）用替身驗證它呼叫了什麼；不得放寬門檻、刪測試或弱化斷言；每個任務只改自己列出的函式與對應的測試檔，不順手重構其他函式；任務完成時，用品質檢查確認列出的函式都是 PASS | 為了過門檻加 `# pragma: no cover`：tdd-crap 規定不得任意排除；調高門檻：使用者選的是修函式；一次改完全部：審查看不完，而且同一個檔案的重構和補測試混在一起，退回時不好定位 | T1 rtm_graph.py 重構 RtmGraph.addRow（複雜度 28）、analyzePlan（13）、RtmGraph.finish（12），補 tests/test_rtm_graph.py | | 執行 python -m pytest -q 通過，既有的 rtm_graph 測試沒有改斷言；品質檢查輸出裡 RtmGraph.addRow、analyzePlan、RtmGraph.finish 是 PASS |
| R1 | | D1 | | T2 rtm_graph.py 補 printSummary（覆蓋率 0%）、buildSingle（0%）、main（62%）、formatList（67%）的測試 | T1 | 執行 python -m pytest -q 通過；品質檢查輸出裡這 4 個函式是 PASS |
| R1 | | D1 | | T3 conductor.py 重構 Conductor.mergeOneTask（複雜度 16）、Conductor.finishWrite（14）、formatEvent（13），補 tests/test_conductor.py | | 執行 python -m pytest -q 通過，既有的 conductor 測試沒有改斷言；品質檢查輸出裡這 3 個函式是 PASS |
| R1 | | D1 | | T4 conductor.py 補 Conductor.runForever（0%）、processAlive（30%）、Conductor.startReviews（67%）、Conductor.startWrites（78%）的測試 | T3 | 執行 python -m pytest -q 通過；品質檢查輸出裡這 4 個函式是 PASS |
| R1 | | D1 | | T5 board.py 重構 cmdInit（複雜度 34）、cmdStatus（12），補 tests/test_board_cli.py | | 執行 python -m pytest -q 通過，既有的 CLI 測試沒有改斷言；品質檢查輸出裡 cmdInit、cmdStatus 是 PASS；board.py init、status 的實際輸出跟改之前相同 |
| R1 | | D1 | | T6 board.py 補 missingPackages（0%）、isHuman（67%）、hasWindowsConsole（78%）的測試 | T5 | 執行 python -m pytest -q 通過；品質檢查輸出裡這 3 個函式是 PASS |
| R1 | | D1 | | T7 board_logs.py 重構 summarizeRecords（複雜度 21）、claudeDetail（17），補 toolTarget（覆蓋率 38%）、toolResultText（40%）的測試 | | 執行 python -m pytest -q 通過，既有的 log 解析測試沒有改斷言；品質檢查輸出裡這 4 個函式是 PASS |
| R1 | | D1 | | T8 server.py 重構 DashboardHandler.routePost（複雜度 11），補 main（覆蓋率 33%）、Dashboard.modelCommand（73%）、Dashboard.runCommand（75%）的測試 | | 執行 python -m pytest -q 通過，既有的 server 與網頁測試沒有改斷言；品質檢查輸出裡這 4 個函式是 PASS |
| R1 | | D1 | | T9 board_agents.py 補 killProcessTree（覆蓋率 43%）、codexFinalResult（78%）、AgentJob.kill（75%）的測試 | | 執行 python -m pytest -q 通過；品質檢查輸出裡這 3 個函式是 PASS |
| R1 | | D1 | | T10 重構 board_stats.py 的 collectStats（複雜度 15）與 board_review.py 的 validateReview（14），補 tests/test_board_stats.py 與 tests/test_board_review.py | | 執行 python -m pytest -q 通過，既有的統計與審查格式測試沒有改斷言；品質檢查輸出裡 collectStats、validateReview 是 PASS |
| R1 | | D1 | | T11 補 modules/system_monitor/metrics.py 的 volumeLabel（覆蓋率 43%）、board_api.py 的 lastEventTime（75%）、board_git.py 的 addWorktree（75%）的測試 | | 執行 python -m pytest -q 通過；品質檢查輸出裡這 3 個函式是 PASS |
| R1 | | D2 全部任務合併後，品質檢查的每個函式都是 PASS，testCommand 在 pytest 全過時結果是通過 | 只看各任務自己的函式：任務之間可能互相影響覆蓋率（例如重構時刪掉了別的測試會走到的路徑） | T12 全部合併後跑一次完整品質檢查，確認沒有任何 FAIL；有的話只修造成 FAIL 的地方 | T2, T4, T6, T7, T8, T9, T10, T11 | 執行 .board/config.json 的 testCommand，結尾是 PASS，輸出裡沒有任何 FAIL 行 |

## 背景

- 清單來自 2026-10-29 直接改 P0008 後的完整品質檢查輸出（排除了不穩定的 test_noReviewUntilBaselineForChangedCommandIsRecorded），AgentJob.stopAfterResult 當次已補測試修好，不在清單內。
- 覆蓋率是排除那個測試後量的，數字可能和實際略有出入；每個任務開始前先用品質檢查確認自己列出的函式現在的數字。

## 範圍

- 不包含：比對規則（P0013）；修 test_noReviewUntilBaselineForChangedCommandIsRecorded 的不穩定；改門檻。
- 建議 P0013 先合併再核准這份：P0013 合併前，品質門檻的失敗沒有名稱，這份 plan 的任務測試把關會失準。
- T3、T4 會改 conductor.py，跟待核准的 P0010、P0012、P0013 重疊，一份合併回 main 後再核准另一份。
