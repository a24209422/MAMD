---
id: P0001
created: 2026-10-27
area: [後端/系統監控, 前端/系統監控]
---

# 以獨立模組加入 CPU、記憶體與磁碟資源監控

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 先做電腦資源監控就好 | 監控執行 server 的電腦，顯示 CPU 使用率、RAM 已用與可用容量，以及目標專案所在磁碟容量；呈現約定的三張摘要卡、CPU 與 RAM 最近一分鐘走勢，每 3 秒更新 | D1 第一版依現有 Windows 使用環境，以 Python 標準庫取得資源數據；資料採集與快取集中於 modules/system_monitor/metrics.py，透過可注入的採集器及時鐘測試；詳細契約見下文 | 每次輪詢啟動 PowerShell 或其他外部程序：增加採樣開銷；新增第三方監控套件：目前範圍可沿用專案只用標準庫的安裝方式；用固定或隨機數據填滿圖表：無法反映主機狀態 | T1 建立 Windows 資源採集、非阻塞取樣及共用快取，新增 modules/__init__.py、modules/system_monitor/__init__.py、metrics.py 與模組內採集測試 | | 執行模組採集測試，驗證 CPU 差分、首次暖機、RAM 與磁碟單位、單項失敗、平台不支援及並行快取；在 Windows 讀取真實樣本，數值落在有效範圍，磁碟對應傳入專案位置 |
| R2 好，當作一個獨立模組，寫成plan | 此功能的採集、API、畫面、樣式、互動、測試與使用說明集中於 modules/system_monitor；主系統僅處理導覽、掛載及服務接線 | D2 模組內 api.py 封裝唯讀 GET /api/system-monitor 與靜態資產白名單；由既有 server.py 掛接，共用該 server 實例的採集器及現有 localhost／Host 檢查；不引入第二個服務或通用插件框架 | 把採集與畫面直接堆進 server.py、web/index.html：功能分散；另開 server、iframe 或任意目錄靜態服務：增加整合與生命週期成本 | T2 建立模組 API 與靜態資產掛接介面，於 server.py 接線，擴充 tests/test_server.py 的 API、Host 與白名單驗證 | T1 | 執行 API 測試，GET /api/system-monitor 取得固定欄位；合法模組資產具有正確 Content-Type，未知資產及目錄穿越請求為 404，外部 Host 為 403；採集失敗時原有 /api/state 與頁面仍可使用 |
| R1 | | D3 modules/system_monitor/web/monitor.html、monitor.css、monitor.js 提供可掛載的頁面與共用摘要；使用既有字型、色彩變數及原生 SVG 繪圖，單一 3 秒排程更新可見視圖，以真實時間戳保留最多一分鐘資料；載入與錯誤狀態見下文 | 圖表套件或外部 CDN：這兩條簡單走勢不需要額外執行依賴；跟隨既有 2 秒狀態輪詢另發重複請求：取樣頻率與畫面契約不一致 | T3 製作模組內監控卡片、CPU／RAM 走勢、摘要元件及共用更新生命週期，新增模組內前端互動測試與 pytest 包裝 | T2 | 以可控 fetch／時鐘測試首次載入、3 秒更新、慢回應不重疊、切頁與頁籤隱藏停止輪詢、重新進入不重複計時、60 秒資料裁切、真實 0% 與 null 的區別，以及斷線／恢復；執行模組內 JavaScript 語法檢查 |
| R2 | | D4 將空白 topology 導覽改為 system-monitor「系統監控」，舊 #topology 連結導向此頁；web/index.html 僅加入掛載容器與資產引用，web/js/core.js 僅接頁面登錄；控制台既有資源區塊使用同模組摘要，點擊跳到完整頁 | 大量重寫共用導覽或在主頁複製監控元件：超出功能範圍且容易造成兩份顯示邏輯不一致 | T4 接上左側導覽、完整頁與控制台摘要，修改 web/index.html、web/js/core.js 及必要的 web/js/console.js 接點，加入導覽與頁面掛載驗證 | T3 | 在瀏覽器點選「系統監控」可看到三卡及兩張走勢；從控制台摘要可跳入完整頁；#system-monitor 可直接進入，#topology 相容導向；切換總覽、任務、模型與設定仍正常；窄視窗卡片排列且無水平溢出 |
| R1 | | D5 驗收使用修改後的 mamd-v1 server 與模組，沿用既有 pytest、Node 及 Edge／Chrome 測試方式；在模組 README 記錄啟動關係、監控主機、數據口徑及取樣限制 | 只啟動 mamd-stable server 驗收：該程式會讀取 stable 自己的 web 與模組，無法呈現 v1 的修改；僅以靜態文字比對驗收即時監控：無法證明更新與失敗處理有效 | T5 補齊模組 README、真實掛載的瀏覽器驗證及既有測試整合，完成針對性測試與全套回歸 | T4 | 執行 python -m pytest -q，確認新模組測試確實被收集，明示瀏覽器／Node 缺少造成的跳過；由使用者啟動 mamd-v1 server，比對工作管理員觀察 CPU／RAM 隨負載變動、磁碟總量與剩餘容量，並驗證離線不顯示假零值及恢復後更新；回報執行指令與結果 |

## 範圍與執行關係

- 以上需求原文取自本次對話；第一版細節沿用討論的 CPU、RAM、磁碟、每 3 秒更新、最近一分鐘走勢與控制台摘要。
- 工具來源為 D:\mamd-stable；實作目標與此 plan 所在專案為 D:\mamd-v1。後續各任務只修改目標專案。
- mamd-stable 的 board.py／conductor.py 可用於管理 v1 的任務；畫面驗收需執行修改後的 v1 server。成果尚在 plan 分支時，使用該分支對應 worktree 內的 server.py。
- 成果進入 D:\mamd-v1 後，由使用者自行在終端機執行 python D:\mamd-v1\server.py D:\mamd-v1 --port 8766，並使用程式印出的網址；若 8765 已有 stable server，可讓兩者並存。agent 不啟動真人儀表板，也不取得其核准 token。
- 模組顯示的是執行該 server 的主機資源。磁碟以傳入的專案資料夾所在磁碟區為準，畫面顯示其磁碟區標示。
- 第一版範圍為整機資源監控；AI 長期記憶、Context、個別 agent 程序用量、GPU／網路、資料清理及程序控制不包含在本 plan。

## 模組邊界與預計結構

```text
modules/
├─ __init__.py
└─ system_monitor/
   ├─ __init__.py
   ├─ metrics.py
   ├─ api.py
   ├─ README.md
   ├─ web/
   │  ├─ monitor.html
   │  ├─ monitor.css
   │  └─ monitor.js
   └─ tests/
      ├─ test_metrics.py
      ├─ monitor-ui.test.cjs
      ├─ test_monitor_ui.py
      └─ test_browser.py
```

- modules/system_monitor 不依賴 board 的 plan、agent 或事件狀態；由主系統傳入專案位置、掛載節點與必要的 API／頁面生命週期能力。
- 採集器、JSON 轉換、頁面模板、樣式與前端互動留在模組；主系統不另存一份採集或顯示實作。
- API 對外暴露固定監控路由；靜態檔案只開放 monitor.html、monitor.css、monitor.js，模組的 Python、測試與 README 不可被下載。
- 既有 server.py 的靜態服務目前僅以 web/ 為根，需加入針對此模組的白名單掛接，不能只把檔案放進資料夾就視為完成。
- pytest.ini 目前只收集 tests/；T5 將模組測試目錄加入 testpaths，保留現有 tests/。JavaScript 檢查也要明確包含模組目錄。
- 任務改動檔案的責任：T1 採集與其測試；T2 API、server.py 與 tests/test_server.py；T3 前端資產及前端單元測試；T4 主介面掛載及導覽測試；T5 模組說明、瀏覽器整合測試及測試收集。依序 T1 → T2 → T3 → T4 → T5，後續若必須修改前置任務的檔案，仍依此順序執行。

## 採集與 API 契約

- 第一版支援現有 Windows 環境。CPU 與 RAM 以 ctypes 呼叫 Windows API；磁碟以 shutil.disk_usage(projectDir) 取得。沿用標準庫，不要求安裝新套件。
- CPU 使用 GetSystemTimes 的相鄰有效取樣差分，kernel 時間已包含 idle，不可把 idle 再加進總時間；使用率為 (差分 kernel + 差分 user - 差分 idle) / (差分 kernel + 差分 user)。第一次只有基準值時顯示「取樣中」，時間差無效時重建基準；不得阻塞等待採樣間隔。
- GetSystemTimes 在超過 64 個邏輯處理器時有 processor group 範圍限制；第一版若偵測此環境，CPU 明示不支援，避免宣稱顯示整機 CPU；RAM 與磁碟仍可使用。
- RAM 使用 GlobalMemoryStatusEx 的實體總量與可用量，已用量為 total - available，百分比使用相同口徑。容量 API 以 bytes 回傳，畫面換算 GiB 並標明單位。
- 磁碟回傳 total／used／free bytes 與使用率，以目標專案的實際路徑查詢，不接受 URL 傳入任意路徑。不讀取／掃描專案檔案內容。
- 每個 Dashboard／server 實例持有一個採集器，鎖定共享採樣狀態；成功或失敗的採樣結果皆快取 3 秒。採樣採請求驅動，無使用者讀取時不新增背景常駐工作；多個頁面／瀏覽器共用同一取樣結果，不各自重置 CPU 基準。
- GET /api/system-monitor 回傳 sampleId、sampledAt（具時區的時間）、intervalSeconds，以及 cpu、memory、disk 三個項目。sampleId 隨實際採樣遞增，重複快取不會增加。各項帶 status、updatedAt 及該項數據；有效 status 為 ok、warming-up、error、unsupported，只有 ok 可當成有效數值。
- 單項採集失敗回傳該項狀態與可理解訊息，數據使用 null；其他項目持續更新。預期中的不支援／採集錯誤不讓整個 server 中止。未處理的請求錯誤沿用既有 HTTP 錯誤處理。
- 此路由為唯讀，沿用目前可唯讀瀏覽的模式與 Host 限制；不新增寫入 API、核准操作或 .board/ 持久化資料。

## 畫面與更新行為

- 左側顯示「系統監控」入口。完整頁上方為 CPU、RAM、磁碟三張卡，下方為 CPU 與 RAM 兩張時間走勢圖，沿用深色終端機風格與共用 CSS 變數。
- CPU 卡顯示整體百分比；RAM 卡顯示使用率、已用／總容量與可用量；磁碟卡顯示磁碟區、使用率、已用／總容量及剩餘量。標示「server 主機」與最後資料時間。
- 模組只有一個共用輪詢排程：監控頁或控制台摘要可見時立即讀取並每 3 秒更新；切到其他頁或瀏覽器頁籤隱藏時停止，重新顯示立即讀取。尚未完成的請求不重疊，過期回應不得更新已卸載畫面。
- 同一模組前端快取提供完整頁與控制台摘要；切換頁面不重複建立計時器，與原有 /api/state 的 2 秒輪詢分開管理。
- 走勢依 sampledAt 的實際時間裁切最近 60 秒，只加入新的 sampleId，保留有界資料；首屏無歷史時由當下累積，不補造過去資料。中斷處留缺口，恢復後不把中斷前後連成連續取樣。
- 首次載入顯示「載入中」，CPU 基準建立期間顯示「取樣中」；null、不支援及失敗與有效 0% 明確區別。HTTP／網路失敗顯示「資料暫時無法取得」，保留的舊值標為過期；距最後成功資料超過 10 秒也必須顯示過期。恢復後自動顯示新資料。
- 控制台原本的資源監控佔位文字改為三項精簡摘要，並提供可鍵盤操作的完整頁連結。
- 窄視窗改為直向排列，文字資訊與圖表相互補足，狀態不只靠顏色表達。

## 驗證與交付

- 採集單元測試使用替身 API／時鐘，驗證計算與錯誤語意；真實主機驗證只檢查值域、容量關係及變化趨勢，不要求與工作管理員在不同取樣時間完全一致。
- API 測試沿用 server.makeServer 建立隔離測試實例，驗證路由、JSON、模組資產、Host 限制與原頁面回歸；測試可用固定替身資料，不取得真人 server 的 token。
- 前端互動測試使用 Node 與可控 fetch／時鐘，驗證輪詢、圖表有界資料、生命週期及錯誤恢復。模組內瀏覽器測試沿用專案既有 Edge／Chrome 無頭方式，驗證實際頁面掛載、導覽、控制台摘要及窄畫面。
- 每項任務執行相關測試；完成 T5 後在成果分支執行 python -m pytest -q，報告通過、失敗與跳過原因。人工使用者端驗收另列出操作步驟及實際結果，未執行必須明示。
- 規劃階段只新增此 plan，並使用 mamd-stable 的 board.py submit 送出；核准由使用者在既有介面或終端機操作。

## 技術依據

- CPU 差分與 Windows processor group 限制依據 [Microsoft GetSystemTimes 文件](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getsystemtimes)。
- RAM 實體容量與可用量依據 [Microsoft GlobalMemoryStatusEx 文件](https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-globalmemorystatusex)。
- 專案所在磁碟容量依據 [Python 3.13 shutil.disk_usage 文件](https://docs.python.org/3.13/library/shutil.html#shutil.disk_usage)。
