---
id: P0013
created: 2026-10-29
area: [後端/測試把關]
---

# 品質門檻超標的函式也當成失敗名稱跟基準比，分得出本來就超標和這次新增的超標

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 兩個都做，分兩份 plan | tdd-crap 專案的 testCommand 是品質檢查，現在有 35 個函式本來就超標，所以就算 pytest 全部通過，基準和任務測試也一律是失敗；Alfred 只認得 pytest 的 FAILED／ERROR 名稱，品質門檻的失敗沒有名稱，造成兩種誤判：基準 pytest 全過時，之後每個任務的失敗都判成「無法比對」照常送審，新增的失敗不會被退回；基準剛好有 pytest 失敗時，任務 pytest 全過、只剩本來就有的品質超標，卻被判成新增的失敗退回；要讓品質門檻的失敗也有名稱、能跟基準比；這份是「比對規則」那一份，修 35 個函式另寫 P0014 | D1 conductor.py 新增 qualityResults(output)：讀 tdd-crap 品質檢查每個函式一行的輸出（`PASS 檔案:行 函式 ...`、`FAIL 檔案:行 函式 ...`），回傳 (checked, names)；checked 表示輸出裡有任何一行 PASS 或 FAIL（pytest 失敗時 quality.py 不計算覆蓋率，就不會有這些行），names 是 FAIL 那幾行的名稱，格式為「品質門檻 檔案::函式」，不含行號（程式改動後行號會變，含行號就比不了）；基準與任務測試的 runTestCommand 結果多帶這兩個值，testBaseline 和 testRun 事件多記 qualityChecked、qualityFailures；舊事件沒有這兩個欄位時視為沒檢查 | 把品質失敗名稱直接混進 failures：基準 pytest 失敗時品質根本沒算，混在一起就分不出「沒有品質失敗」和「品質沒有檢查」；名稱含行號：任務只要在函式上面加一行，就會被當成新的超標；改讀 .quality/report.json：報告在測試的 worktree 裡，基準 worktree 跑完就刪掉，而且非 tdd-crap 專案沒有這個檔 | T1 conductor 讀出品質門檻的 FAIL 名稱與是否有檢查，寫進 testBaseline、testRun；補 tests/test_conductor.py | | 執行 python -m pytest -q tests/test_conductor.py 通過；新增的測試確認：品質檢查輸出有 PASS、FAIL 行時，qualityChecked 為 true、qualityFailures 是 FAIL 那幾行的「品質門檻 檔案::函式」且不含行號；只有 pytest 失敗、沒有 PASS／FAIL 行時 qualityChecked 為 false；一般 pytest 輸出（沒有品質檢查）時 qualityChecked 為 false、qualityFailures 為空 |
| R1 | | D2 compareWithBaseline 分兩部分判斷再合併：pytest 部分照原本的規則；品質部分只在任務測試和基準都 qualityChecked 時比較，任務的 qualityFailures 不在基準裡的就是新增的超標；任一方沒有檢查品質時，品質部分不判定為新增，也不能讓整體變成「通過」；合併後有任何新增的失敗（pytest 或品質）就是 newFailure，newFailures 列出全部新增的名稱；沒有新增、但有基準就有的失敗是 preexisting；pytest 部分無法比對、品質部分也沒有新增時維持 unknown；沒有任何品質檢查的專案（一般 pytest）結果和現在完全相同 | 品質部分無法比較時一律退回：基準 pytest 失敗、品質沒算的情況很常見（例如不穩定的測試），會讓每個任務都被退回；品質部分無法比較時直接當成通過：任務真的弄出新的超標也會送審 | T2 compareWithBaseline 加上品質部分的比較與合併規則；conductor 告訴審查者基準就有的失敗時也列出品質門檻的名稱；補 tests/test_conductor.py 與 tests/test_board_prompts.py | T1 | 執行 python -m pytest -q 通過；新增的測試確認：基準 pytest 全過、品質有 3 個 FAIL，任務 pytest 全過、品質同樣 3 個 FAIL 時是 preexisting 並送審；任務多 1 個 FAIL 時是 newFailure，newFailures 只有新增的那個；基準 pytest 有失敗、品質沒檢查，任務 pytest 全過、品質有 FAIL 時不是 newFailure；任務 pytest 有新的失敗時照樣 newFailure；一般 pytest 專案的既有判定測試全部照樣通過；審查 prompt 的「基準就有」段落列出品質門檻的名稱 |
| R1 | | D3 README「測試判定」補上品質門檻：tdd-crap 的品質檢查每個超標的函式也是一個失敗名稱（「品質門檻 檔案::函式」，不含行號），跟基準比的方式和 pytest 的失敗名稱一樣；基準的 pytest 失敗時品質沒有計算，這時品質超標不算新增；表格補上這兩種情況 | 只改程式不改 README：README 目前只說失敗名稱來自 FAILED／ERROR 行，使用者看不懂為什麼品質超標也會被退回或放行 | T3 更新 README「測試判定」，說明品質門檻的失敗名稱與比對方式 | T2 | README「測試判定」看得到品質門檻的名稱格式、不含行號的原因，以及基準 pytest 失敗時品質超標不算新增 |

## 背景

- 2026-10-29 直接改 P0008 後跑完整品質檢查：pytest 512 passed，但有 36 個函式超標（其中 1 個是當次新加、已補測試），testCommand 結果一律失敗；同一天 P0007 T2、P0009 T1 的任務測試都受這個影響。
- quality.py 在 pytest 失敗時只回報「pytest failed」，不計算覆蓋率，也不印 PASS／FAIL 行（P0009 基準的輸出就是這樣）。
- failureNames 目前只認 `FAILED ` 與 `ERROR ` 開頭的行；quality.py 最後一行 `FAILED: ...report.json` 因為冒號緊接在後，不會被當成名稱。

## 範圍

- 不包含：修掉 35 個超標的函式（P0014）；Alfred 判定新增失敗前重跑失敗的測試。
- 這份 plan 會改 conductor.py，跟待核准的 P0010、P0012 重疊，一份合併回 main 後再核准另一份；P0010 沿用基準或任務測試結果時，要一併照抄 qualityChecked、qualityFailures，後核准的那份要處理。
