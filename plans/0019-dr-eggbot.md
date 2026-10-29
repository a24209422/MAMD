---
id: P0019
created: 2026-10-30
area: [後端/常駐 bot]
---

# 在常駐 bot 模組加入 Dr. Eggbot：依範本設計新 bot，並健檢所有 bot 的對話記錄、提出要你確認才套用的改進建議

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 我想要做這個（Dr. Eggbot：設計 bot 的 bot，會定期翻看所有 bot 的對話記錄，提出新增 skill、拆出新 bot 等改進建議），應該怎麼做？我想要一個 mvp | 在 P0018 的 bot_house 模組上加三個指令：`eggbot new` 用問答幫你設計新 bot，草稿確認後才建立；`eggbot audit` 讀所有 bot 的身分設定、記憶與最近的對話記錄，先用程式篩出有問題訊號的片段，再交給模型產生有編號的建議報告；`eggbot apply` 套用某一條建議，套用前要你確認並先備份；第一版手動執行，不含排程 | D1 新增 modules/bot_house/eggbot.py 與 cli.py 的 `eggbot new` 指令：依序問 5 個問題（要它負責什麼、不負責什麼、交付格式、怎麼確認自己做對了、平常會遇到的例子），再用 P0018 的 runner 呼叫 Claude（不接續 session），以 schema 回傳 `{"name": 食物名字, "role": 一句話, "sections": {"負責的事", "不負責的事", "交付格式", "怎麼確認自己做對了"}}`；印出草稿，使用者輸入 y 才用 store 建立 bot 並寫入 profile.md 對應段落；名字已存在或不符規則時請模型重取一次，仍不行就請使用者自己輸入；`--name` 可以直接指定名字 | 不經確認直接建立：bot 的設定要使用者看過才算數；只用固定範本、不呼叫模型：使用者的回答通常很口語，需要整理成清楚的職責與邊界 | T1 eggbot new：問答、產生草稿、確認後建立 bot，用假 CLI 補 tests/test_bot_house_eggbot_new.py | | 執行 python -m pytest -q 通過；實際執行 `python -m modules.bot_house eggbot new`，回答問題後看到草稿，輸入 y 後 `list` 出現新 bot，profile.md 的四個段落都有內容；輸入 n 時不建立任何檔案 |
| R1 | | D2 新增 modules/bot_house/audit.py 的收集步驟，不呼叫模型：讀每個 bot 的 profile.md、memory.md，以及最近 14 天（`--days` 可調）的 logs；計算每個 bot 的對話數、訊息數、使用者訊息裡的糾正詞次數（不是、不要、不對、我說過、又、錯了、重來，以及英文 no、wrong、again，整個詞比對）、記憶字數；挑出含糾正詞的使用者訊息和它前一則 bot 回覆當作代表片段，每個 bot 最多 20 段、每段截到 500 字；輸出成一份 JSON 摘要 | 整份對話直接丟給模型：bot 多、對話長時用量很大，先篩選只送有訊號的片段 | T2 健檢的資料收集與機械訊號，補 tests/test_bot_house_audit_collect.py（用暫存資料夾造假的 bot 與記錄） | | 執行 python -m pytest -q 通過；新增的測試確認：糾正詞計數正確、超過天數的記錄不算、片段數量與截斷長度不超過上限、沒有記錄的 bot 也出現在摘要裡 |
| R1 | | D3 audit.py 的分析與報告，以及 cli.py 的 `eggbot audit` 指令：把 D2 的摘要交給 Claude，以 schema 回傳 `{"proposals": [{"id", "bot", "kind", "reason", "evidence", "change"}]}`，kind 只能是「拆分」「寫進設定」「做成固定流程」「整理記憶」「其他」；報告寫到 <home>/reports/audit-YYYYMMDD-HHMM.md 與同名的 .json，每條建議有編號、原因、引用的對話片段與要改的內容；沒有任何建議時也產生報告並寫明「沒有發現問題」；判斷標準寫在給模型的系統提示（見下方「健檢的判斷標準」） | 自動套用建議：違反逐步建立信任的原則，使用者要先看過；kind 讓模型自由填：後面的 apply 要依類型決定怎麼改檔，必須是固定的幾種 | T3 健檢分析與報告、eggbot audit 指令，用假 CLI 補 tests/test_bot_house_audit_report.py | T1, T2 | 執行 python -m pytest -q 通過；實際操作：讓某個 bot 處理三個不相關的主題，並在不同對話重複糾正它同一件事，執行 `eggbot audit`，報告裡有「拆分」與「寫進設定」兩類建議，而且每條都附了對話片段 |
| R1 | | D4 新增 modules/bot_house/apply.py 與 cli.py 的 `eggbot apply <報告的 .json> <編號>`：「寫進設定」附加到 profile.md 的「規則」段；「做成固定流程」附加到 profile.md 的「固定流程」段；「整理記憶」用建議的新內容取代 memory.md；「拆分」用建議的設定建立新 bot，並在原 bot 的「不負責的事」加一行「<內容>交給 <新 bot>」；「其他」只印出建議、不改檔；套用前印出變更內容，輸入 y 才寫入；寫入前把原檔備份成「原檔名.bak-YYYYMMDD-HHMMSS」；套用過的建議記在報告的 .json，重複套用時提示並要求再確認一次 | 直接覆寫不備份：記憶與設定改壞了沒辦法復原；一次套用整份報告：建議可能互相衝突，一條一條確認比較安全 | T4 eggbot apply：依建議類型改檔、確認、備份與記錄，補 tests/test_bot_house_apply.py | T3 | 執行 python -m pytest -q 通過；實際操作：套用一條「寫進設定」後，profile.md 的「規則」段多了該條，旁邊有 .bak 備份；套用「拆分」後 `list` 出現新 bot，原 bot 的「不負責的事」多了交接的那一行；輸入 n 時沒有任何檔案改變 |
| R1 | | D5 modules/bot_house/README.md 加上 Dr. Eggbot 段落：三個指令的用法、健檢看哪些訊號、報告放在哪裡、套用前會確認並備份、怎麼用備份還原 | 只靠指令的 --help：健檢的判斷標準與備份還原方式需要較完整的說明 | T5 README 加上 Dr. Eggbot 的說明 | T4 | 照 README 的步驟可以建立一個新 bot、跑一次健檢並套用一條建議，最後用 .bak 檔還原 |
| R2 三件都寫：後兩件做成 MAMD 的 modules/ 模組（像 system_monitor 那樣） | 放在 P0018 建立的 modules/bot_house 裡，沿用它的 store、runner 與 CLI；主系統不改；測試檔放在 tests/ 底下 | D1 | | T1 | | 執行 git diff --stat 檢查整份 plan 的改動，只在 modules/bot_house/ 與 tests/ |

## 健檢的判斷標準

給模型的系統提示要包含下列標準，每條建議都要引用摘要裡的片段當證據，沒有證據就不提：

| 類型 | 什麼時候提出 |
|---|---|
| 拆分 | 一個 bot 的對話分散在幾個彼此無關的主題，profile 的「負責的事」也涵蓋不了 |
| 寫進設定 | 使用者在不同對話裡重複糾正同一件事，或說過「我說過」「又」這類話 |
| 做成固定流程 | 同一種多步驟的請求出現兩次以上 |
| 整理記憶 | 記憶裡有重複、互相矛盾或已經過時的條目，或記憶超過 8000 字 |
| 其他 | 不屬於上面的建議，只供參考、不會自動改檔 |

## 背景

- 需求來自使用者在對話中，針對 Peter Yang 訪談 Lauren Tan 那一集提到的 Dr. Eggbot：它會依嚴謹的範本設計新 bot，並用 routine 定期翻看所有 bot 的對話記錄，提出新增 skill、拆出新 bot、調整 routine 等建議。
- **這份 plan 建在 P0018 上，要等 P0018 完成並合併到核准時所在的分支後再核准**，否則 modules/bot_house 不存在。
- 原版的「routine 太頻繁、太耗用量」檢查要等 bot_house 有 routine 之後才做得到，這份 plan 不包含。
- 套用前確認、先備份，對應影片裡的觀點：信任要逐步建立，建議先由人看過才套用。

## 範圍

- 不包含：排程自動健檢（之後可以用 Windows 工作排程器或 bot_house 的 routine）、routine 成本檢查、儀表板頁面、Codex CLI。
- 不修改 conductor.py、server.py、web/。
