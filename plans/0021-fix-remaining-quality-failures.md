---
id: P0021
created: 2026-10-31
area: [後端/測試把關, 後端/CLI, 後端/統計]
---

# 把整合 P0014 與 P0020 後剩餘的 11 個品質 FAIL 拆成小任務修完

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 我要怎麼做? | 延續前一則建議，另開 plan 把剩餘 11 個函式拆成可單獨審查的小任務，解除 P0014 T12 與 P0020 的品質驗收阻塞。範圍是 9 個複雜度超標函式與 2 個覆蓋率不足函式；不要求 T12 一次處理，也不把未完成的品質檢查標成通過 | D1 以整合 P0014 與 P0020 的 813b9e1 為起點，對 conductor.py 的 compareWithBaseline（C=13）與 mergeTestOutcome（C=21）做不改行為的重構，把 pytest 比對、品質比對、單次任務結果與拆分結果等分支拆成具名小函式；先用原有測試與缺少的行為案例鎖住輸入輸出，再重構。新增 helper 一樣納入品質門檻 | 把 unknown 改成 passed：掩蓋基準不足；把這 11 個全部塞進 T12：超出原函式清單且一次審查太大；只壓低原函式複雜度、把問題搬進另一個超標 helper：沒有改善品質 | T1 重構 compareWithBaseline 與 mergeTestOutcome，保留所有結果判定契約 | | 執行 tests/test_conductor.py 與 tests/test_conductor_after_merge.py 的相關測試通過；涵蓋 passed、preexisting、newFailure、unknown、品質未檢查、split 與 stillFailing，回傳欄位與事件意義不變；品質檢查中這兩個函式與新增 helper 都是 PASS |
| R1 | | D2 只重構 conductor.py 的 Conductor.runTestBaselines（C=11），抽出尋找可沿用來源與決定等待或啟動的步驟；保持來源新舊順序、測試指令比對、只允許 plans/ 差異、同等基準等待與名額不足時的行為 | 改變快取條件或來源優先序：會改動已核准的基準沿用行為；把 git 失敗當作可沿用：不能確認程式碼相同 | T2 重構 runTestBaselines，保留基準沿用與等待規則 | T1 | 執行 tests/test_conductor.py 中基準、沿用、名額與指令變更的相關測試通過；來源有效時沿用、來源不明時重跑、相同基準執行中時等待，與改前一致；品質檢查中 runTestBaselines 與新增 helper 都是 PASS |
| R1 | | D3 只重構 conductor.py 的 Conductor.startMergeTests（C=17）與 Conductor.startMergeTest（C=14），拆出 plan 是否可測、待測任務排序與測試目標選擇；保持每 plan 一個 job、maxParallelTests、等基準、mergeTest 人工介入阻塞、split 範圍、covers 與 reused 的既有契約 | 調整排程或每次合併都測一次：這份只處理品質；移除名額或人工介入檢查：會改變執行流程 | T3 重構合併後測試排程與目標選擇 | T2 | 執行 tests/test_conductor_after_merge.py 與 tests/test_conductor.py 的相關測試通過；名額滿時不啟動、等待基準、不越過 mergeTest 阻塞、平常批次與 split 逐一測的 commit 和 covers 都與改前相同；品質檢查中兩個函式與新增 helper 都是 PASS |
| R1 | | D4 對 conductor.py 的 Conductor.finishMergeTest（覆蓋率 58.3%）與 Conductor.removeMergeTestWorktree（50%）補真的驗證錯誤處理的測試；用替身模擬 job.result、job.error、BoardError、一般例外與移除 worktree 失敗，確認紀錄、通知、傳播與清理語意，避免真的破壞 worktree | 以 pragma 排除錯誤路徑：失去測試保護；只呼叫函式但不檢查結果：不能證明既有行為 | T4 補 finishMergeTest 與 removeMergeTestWorktree 的錯誤路徑測試 | T3 | 執行 tests/test_conductor_after_merge.py 的相關測試通過；成功時寫入正確測試紀錄，BoardError 依既有行為傳播，一般失敗只通知且不捏造測試結果，移除失敗通知不重複；品質檢查中這兩個函式的覆蓋率至少 80%，狀態都是 PASS |
| R1 | | D5 只重構 board_core.py 的 onUserDecision（C=11），拆出 retry 的狀態轉換；保持 done、abandon、retry、mergeTest、mergeConflict、各種 needs kind、輪數與 userNote 的事件重播結果，補 tests/test_board_events.py 與 tests/test_board_state_merge_tests.py 的缺少分支 | 修改 retry 目標或用特殊事件直接標完成：會改變看板行為；修改既有 .board/events.jsonl：破壞原始紀錄 | T5 重構 onUserDecision，保留人工決定的重播結果 | | 執行 tests/test_board_events.py 與 tests/test_board_state_merge_tests.py 通過；既有與新增事件案例的狀態、round、maxRounds、userDone、userNote 與 mergeFix 等欄位符合原契約；品質檢查中 onUserDecision 與新增 helper 都是 PASS |
| R1 | | D6 只重構 board.py 的 cmdApprove（C=11），把狀態、plan 檢查與確認步驟拆成小函式；保持計畫檔已提交檢查、警告確認、回傳碼、中文訊息與 planApproved 事件的欄位。tests/test_board_cli.py 使用暫時專案呼叫函式驗證，不核准真實計畫 | 取消確認或讓 agent 直接核准真實計畫：違反使用者操作邊界；修改 CLI 選項或事件格式：不屬於這次品質修正 | T6 重構 cmdApprove，保留核准檢查與輸出 | | 執行 tests/test_board_cli.py 的核准相關測試通過；非待核准、有紅色問題、plan 未提交或使用者取消時都不寫核准事件，成功時資料與原流程一致；品質檢查中 cmdApprove 與新增 helper 都是 PASS |
| R1 | | D7 只重構 board_prompts.py 的 buildReviewPrompt（C=12），抽出可選的審查區段組合；保持文字、段落順序、diff 截斷、舊流程測試結果、合併後測試說明、解衝突內容與 previousErrors 的契約，以 tests/test_board_prompts.py 鎖住代表性的完整輸出 | 順手改提示詞或驗收規則：會影響 agent 行為；只驗證輸出非空：不足以保護提示詞契約 | T7 重構 buildReviewPrompt，保留提示詞內容與順序 | | 執行 tests/test_board_prompts.py 通過；舊流程、afterMerge、TDD、解衝突、過長 diff 與格式重試的輸出和改前一致；品質檢查中 buildReviewPrompt 與新增 helper 都是 PASS |
| R1 | | D8 只重構 board_stats.py 的 testStats（C=15），分開整理實際執行、沿用與退回的計數；tests/test_board_stats.py 鎖住新舊事件格式、一次涵蓋多任務、reused、newFailure 與 stillFailing，不新增統計欄位 | 把 reused 當成真的測試次數或每個 covers 各算一次：改變既有統計；調整其他成效指標：不屬於這次品質修正 | T8 重構 testStats，保留測試與退回次數的統計 | | 執行 tests/test_board_stats.py 通過；runs、failed、returned、reused 與改前相同，單次完整測試涵蓋多任務只算一次執行；品質檢查中 testStats 與新增 helper 都是 PASS |

## 基準與範圍

- 來源是 2026-10-31 00:22:06 的 P0020 mergeTestRun：commit 813b9e10948ef2944a0a8942d71188c71dce375d，pytest failures 為空、qualityChecked 為 true、qualityFailures 有上述 11 個名稱、verdict 為 unknown。相較前一筆，品質 FAIL 由 44 個減為 11 個，沒有新增的品質失敗名稱。
- 原基準因不穩定測試失敗而沒有量到品質，因此 P0020 的 unknown 不能用重試或直接接受結果消除。這份修正使用已經量到 11 個 FAIL 的整合版當起點，保留 P0014 與 P0020 的成果。
- 提交這份 plan 前，主工作目錄已從 2eeb325 快轉到 813b9e1；核准仍從當時 HEAD 開分支，核准前須確認 813b9e1 是 HEAD 的祖先，不可退回整合前版本。這份 plan 不取代 P0014 或 P0020，不修改它們已核准的內容。
- 複雜度門檻仍為每個函式 C 不超過 10，覆蓋率至少 80%，CRAP 依既有 tdd-crap 門檻。新抽出的 helper 一樣必須通過，不能只把複雜度移到另一個超標函式。
- 只修列出的 11 個函式、必要的小 helper 與對應測試；不修改品質判定、事件格式、提示詞內容、統計定義、排程規則或測試門檻。不刪測試、不弱化斷言，不用 skip、xfail、deselect、重試插件或任意 pragma 取得通過。

## 任務檔案與測試分工

- T1～T4 都會修改 conductor.py 或共用的 conductor 測試，因此依序執行。T5 只用 board_core.py 與兩個 board 狀態測試檔；T6 只用 board.py 與 tests/test_board_cli.py；T7、T8 各改自己的模組與測試檔。若實作發現還要改其他任務的檔案，先回報，不同時擴張檔案範圍。
- 重構前先確認既有行為測試通過，必要時補缺少分支的測試，再重構並確認結果一致。既有功能的純重構不捏造 Red；補到原本缺少的行為保護時如實記錄驗證過程。
- writer 只跑相關測試與相應的局部品質量測；完整 testCommand 由 Alfred 依既有流程在合併後背景執行，遵守 maxParallelTests。局部量測其他函式沒有覆蓋時的 FAIL 不可冒充這次全套結果。
- 八個任務的成果全部整合後，必須有一筆對應 plan/P0021 最新 commit 的完整檢查：passed=true、qualityChecked=true、failures=[]、qualityFailures=[]，品質報告 status=passed，所有函式沒有 FAIL。不能只用 preexisting、局部測試或看板完成狀態代替最終品質驗收。
- 如果最後還有 FAIL，先列出確切函式與原因，依任務範圍交回修正或另行規劃；不得未量測就宣稱 11 個問題全部解決。

## 完成後接續 P0014 與 P0020

- P0014 T12、P0020 T1 與 T2 在這份 plan 驗收前先保留等你處理，不持續空白重試；不修改 .board/、不由 agent 執行 approve 或 decide。
- 完整品質驗收通過後，由使用者確認各 worktree 乾淨，將 plan/P0021 成果整合回 plan/P0014、task/P0014-T12 與 plan/P0020。三個分支若仍停在 813b9e1，可在各自的 worktree 用 merge --ff-only plan/P0021；若已分岔，保留各自成果並先處理解衝突，不強制重設。
- P0020 的 T1、T2 都是 mergeTest 等你處理，兩個都由使用者選再跑一輪，Alfred 才能解除該 plan 的阻塞並重測整合版。這種重試只重測，不重新派 writer、不消耗寫程式輪數。
- P0014 T12 是 blocked，重試會再派 writer。若本次完整檢查對應的 commit 已同時包含 P0014 全部成果、P0020 測試修正與本次 11 個修正，且 P0014 與 T12 已整合到該受測 commit，使用者可依這份完整通過的證據接受 T12 完成，避免再次派發沒有剩餘程式改動的驗證任務。接受時註明受測 commit 與完整檢查來源；這是使用者操作，不由 agent 代執行。
- 若 P0014 又有後續程式改動，先補完整複驗，不能沿用不同程式版本的通過結果。最後確認 P0014 與 P0020 的驗收都完成，才由使用者決定是否合併 main。
