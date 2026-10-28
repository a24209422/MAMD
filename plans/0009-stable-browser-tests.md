---
id: P0009
created: 2026-10-28
area: [測試/儀表板]
---

# 瀏覽器測試偶爾畫不出內容時重畫一次並留下診斷，不再讓 Alfred 誤判成新增的失敗

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 照這個順序，先寫瀏覽器測試的 plan | tests/test_web.py 的瀏覽器測試在 CPU 負載高時偶爾拿到沒有資料的畫面（例如找不到「開始寫程式」），單獨重跑就過；Alfred 把它判成新增的失敗、退回 writer，白跑一輪 13 分鐘的測試；要讓這類偶發的「畫面還沒有資料」不再造成失敗，真的壞掉時仍然失敗，並且留下足以找出原因的線索 | D1 tests/test_web.py 新增 renderUntil(tmp_path, url, ready, *extraArgs)：呼叫既有的 renderDom，ready(dom) 不成立時用新的 user-data-dir 重畫，最多 3 次；任何一次成立就回傳那次的 DOM，若用了不只一次，用 warnings.warn 記下測試名稱和次數，讓 pytest 結尾的 warnings summary 看得到；3 次都不成立時 assert 失敗，訊息附上每次的耗時、瀏覽器 stderr 最後幾行、這段期間 server 收到的請求路徑與狀態碼；renderDom 改成也回傳 stderr 與耗時，dashboard fixture 記錄 server 收到的請求（路徑、狀態碼、是否丟出例外），既有的 renderDom 呼叫行為不變 | 加大 --virtual-time-budget：2026-10-28 實驗，API 晚 8 秒回應、預算 1 秒仍拿得到資料，虛擬時間會等 fetch，加大預算不一定對症；pytest-rerunfailures 整個測試重跑：要多裝套件，而且會連斷言失敗一起重跑，真正的錯誤也會被重跑掩蓋；Alfred 判定新增失敗前先重跑失敗的測試：會改 conductor.py，跟 P0008 衝突，留給之後的「不穩定測試」plan；改用 CDP 或 Playwright 等條件成立再取畫面：要多裝套件或自己實作 websocket，範圍太大 | T1 新增 renderUntil 與診斷資訊：renderDom 回傳 stderr 與耗時、dashboard fixture 記錄 server 請求、重畫最多 3 次並在重試時發出 warning；補測試確認重試與失敗訊息 | | 執行 python -m pytest -q tests/test_web.py 通過；新增的測試用 server 前兩次 /api/state 回 503 的替身，renderUntil 第 3 次成立、測試通過、warnings summary 看得到重試次數；用永遠不成立的 ready，測試失敗且訊息列出 3 次的耗時、stderr 與 server 請求和狀態碼 |
| R1 | | D2 tests/test_web.py 每個用瀏覽器的測試改用 renderUntil，ready 用該測試第一個斷言要看的內容（例如「開始寫程式」或 data-probe 已寫出），只判斷「畫面已經有資料」，斷言本身不放寬也不重跑；不用瀏覽器的測試不動 | 每個測試各自寫重試迴圈：21 個測試重複同一段邏輯，規則改了要改 21 處；ready 一律用同一個條件（例如 /api/state 讀到過）：各測試要看的頁面與元件不同，有的要等點擊後的量測，單一條件判斷不了 | T2 把 test_web.py 用瀏覽器的測試改用 renderUntil，ready 用各測試第一個要看的內容 | T1 | 執行 python -m pytest -q tests/test_web.py 通過，改用 renderUntil 的測試數等於原本呼叫 renderDom 的瀏覽器測試數；同時在另一個終端機跑一份完整測試製造負載，再跑 python -m pytest -q tests/test_web.py，仍然通過，有重畫時 warnings summary 會列出是哪個測試 |

## 背景

- 2026-10-28 出現 3 次：test_consoleShowsBusyAgentDetail（合併後跑完整測試時）、test_opsFeedAndBadges（P0007 T2 的任務測試，被判成新增的失敗，退回 writer）；畫面裡有頁面骨架，但沒有從 /api/state 畫出來的內容；單獨重跑都通過（test_web.py 單獨 25 passed、test_opsFeedAndBadges 單獨 16 秒通過）。
- 同一天量測：test_web.py 單獨跑 122 秒，負載高時 304 秒，慢 2.5 倍，整套只慢 1.5 倍，瀏覽器測試對負載特別敏感。
- 同一天實驗（Edge headless，--virtual-time-budget=1000）：fetch 晚 0.2、3、8 秒回應，dump-dom 都拿得到資料；所以「server 太慢」不是唯一可能，失敗原因還不確定，T1 的診斷資訊就是為了下次失敗時查得出來。

## 範圍

- 不包含：modules/system_monitor/tests/test_browser.py（目前沒有觀察到失敗）、Alfred 端的不穩定測試重跑、讓瀏覽器測試變快（共用瀏覽器）、tests/test_conductor.py::test_noReviewUntilBaselineForChangedCommandIsRecorded 的不穩定（不是瀏覽器測試，另外處理）。
- 只改測試，不改 web/ 與 server.py 的產品程式。
