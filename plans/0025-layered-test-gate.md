---
id: P0025
created: 2026-11-02
area: [後端/測試把關, 工具/Skill]
---

# 測試改成分層把關：任務階段檢查改動的函式，完整測試每份 plan 只跑一次，基準盡量沿用

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 有必要每次寫新的東西就跑一次完整的測試嗎？我有一個想法，當寫一個新的plan後，除了執行tdd的測試外，只測試最基本的測試，你覺得這個想法可以嗎？ | 完整品質檢查已超過 30 分鐘，一份 plan 至少跑基準與合併後測試兩次，P0023 這種依序完成的 plan 每合併一個任務就跑一次。過去合併後測試抓到的新增失敗多是本任務改動函式的品質門檻，跨檔回歸很少。依對話確認改採分層：任務階段跑 TDD 相關測試加上「本次改動函式」的品質檢查，完整測試每份 plan 在任務都合併後只跑一次，基準能沿用就沿用；不另外維護一份「基本測試」清單 | D1 onlyPlansChanged 改成判斷「只差不影響測試的檔案」，清單只有兩項：plans/ 底下的檔案，以及 export-state 產生的 .agent-board/progress.json。其他路徑一律視為可能影響測試，包括 docs/（tests/test_board_cli.py 會讀 docs/AGENTS-snippet.md）。使用這個判斷的兩個入口 reusableBaselineSource（沿用基準）與 waitsForRunningBaseline（等待同碼基準）行為一致 | 忽略所有非 .py 檔：docs、skill 與設定檔會被測試讀取；改用整個 tree 的雜湊比對：交接檔一變動雜湊就不同，仍無法沿用 | T1 基準沿用時把交接檔視為不影響測試 | | 執行 python -m pytest tests/test_conductor.py -q 通過；測試確認被測 commit 與 baseCommit 只差 plans/ 和 .agent-board/progress.json 時沿用結果、不建基準 worktree，多差 docs/ 或程式檔時照常重跑；另確認 tests/ 沒有讀取 repo 本身的 .agent-board/progress.json |
| R1 | | D2 tdd-crap 的 quality.py 新增 --changed-since <commit>：只判定原始碼行範圍與 git diff <commit>（含工作目錄未提交改動）有交集的函式，其他函式不列為 FAIL；report.json 記錄這次的判定範圍與被判定的函式，結束代碼只看被判定的函式與 pytest 結果。沒帶這個參數時輸出與現在完全相同。SKILL.md 補上參數說明 | 只在 conductor 過濾輸出的 FAIL 行：quality.py 結束代碼仍會因未判定的函式失敗，無法分辨真正的結果；以檔案為單位判定：同檔未改動的舊函式只靠相關測試會被誤判覆蓋率不足 | T2 quality.py 新增 --changed-since，只判定改動到的函式 | | 執行 python -m pytest skills/tdd-crap/tests -q 通過；測試確認改動函式超標時 FAIL、同檔未改動的函式即使覆蓋率不足也不列為 FAIL、未提交改動也算在內，不帶參數時輸出與既有測試相同 |
| R1 | | D3 合併後測試的 plan 在 tddCrap 開啟、有 testCommand 且未設 skipTests 時，writer 每次交付（write 與 resolve 輪）後先進「測試中」跑任務階段品質檢查：範圍是任務分支相對於 plan 分支 merge-base 的改動，原始碼取 testCommand 的 --source 清單中有改動的檔案，pytest 只跑 testCommand 測試路徑內有改動的測試檔，並帶 --changed-since merge-base。略過檢查、直接送審的情況：沒有改動的原始碼、有改動原始碼但沒有改動的測試檔、已安裝的 quality.py 不支援 --changed-since（每次啟動 Alfred 檢查一次，不支援時提示重跑 init）。這項檢查不等基準、不跟基準比、不佔 maxParallelTests 名額；有失敗名稱時退回 writer 並算一輪，提示說明只檢查本次改動的函式與改動的測試檔；失敗卻分不出名稱時照常送審，留給合併後的完整測試把關。testRun 事件記錄 scope 為 changed 與實際指令，不會被拿來當基準沿用 | 每個任務都跑完整測試：正是現在耗時的原因；維護一份基本測試清單：清單容易過時，也抓不到最常見的改動函式品質門檻；請 writer 自己跑品質檢查：Claude writer 只放行完整的 testCommand，而且 agent 可能略過步驟 | T3 任務階段加入只檢查改動函式的品質檢查，失敗時退回 writer | T1, T2 | 執行 python -m pytest tests/test_conductor.py tests/test_board_state_tasks.py tests/test_board_prompts.py -q 通過；測試確認：有改動原始碼與測試檔時跑帶 --changed-since 的指令；上列三種略過情況直接送審；品質門檻失敗退回 writer、輪數加一且提示含報告位置；分不出名稱時照常送審；基準還沒跑完時也能檢查；同時有完整測試在跑時不必排隊 |
| R1 | | D4 合併後完整測試延到 plan 裡沒有任務還能自己往前推進時才開始。還能推進的狀態清單：寫程式中、測試中、待審查、審查中、合併中，以及前置都已滿足的排隊中。不擋測試的狀態：已合併待測試、完成、放棄、等你處理，以及前置還沒滿足的排隊中。開始後照現有規則一次涵蓋所有已合併待測試的任務，新增失敗涵蓋多個任務時沿用既有的逐一拆分找出原因；合併後測試等你處理的既有規則不變 | 維持每次合併後就測：依序完成的 plan 會跑很多次完整測試；等 plan 全部任務完成才測：有任務停在等你處理時，其他已合併的任務會一直沒人測 | T4 合併後完整測試等 plan 內沒有可推進的任務才跑 | T3 | 執行 python -m pytest tests/test_conductor_after_merge.py tests/test_board_state_merge_tests.py -q 通過；測試確認：T1 已合併但 T2 還在寫程式、待審查或前置已滿足的排隊中時不開始；剩下的任務都在不擋測試的狀態時開始且一次涵蓋所有已合併待測試的任務；另一個任務停在等你處理時仍會開始；失敗涵蓋多個任務時照舊逐一拆分 |
| R1 | | D5 README「測試把關」改寫成分層說明：任務階段的改動函式品質檢查與三種略過情況、合併後完整測試的開始時機與狀態清單、基準沿用時不影響測試的檔案清單，並更新 maxParallelTests 說明，註明任務階段檢查不佔名額 | 只在程式註解說明：使用者看不到看板為何停在「測試中」或為何還沒跑完整測試 | T5 更新 README 的測試把關說明 | T4 | 開 README.md「測試把關」段落，可以看到任務階段檢查、合併後完整測試的開始時機與基準沿用的檔案清單，內容和 T1–T4 的行為一致 |

## 交付流程與範圍

- T1 改 conductor.py；T2 只改 skills/tdd-crap（quality.py、測試、SKILL.md），兩者可同時進行。T3 依 T2 的參數，且和 T1 都改 conductor.py，所以前置寫 T1、T2；T4 也改 conductor.py，前置寫 T3；T5 只改 README.md，等 T4 完成以描述最終行為。
- 合併後由使用者把 plan 分支合併回 main 並重跑 init，更新 .agents/skills/tdd-crap 與 .claude/skills/tdd-crap 的副本；更新前 Alfred 偵測到 quality.py 不支援 --changed-since，會維持現行流程。writer 不執行 init，也不手改這些產生的副本。其他已初始化的專案同樣要各自重跑 init。
- 原流程（改版前核准、在任務寫完後跑完整測試的 plan）維持不變。
- 不在範圍：讓完整測試本身變快（共用 Git 測試專案、平行執行）、依函式把品質門檻失敗直接歸因到任務以省去逐一拆分、改審查 prompt。
- Python 改動依既有 tdd-crap 流程執行。
