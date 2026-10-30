---
id: P0020
created: 2026-10-30
area: [後端/測試把關]
---

# 修正審查重啟測試的啟動競態，並準備 P0014 T12 的完整品質複驗

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 照你的建議做 | 採用前一則建議：另開 plan 修正 test_noReviewUntilBaselineForChangedCommandIsRecorded 的不穩定，修好後把修正帶進 P0014，再由使用者讓 T12 再跑一輪，完整品質檢查交給 Alfred 背景執行；全部通過才完成 P0014 | D1 修正 tests/test_conductor.py 的測試同步：保留等待審查中的條件，再等第一次假 reviewer 呼叫紀錄確實建立，才送出測試用的 paused 事件；沿用 tests/test_conductor_control.py 等待假 agent 呼叫紀錄的模式，使用有上限的輪詢，避免讀取尚未寫完的 JSON。保留新指令的基準未完成前不送審、失敗通知只印一次、恢復後基準先於第二次審查等斷言。補一個可控制 reviewer 延遲啟動的回歸情境，確認暫停發生在第一次呼叫已記錄之後，恢復後使用第二次 pass 呼叫並完成；優先在此測試內提供控制點，必要時才在 tests/fakes/ 加上僅由這個情境啟用的同步機制，並確保失敗時清理重啟的 conductor 與子程序 | 加固定 sleep 或單純延長 runUntil 超時：仍依賴機器負載與排程；整個測試重試到通過：掩蓋競態；跳過測試或移除基準順序斷言：失去原本的測試保護；修改正式 conductor 的流程：目前證據指向測試把已派工誤當成子程序已初始化，先修測試同步 | T1 修正 reviewer 啟動與暫停的測試競態，加入延遲啟動回歸驗證 | | writer 先以可控制的延遲重現原測試提前暫停、重啟仍取得第一次 hang 呼叫的問題，再確認修正後通過；單獨執行目標測試與新增回歸情境連續 10 次全部通過，沒有使用 skip、xfail 或測試重試插件；執行 tests/test_conductor.py 與 tests/test_conductor_control.py 的相關測試通過，既有基準與審查順序斷言都保留；完整測試由 Alfred 在合併後執行 |
| R1 | | D2 新增 docs/p0014-t12-recovery.md，記錄本次實際測試修正的檔案與整合方式，以及使用者接續 P0014 T12 的步驟。說明 P0020 分支完成不代表 P0014 的 plan 與 T12 worktree 自動取得修正；先確認分支乾淨並將測試修正整合進 plan/P0014，解決與 P0014 既有補測試的衝突，再確認 task/P0014-T12 也包含修正，能快轉時才快轉。完整檢查仍由 Alfred 負責，T12 writer 只做相關檢查、品質問題修正與如實交付，不能把交付前未執行的完整驗證當成阻塞理由。文件標明再跑一輪由使用者操作，並要求最後結果對應包含 P0014 全部改動與本次修正的 commit，pytest 通過、qualityChecked 為 true、qualityFailures 為空，品質報告 status 為 passed 且沒有 FAIL；如果只有 preexisting 判定或 qualityChecked 為 false，仍未達成 P0014 的驗收目標 | 直接修改已核准的 P0014 plan：改動已核准範圍且失去原需求依據；修正只合併 main 就立刻重試 T12：既有 plan 與 task worktree 可能仍含舊測試；讓 writer 自己背景跑整套：違反既有測試分工；未完成品質檢查就標完成：不能證明每個函式通過 | T2 撰寫 P0014 T12 復原與完整品質驗收操作說明 | T1 | 使用者依 docs/p0014-t12-recovery.md 能確認 P0014 與 T12 都包含修正，在看板對 T12 選再跑一輪；Alfred 對包含全部改動的版本執行完整 testCommand，沒有排除該測試，pytest 與每個函式的品質檢查都通過後 P0014 才可完成；若仍有品質 FAIL，交給 T12 修正後由流程再測，不以消除警告代替驗收 |

## 背景與原因判讀

- 2026-10-30 本機 P0014 已完成 T1～T11，T12 因這個測試不穩定停在等你處理。完整測試紀錄為 1 failed、619 passed、33 skipped，qualityChecked 為 false；判為 preexisting 只代表沒有新增的 pytest 失敗，不代表已經執行每個函式的品質檢查。
- 既有測試在看到 TaskStatus.reviewing 後立即暫停。這個狀態由父程序派工時寫入，但 tests/fakes/fake_agent.py 的 nextAction 要等子程序實際啟動後才遞增 count-review-P0001-T1.txt，startCall 隨後才寫出 call-review-P0001-T1-1.json。若在那之前中斷，重啟仍可能取到情境中的第一次 hang，和最後停在審查中的失敗現象相符。此為程式碼判讀，實作時須用受控延遲重現確認。
- tests/test_conductor_control.py 的停止與重啟測試已有等待 call-write-P0001-T1-1.json 的做法，可沿用其同步方式。
- P0014 明確排除修正這個測試，所以這份 plan 獨立處理，不取代或修改 P0014。

## 執行與接續邊界

- 本 plan 的自動任務是測試修正與復原文件；不替使用者核准計畫、執行 decide、修改本機設定或強制寫入完成事件。
- writer 只跑本次相關測試；完整 testCommand 由 Alfred 的既有測試流程執行。不得用 --deselect、skip 或調低品質門檻取得通過結果。
- P0014 T12 的重試是本 plan 修正整合完成後的人工接續步驟，不是對本 plan 尚未存在的跨 plan 任務前置。文件須清楚區分 P0020 的完成與 P0014 的最終驗收。
- 不修改正式排程、基準比對規則、品質工具的失敗處理或其他偶發失敗測試；受控重現若指出正式流程有另一個錯誤，先據實回報並重新界定修正範圍。
