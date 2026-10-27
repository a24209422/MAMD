# 系統監控模組

在儀表板顯示執行 server 那台主機的 CPU、RAM，以及專案所在磁碟的用量。完整頁有三張卡（CPU、RAM、磁碟）和 CPU、RAM 最近一分鐘的走勢，每 3 秒更新；控制台的「系統資源」區塊是同一份資料的三項摘要。

## 啟動關係

- 不另開 server、不在背景常駐：`server.py` 每啟動一個 server，就建一個 `SystemMonitor`，掛上：
  - `GET /api/system-monitor`：唯讀的監控資料
  - `/modules/system-monitor/monitor.html`、`monitor.css`、`monitor.js`：只開放這三個畫面檔，模組的 Python、測試和這份說明都下載不到
- 這些路徑沿用 server 原有的檢查：只接受 `127.0.0.1`／`localhost` 的網址，錯誤由 server 統一回應。看監控不需要 token
- 儀表板左側的「系統監控」是完整頁（網址 `#system-monitor`，舊的 `#topology` 會轉過來）；控制台的摘要整塊可以點，會跳到完整頁
- 畫面是哪一份程式，就看是哪一份 `server.py` 啟動的：server 讀的是自己資料夾裡的 `web/` 和 `modules/`。要看某個分支、worktree 或另一份 clone 的修改，`<agent-board>` 就要換成那一份的資料夾：

  ```
  python <agent-board>\server.py <專案資料夾> --port 8766
  ```

  8765 已經有別份 server 時換一個 `--port`，兩個可以同時開
- server 只能由你在自己的終端機啟動（網址裡的 token 等於核准權限），agent 不會幫你啟動

## 監控對象

- 執行 server 的主機，畫面標示「server 主機」
- 磁碟是啟動 server 時傳入的專案資料夾所在的磁碟區，卡片標題顯示磁碟區（例如「磁碟 D:」）。不能從網址指定其他路徑，也不會讀取或掃描專案裡的檔案
- 第一版只看整台主機的 CPU、RAM 和這一個磁碟區；不含個別程式或 agent 的用量、GPU、網路

## 數據口徑

| 項目 | 來源 | 計算 |
|---|---|---|
| CPU | Windows `GetSystemTimes` | 相鄰兩次取樣的差分：(kernel + user − idle) ÷ (kernel + user)；kernel 已經包含 idle。所有邏輯處理器合計 |
| RAM | Windows `GlobalMemoryStatusEx` | 實體記憶體的總量與可用量；已用 = 總量 − 可用，使用率 = 已用 ÷ 總量 |
| 磁碟 | Python `shutil.disk_usage(專案資料夾)` | 總量、已用、剩餘；使用率 = 已用 ÷ 總量 |

- 只用 Python 標準庫，不啟動外部程式、不用安裝套件
- API 的容量都是 bytes；畫面換算成 GiB（1 GiB = 1024³ bytes），小數一位。工作管理員和檔案總管標的「GB」也是這個換算，數字可以直接比
- 百分比四捨五入到小數一位，限制在 0～100
- 和工作管理員比對時：
  - CPU：工作管理員的使用率會依處理器實際頻率調整，這裡是處理器忙碌時間的比例；CPU 加速時兩邊可能有落差，看升降趨勢是否一致
  - RAM：可用量和工作管理員的「可用」對應。已用量是總量 − 可用，包含「已修改」的分頁，可能比工作管理員的「使用中」略多；總量是系統可用的實體記憶體，不含硬體保留，可能比工作管理員右上角的安裝容量略少
  - 磁碟：總量、剩餘和檔案總管「本機」裡該磁碟的總容量、可用空間相同

## 取樣與更新

- 有人讀取才取樣，沒人看時不會在背景取樣
- 同一個 server 的取樣結果（包含失敗）保留 3 秒；多個頁面、多個瀏覽器共用同一份結果和 CPU 基準，不會互相重置
- CPU 要兩次取樣才算得出來：第一次只建立基準，顯示「取樣中」，約 3 秒後才有數字。距離上一次超過 30 秒、或系統計數倒退時，重建基準、再顯示一次「取樣中」
- CPU 是兩次取樣之間（約 3 秒）的平均，RAM 和磁碟是取樣當下的值；和工作管理員的取樣時間不同，不會每一刻都完全相同
- 網頁只有一個 3 秒的輪詢，和儀表板原本 2 秒一次的狀態輪詢分開：
  - 完整頁或控制台摘要看得到時，立刻讀一次，之後每 3 秒一次
  - 切到其他頁、或瀏覽器頁籤隱藏時停止；回來時立刻再讀
  - 上一次還沒回來就不再發；超過 10 秒沒回應就放棄這一次
- 走勢：依 server 的取樣時間保留最近 60 秒（約 20 筆），從開頁起才開始累積，不補過去的資料。讀取失敗、停止輪詢時錯過的讀取、或相鄰兩筆隔超過 10 秒的地方會斷開，不把前後連成一條線

## 狀態與失敗

- 每一項有自己的狀態，只有 `ok` 才有數字：
  - 取樣中：CPU 還在建立基準
  - 不支援：這台主機無法提供
  - 讀取失敗：這一次讀不到，下一次會再試
- 沒有數字時顯示「—」和狀態文字，不會顯示成 0%；真的是 0% 時才顯示 0.0%
- 一項讀取失敗只影響那一項，其他項照常更新；也不影響儀表板其他頁面
- 連不到 server 或 server 回錯誤時顯示「資料暫時無法取得」，保留最後一次的數值並標「過期」；距最後一次成功超過 10 秒也標「過期」。恢復後自動更新，不用重新整理
- 第一版只支援 Windows：其他系統的 CPU、RAM 顯示「不支援」，磁碟照常
- 超過 64 個邏輯處理器（多個處理器群組）時，`GetSystemTimes` 只涵蓋其中一個群組，CPU 顯示「不支援」，RAM 和磁碟照常

## API

`GET /api/system-monitor`：

```json
{
  "sampleId": 12,
  "sampledAt": "2026-10-27T10:00:03.123+08:00",
  "intervalSeconds": 3,
  "cpu": { "status": "ok", "updatedAt": "…", "message": null, "percent": 23.4 },
  "memory": { "status": "ok", "updatedAt": "…", "message": null,
              "totalBytes": 17179869184, "usedBytes": 12884901888, "availableBytes": 4294967296, "percent": 75.0 },
  "disk": { "status": "ok", "updatedAt": "…", "message": null, "volume": "D:",
            "totalBytes": 536870912000, "usedBytes": 134217728000, "freeBytes": 402653184000, "percent": 25.0 }
}
```

- `sampleId` 每次真的取樣加一；拿到快取時不變
- `status` 是 `ok`、`warming-up`、`unsupported`、`error` 其中之一；不是 `ok` 時數值欄位都是 `null`，`message` 說明原因

## 檔案

| 檔案 | 內容 |
|---|---|
| `metrics.py` | 採集、計算與 3 秒快取 |
| `api.py` | API 路由與畫面檔白名單，由 `server.py` 掛上 |
| `web/monitor.js` | 完整頁、控制台摘要和共用的輪詢；主系統用 `SystemMonitor.shared()` 掛載 |
| `web/monitor.css` | 畫面樣式，沿用儀表板的色彩與字型變數 |
| `web/monitor.html` | 單獨開的頁面（`/modules/system-monitor/monitor.html`） |

## 測試

```
python -m pytest -q
```

`pytest.ini` 已經把 `modules/system_monitor/tests` 加進來，和 `tests/` 一起跑：

| 測試 | 內容 | 跳過條件 |
|---|---|---|
| `tests/test_metrics.py` | 用替身系統 API 和時鐘驗證計算、錯誤和快取；Windows 上另讀一次真實樣本 | 真實樣本只在 Windows 跑 |
| `tests/test_monitor_ui.py` | 用 Node 跑 `monitor-ui.test.cjs`（輪詢、走勢、生命週期、斷線恢復）和語法檢查 | 找不到 Node |
| `tests/test_browser.py` | 用 Edge／Chrome 無頭模式開真的 server 和真實採集器，看資料畫到「系統監控」頁、持續更新，讀不到時不顯示假的 0、恢復後接著更新 | 找不到 Edge 或 Chrome；真實主機資料那一項只在 Windows 跑 |

API、白名單和 Host 檢查在專案的 `tests/test_server.py`；導覽、控制台摘要和窄畫面在 `tests/test_web.py`。

## 人工驗收

自動測試無法和工作管理員比對，也無法真的把 server 停掉，這兩項要自己看：

1. 在自己的終端機啟動要驗收的那一份 `server.py`（見「啟動關係」），用印出的網址開瀏覽器，點左側「系統監控」
2. 開工作管理員的「效能」頁並排比對：
   - CPU：另開終端機執行 `python -c "while True: pass"`，CPU 卡和走勢上升（一個核心滿載，約增加 100 ÷ 邏輯處理器數 %），按 Ctrl+C 後回落
   - RAM：另開終端機執行 `python -c "data = b'x' * 2 * 1024**3; input('按 Enter 結束')"`，RAM 已用約增加 2 GiB、可用跟著減少，按 Enter 後回復
   - 磁碟：和檔案總管裡專案所在磁碟的總容量、可用空間相同
3. 在 server 的終端機按 Ctrl+C：3 秒內顯示「資料暫時無法取得（連不到 server）」，數值保留並標「過期」，不會變成 0%
4. 用同一個 `--port` 再啟動 server：不用重新整理，3 秒內恢復更新、過期標記消失，走勢在中斷處斷開。server 重開後 token 會換，只看監控不受影響；要核准等操作時改用新印出的網址
