---
id: P0011
created: 2026-10-29
area: [測試/儀表板, 測試/系統監控]
---

# 瀏覽器測試改用 Playwright 共用一個 Edge，等畫面真的畫好再檢查

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 幫我寫瀏覽器測試共用瀏覽器的 plan | 瀏覽器測試現在每個測試各開一次 Edge（--headless --dump-dom，每次約 1.2 秒，冷啟動 2.3 秒），整段測試只開一個 Edge、每個測試用自己的乾淨分頁；同時改成等畫面真的出現要檢查的內容再取 DOM，取代固定的虛擬時間預算，從根本處理「script 還沒載完就印出畫面」的不穩定；使用者選了 Playwright | D1 新增 tests/browser.py 共用模組：session 範圍的 fixture 用 Playwright 啟動一次 Edge（executable_path 沿用既有 findBrowser 找到的 Edge 或 Chrome，不另外下載瀏覽器），每個測試開新的 browser context 與 page，測試結束就關掉，彼此不共用 cookie、storage；提供 renderPage(url, ready, viewport=None)：載入網址後用 wait_for_function 等 ready 成立再回傳 page.content()；沿用 P0009 的 renderUntil 介面與規則：等不到時換新的 context 重來，最多 3 次，重來時發出 warning，3 次都不成立時 assert 失敗並附上每次的耗時、瀏覽器 console 的錯誤、失敗的請求、server 收到的請求與狀態碼；找不到 Playwright 或 Edge／Chrome 時跳過並寫明原因；新增 requirements-dev.txt 鎖定 Playwright 版本 | 自己寫 CDP 用戶端：使用者選了 Playwright；改用 Playwright 內建下載的 Chromium：要多下載約 150MB，而且跟使用者實際用的 Edge 不同；整個 session 共用同一個 page：前一個測試的 localStorage、事件監聽會影響下一個，失敗時也難判斷是誰造成的；每個測試仍各自啟動瀏覽器只換成 Playwright：省不到啟動時間 | T1 新增 tests/browser.py：session 範圍啟動一次 Edge、每個測試新 context，renderPage 等 ready 成立再取 DOM，renderUntil 沿用 P0009 的重來與診斷；新增 requirements-dev.txt；補 tests/test_browser_helpers.py | | 執行 python -m pytest -q tests/test_browser_helpers.py 通過；新增的測試確認：同一個 session 裡 Edge 只啟動一次；兩個測試各自寫 localStorage 不會互相看到；ready 晚 3 秒才成立仍拿到完整畫面；ready 一直不成立時重來 3 次後失敗，訊息有耗時、console 錯誤與 server 請求；前兩次失敗、第 3 次成立時通過並有 warning；沒裝 Playwright 時測試顯示跳過原因 |
| R1 | | D2 tests/test_web.py 的瀏覽器測試改用 tests/browser.py：renderDom、renderUntil 改成呼叫共用模組，ready 用各測試第一個要看的內容（例如「開始寫程式」、data-probe 已寫出）；原本靠 --virtual-time-budget 讓計時器先跑幾輪的測試，改成等條件成立，需要等好幾輪輪詢時用 Playwright 的 page.clock 快轉時間；--window-size 改用 viewport；斷言內容不變、不放寬；addProbe 的機制保留 | 只把 dump-dom 換成 page.content()、保留固定等待時間：沒有解決 script 還沒載完的問題；每個測試改用 Playwright 的 locator 斷言：要重寫所有斷言，範圍太大，也會改到測試在驗證什麼 | T2 tests/test_web.py 的瀏覽器測試改用共用瀏覽器，等條件成立再取畫面，計時器用 page.clock 快轉，斷言不變 | T1 | 執行 python -m pytest -q tests/test_web.py 通過，測試數和改之前一樣，沒有測試被改成跳過；單獨跑 tests/test_web.py 的時間比改之前（2026-10-28 實測 122 秒）短，實際秒數寫在任務回報裡；同時在另一個終端機跑一份完整測試製造負載，再跑 tests/test_web.py 仍通過 |
| R1 | | D3 modules/system_monitor/tests/test_browser.py 也改用 tests/browser.py：它原本用 --virtual-time-budget 讓監控頁輪詢好幾輪，改成用 page.clock 快轉，並等量測結果寫進 data-probe 再取畫面；2026-10-28 P0009 T1 的完整測試中，這個檔案的 test_outageShowsNoFakeZeroAndRecovers 也出現「頁面裡的量測沒有跑完」 | 這個檔案維持原樣：同一份測試會同時有兩種瀏覽器寫法，而且它在負載下也會出現同樣的不穩定 | T3 系統監控模組的瀏覽器測試改用共用瀏覽器，輪詢用 page.clock 快轉 | T1 | 執行 python -m pytest -q modules/system_monitor/tests 通過，測試數不變；同時跑一份完整測試製造負載時仍通過 |
| R1 | | D4 README 第 330 行附近的測試說明改成：網頁畫面的測試用 Playwright 控制電腦上的 Edge（或 Chrome），整段測試只開一次瀏覽器；先執行 python -m pip install -r requirements-dev.txt；找不到 Playwright 或瀏覽器時跳過 | 只改程式不改 README：別台電腦 clone 後不知道要裝 Playwright，瀏覽器測試會全部跳過而沒發現 | T4 更新 README 的測試說明，寫明 requirements-dev.txt 的安裝方式與跳過條件 | T1 | README 看得到安裝 requirements-dev.txt 的指令，以及沒裝時瀏覽器測試會跳過 |

## 前提

- 核准前，使用者先在專案的 .venv 安裝 Playwright：`D:\MAMD\.venv\Scripts\python.exe -m pip install playwright`。writer 只能執行測試指令，不能自己安裝套件；T1 把安裝好的版本寫進 requirements-dev.txt。
- 這份 plan 會改 tests/test_web.py，跟進行中的 P0009 重疊；P0009 合併回 main 之後再核准。

## 背景

- 2026-10-28 量測：tests/test_web.py 單獨跑 122 秒，負載高時 304 秒，佔完整測試約一半；最慢的 25 個測試裡有 18 個是瀏覽器測試。
- 2026-10-29 量測：Edge headless 開一次約 1.2 秒，冷啟動約 2.3 秒；21 個瀏覽器測試光開瀏覽器就要約 25 到 50 秒。
- P0009 T1 的診斷資訊找出不穩定的原因：負載高時 server 只收到 /js/models.js 之前的請求，沒收到 /js/main.js 和 /api/state，Edge 在 script 載完前就印出畫面；固定的虛擬時間預算無法保證 script 載完，所以改成等條件成立。

## 範圍

- 不包含：每個測試的 dashboard fixture（建 git 專案、送出與核准 plan、啟動 server）改成共用；Alfred 端的不穩定測試重跑；限制同時跑的完整測試數量（P0012）。
- 只改測試與測試依賴，不改 web/、server.py 的產品程式。
