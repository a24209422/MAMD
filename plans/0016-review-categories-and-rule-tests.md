---
id: P0016
created: 2026-10-30
area: [後端/審查, 後端/成效, 前端/儀表板, 後端/測試把關]
---

# 審查的必改加上類型、成效列出重複出現的類型，並把第一條能機械檢查的規則寫成 pytest 規則測試

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 讓 stats 依類型彙整重複出現的必改，提醒你哪些該升級成規則 | 現在審查結果的必改只有 ref 和 text，事件只記條數，看不出哪一類問題一直重複；要讓 reviewer 替每條必改標上固定的類型，事件記下類型，`stats` 和儀表板的成效依類型統計，同一類在 2 個以上任務出現時提示考慮寫成規則測試 | D1 board_review.py 新增必改類型清單常數（定義見下方「必改類型」）；reviewResultSchema 的 mustFix 項目加 `category`，用 enum 限定在清單內並列進 required（Codex 的 output schema 要求所有欄位都在 required）；validateReview 檢查 category 在清單內，不在時錯誤訊息列出可用的類型，沿用現有「格式不符先重審一次」的流程；renderReviewMarkdown 的必改寫成 `- [R1｜測試沒有真的驗證] 內容`；board_prompts.py 的 reviewerRules 加上「每條必改的 category 從下列類型擇一」與六種類型的一句話定義；tests/fakes/fake_agent.py 的 fail 結果補上 category，讓用假 agent 的流程測試照常通過 | 讓 stats 呼叫模型把必改文字分群：每次看成效都要花用量，結果也不穩定，stats 目前只讀事件；讓 reviewer 自由填類型：同一類會寫成不同的字，算不出重複；直接用 ref 當類型：ref 是需求編號，跨 plan 沒有意義 | T1 審查結果的必改加上 category：schema、驗證、審查檔顯示、reviewer 規則與假 agent，補 tests/test_board_review.py、tests/test_board_prompts.py | | 執行 python -m pytest -q 通過；新增的測試確認：缺少 category 或類型不在清單內時 validateReview 回傳錯誤並列出可用類型；審查檔的必改顯示成 `- [R1｜測試沒有真的驗證] …`；Claude 與 Codex 的 reviewer 規則都列出六種類型；用假 agent 跑的審查不通過流程照常退回 writer |
| R1 | | D2 conductor.py 的 finishReview 寫 reviewPassed／reviewFailed 事件時，data 加 `categories`：依必改順序列出每條的類型，通過時是空清單；mustFix、suggest 條數照舊；終端機印出審查不通過時，「必改 N 條」後面接上類型，例如「必改 2 條（測試沒有真的驗證、漏掉邊界情況）」 | 讓 stats 改讀 .board/reviews/ 的審查檔解析類型：stats 目前只讀事件，審查檔的顯示格式一改就會算錯 | T2 審查事件記下每條必改的類型，終端機的審查不通過訊息列出類型；補 tests/test_conductor.py | T1 | 執行 python -m pytest -q 通過；新增的測試確認：審查不通過的事件 data 有 categories 且順序和必改一致，通過的事件是空清單；終端機訊息含類型；沒有 categories 的舊事件照樣能推導出任務狀態 |
| R1 | | D3 board_stats.py 的 collectStats 加 `mustFixCategories`：每種類型的必改條數與出現在幾個不同的任務；舊事件沒有 categories 時，把該事件的 mustFix 條數記為「未分類」；依條數由多到少排，同數照第一次出現的順序；類型出現在 2 個以上不同任務、而且不是「其他」或「未分類」的，列進 `repeated`；board.py 的 `stats P0016` 細節多一行「必改類型：測試沒有真的驗證 2（2 個任務）、漏掉邊界情況 1（1 個任務）」，沒有必改時寫「（無）」；不帶 plan 的 `stats` 在總計後面，總計有 repeated 時多印一行「重複出現的必改類型：測試沒有真的驗證；考慮寫成規則測試（見 README「專案規則測試」）」 | 門檻設 3 個任務：目前 3 份 plan 只有 3 條必改，門檻太高永遠看不到提示；只看條數不看任務數：同一個任務連續幾輪被退回同一個問題會被誤判成「重複出現」；把類型統計放進每份 plan 的摘要列：摘要列已經很長，細節與總計才是看趨勢的地方 | T3 成效依類型統計必改，標出重複出現的類型，`stats` 的細節與總計顯示出來；補 tests/test_board_stats.py、tests/test_board_cli.py | | 執行 python -m pytest -q 通過；新增的測試確認：兩個任務各有一條「測試沒有真的驗證」時列進 repeated，同一任務兩輪各一條時不列；舊事件記成「未分類」；「其他」重複出現也不提示；在專案資料夾執行 python board.py stats P0009，看得到「必改類型：未分類 2（1 個任務）」這種舊資料的統計 |
| R1 | | D4 web/js/plan.js 的 statsItems 在「plan 退回」後面加「必改類型」一列，文字和 `stats` 細節那一行相同；有 repeated 的類型後面標「重複」 | 由 board_api 回傳排好的文字：現有成效的每一列都是前端自己排版，維持同一種做法 | T4 儀表板「任務」頁的成效區塊加上「必改類型」；補 tests/test_web.py | T3 | 執行 python -m pytest -q 通過；開儀表板的「任務」頁、選一份核准過的 plan，成效區塊看得到「必改類型」，內容和 python board.py stats 該 plan 的那一行一樣 |
| R2 把專案規則寫成 pytest 測試，違規會直接以 FAILED 測試名稱出現，現有的基準比對機制就能自動處理，不用改 conductor | 規則寫成一般的 pytest 測試，放在固定位置；違規時測試失敗，名稱出現在 short test summary，Alfred 現有的基準比對會把它當成新增的失敗退回 writer；第一條規則選目前只靠 reviewer 檢查、但能機械判斷的：不得用 `# pragma: no cover` 或 coverage 排除設定繞過品質門檻 | D5 新增 tests/test_rules.py：用 `git ls-files "*.py"` 取得 git 追蹤的 Python 檔，以 tokenize 只看 COMMENT token，找出含 `pragma: no cover`（大小寫與空白寬鬆比對）的註解；讀 pyproject.toml 的 `[tool.coverage.run]`、`[tool.coverage.report]`，以及存在時的 `.coveragerc`、`setup.cfg` 的 coverage 段落，不得有 `omit`、`exclude_lines`、`exclude_also`；違規時失敗訊息列出「檔案:行號」或「檔案 段落.設定名稱」，讓 writer 直接找到；找檔案與判斷的部分寫成函式，用 tmp_path 的假檔案補單元測試（含「字串裡出現這段文字不算違規」）；規則測試本身就在 tests/ 底下，現有 testCommand 會收集，不改 conductor 與設定 | 用 grep 找字串：board_prompts.py 與 tdd-crap 的測試在字串裡就有這段文字，會誤報；用 rglob 掃整個資料夾：本機 .venv 的套件有大量 pragma 註解；第一條先做「瀏覽器測試至少要有一個正向斷言」：P0011 之後 renderPage 沒準備好就丟 AssertionError，P0009 那條必改的情況已被機械擋下，掃描現有 518 個測試只有 1 個全是否定斷言，而且是合理的「什麼都不做」測試；在 conductor 新增獨立的規則檢查階段：pytest 測試的失敗名稱已經能跟基準比，多一個階段只是重做同一件事 | T5 新增 tests/test_rules.py：禁止 `# pragma: no cover` 註解與 coverage 排除設定，並補判斷函式的單元測試 | | 執行 python -m pytest -q 通過；在任一個 board_*.py 的函式後面暫時加上 `# pragma: no cover`，再跑 python -m pytest tests/test_rules.py -q，看到 FAILED 和違規的「檔案:行號」，拿掉後恢復通過；在 pyproject.toml 暫時加上 `[tool.coverage.report]` 的 `exclude_lines` 也一樣失敗 |
| R1 | | D6 README：「測試把關」底下新增「專案規則測試」小節，說明同一類必改重複出現時，把能機械判斷的部分寫成 tests/test_rules.py 裡的 pytest 測試，違規會以 FAILED 名稱跟基準比；新增規則的那個任務要同時修掉既有違規，否則會被當成新增的失敗退回；pytest 以外的測試工具分不出名稱，規則測試只在 pytest 與 tdd-crap 專案有效；「指令」的成效指標表加「必改類型」一列（算法同 D3）；說明 reviewer 的每條必改會標上類型，並列出六種類型 | 只改程式不改 README：使用者看到「重複出現的必改類型」提示時，不知道規則測試要寫在哪裡、怎麼生效 | T6 更新 README：專案規則測試、成效的必改類型指標、審查必改的類型 | T1, T3, T5 | README 看得到「專案規則測試」小節、成效指標表的「必改類型」一列，以及六種必改類型 |
| R2 | | D6 | | T6 | | README 的「專案規則測試」說明規則測試的位置、失敗名稱如何跟基準比，以及新增規則時要同時修掉既有違規 |

## 必改類型

reviewer 每條必改的 `category` 只能是下列其中一個，顯示與統計都用這六個字串：

| 類型 | 什麼時候用 |
|---|---|
| 需求未達成 | diff 沒做到需求、AI 理解或驗證欄要求的行為 |
| 弄壞既有功能 | 改動讓原本正常的功能或其他任務已合併的內容壞掉 |
| 測試沒有真的驗證 | 功能壞掉時測試仍會通過，例如只檢查某段文字不存在、替身繞過了要驗證的路徑、放寬斷言、刪除測試、排除覆蓋率 |
| 漏掉邊界情況 | 錯誤、逾時、中斷、競態、空資料等路徑沒有處理 |
| 偏離做法 | 實作和 plan 的做法（ADR）不同，或用了「不採用」欄排除的做法 |
| 其他 | 不屬於上面任何一類 |

`ref` 的規則不變：仍然只能填這個任務的需求編號或「驗證」。`ref` 說明違反了什麼，`category` 說明是哪一類問題，兩者分開記。

## 背景

- 需求來自使用者在對話中核准的建議：參考 Lauren Tan 的 Grok Bot workshop（2026-08）與 pstack 的 `correct` skill。主張是「同一條意見在 review 裡重複寫，就該變成 lint 或 CI 規則」，因為只靠 review 或 skill 的軟性規則，agent 遲早會忘記。
- `.board/reviews/` 目前共有 3 條必改：P0007-T1-r1（Ctrl+C 剛好落在兩次 tick 之間時，基準 worktree 沒有清掉，屬於漏掉邊界情況）、P0009-T1-r1（逾時時回傳空白 DOM，只檢查文字不存在的測試也會通過）、P0009-T1-r2（替身直接回傳預製 DOM，沒有讓 server 真的回 503）。後兩條都是「測試沒有真的驗證」，但事件只記了條數，`stats` 看不出這件事。
- P0009-T1-r1 指出的情況已在 P0011 由 tests/browser.py 機械擋下（畫面沒準備好就丟 AssertionError），這是「把 review 意見變成機械約束」的實例。第一條規則測試因此改選目前只寫在 reviewer 規則裡（board_prompts.py 的 tddReviewLines）、但能機械判斷的 `# pragma: no cover` 與 coverage 排除設定。tdd-crap 的品質檢查會把這些排除的函式列為 skipped，等於繞過門檻。
- 2026-10-30 確認：git 追蹤的 .py 檔裡，`pragma: no cover` 只出現在 board_prompts.py、tests/test_board_prompts.py、skills/tdd-crap/tests/test_quality.py 的字串裡，沒有真正的註解；pyproject.toml 的 coverage 設定只有 `[tool.coverage.run] patch = ["subprocess"]`。新規則加上去時應該直接通過。

## 範圍

- 不包含：依審查紀錄自動產生規則測試、修改寫 plan 的規則（docs/AGENTS-snippet.md）、替舊的審查檔補上類型、在 pytest 以外的測試工具上支援規則測試；這些之後視需要另寫 plan。
- 舊事件與舊審查檔不改寫；統計時舊事件一律記為「未分類」。
