---
id: P0003
created: 2026-10-28
area: [後端/Agent, 後端/Context]
---

# 讓寫程式與審查 agent 在 context 超過 60% 時自動壓縮並繼續任務

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 現在不管是寫程式還是審查都不會自己壓縮context，我希望它們在超過60%時就會壓縮 | MAMD 啟動的 writer、reviewer 與其平行工作席位，無論選用 Claude 或 Codex，都在目前對話 context 使用量超過有效視窗的 60% 時由 CLI 自動壓縮；壓縮後繼續原任務，writer 續接對話與 reviewer 重審也要適用 | D1 共用固定 60% 的門檻計算，以角色有效 contextWindow 作為分母，門檻採整數 tokens；對視窗設定增加正整數及非 bool 驗證，明訂設定必須符合實際模型視窗，區分目前 context 與累計 usage，保留舊設定未填視窗時的預設行為 | 以累計 input_tokens 判斷：重複讀取對話會累加，不能當作目前 context；只改儀表板警示：不會啟動壓縮；新增可調百分比介面：本需求已指定 60% | T1 在 board_core.py 建立共用壓縮門檻及視窗驗證，於 tests/test_board_events.py 補預設、自訂、角色切換與無效數值測試 | | 執行 .venv/Scripts/python.exe -m pytest -q tests/test_board_events.py；200000 與 475000 視窗分別得到 120000、285000 tokens，非整數、零、負數及 bool 不能進入啟動設定，writer 與 reviewer 各取自己的視窗 |
| R1 | | D2 Codex writer 新對話、exec resume 與 reviewer exec 均透過每次啟動的 -c 傳入 model_auto_compact_token_limit，使用總 active context 的門檻語意；依 CLI 支援明確指定 total 計算範圍，避免使用者的 body_after_prefix 設定改變 60% 的含義；保留沙箱、模型、推理強度與結構化輸出參數 | 僅在新對話設定：續接可能漏掉門檻；修改使用者全域 config.toml：會影響 MAMD 以外的對話；另起新 thread 當壓縮：會丟失原對話狀態 | T2 修改 board_agents.py 的 Codex 啟動參數，補 tests/test_board_agents.py 中新對話、resume、review、不同視窗與原有權限參數的回歸測試 | T1 | 執行 .venv/Scripts/python.exe -m pytest -q tests/test_board_agents.py；三種 Codex 啟動均帶正確門檻，resume 保留 thread ID，reviewer 保持 read-only，writer 保持 workspace-write，不改全域設定 |
| R1 | | D3 Claude 使用已安裝 CLI 版本支援的原生自動壓縮設定，僅傳給該 AgentJob 子程序；依版本驗證 CLAUDE_AUTOCOMPACT_PCT_OVERRIDE、CLAUDE_CODE_AUTO_COMPACT_WINDOW 或 --autocompact 的實際計算方式及優先序，選出使有效門檻為視窗 60% 的組合；處理繼承的停用及覆寫設定，不能重複套用 60%；不支援或被設定阻止時明確回報原因 | 只寫提示詞要求自行 /compact：無人互動模式不保證執行；固定只傳百分比：部分模型與版本不適用；同時把 window 設為 60% 再乘 60%：門檻變成 36%；直接修改 os.environ：會污染平行工作與其他 CLI | T3 在 board_agents.py 增加每個 AgentJob 的環境覆寫支援與 Claude 壓縮啟動策略，補 tests/test_board_agents.py 的子程序隔離、設定優先序、停用衝突及 CLI 相容性測試 | T2 | 執行 .venv/Scripts/python.exe -m pytest -q tests/test_board_agents.py；Claude write、resume 與 review 採同一 60% 策略，兩個不同角色可同時使用各自的門檻，父程序與 Codex 子程序不受 Claude 設定污染，無法生效時不能回報已啟用 |
| R1 | | D4 conductor 將角色啟動快照的視窗及壓縮策略接到所有 writer、reviewer 啟動路徑，涵蓋重試、解衝突、重審與平行席位；依現有設定刷新方式從下一個新啟動程序生效；以假 CLI 驗證接線，再以實際 CLI 證據驗證壓縮後仍能完成結構化結果與續接 | 殺掉程序後從頭重跑：不等於壓縮且可能重複工作；讓 reviewer 取得寫入權限處理壓縮：壓縮由 CLI 完成，不需擴權；用假 CLI 通過宣稱原生壓縮已驗證：只能證明參數與環境傳遞 | T4 修改 conductor.py 接通雙角色雙 CLI 的門檻與環境，擴充 tests/test_conductor.py、tests/fakes/ 的行為測試，更新 README.md 並完成必要回歸及實際 CLI 驗證紀錄 | T3 | 執行 .venv/Scripts/python.exe -m pytest -q tests/test_board_agents.py tests/test_board_events.py tests/test_conductor.py tests/test_conductor_control.py tests/test_board_api.py；四種角色與 CLI 組合均取得 60% 門檻，writer 重試沿用 session，reviewer 可回傳有效審查 JSON；受控長對話跨越門檻後由原生 CLI 壓縮並繼續，沒有增加任務失敗輪數或放寬 reviewer 權限；回報實際 CLI 版本、觸發與繼續工作的證據，未執行的真人驗證明列未完成 |

## 門檻與執行範圍

- 本需求限定 MAMD 由 Alfred 啟動的工作程序，不自動修改使用者另開的互動終端機或全域設定。
- 使用者的「超過 60%」指目前對話佔有效 context window 的比例，非費用、額度、累計 token 或任務進度。採原生 CLI 在可壓縮的檢查點觸發，工具回覆可能讓使用量跨過門檻；不承諾在 token 恰好等於門檻時中斷正在進行的工具或串流。
- 現有 contextWindow 是儀表板分母，這次也作為壓縮門檻依據。README 必須說明：角色更換模型時，視窗值須與實際模型相符；既有 Claude 200000、Codex 475000 是專案預設，不能宣稱適用所有模型。不得透過放大 CLI 的真實視窗規避限制。
- Codex exec 的 turn.completed usage 可能是整輪統計，不可用目前儀表板數字作為即時壓縮開關或唯一成功證據；以 CLI 原生壓縮與後續任務結果驗證。
- Claude 原生壓縮可能保留必要指示、工具定義及摘要，不能保證每次壓縮後都降至 60% 以下。若固定內容已過大，須明確回報，不能無限重啟或把失敗說成成功。
- 正在執行的程序不會被這份 plan 直接變更；合併後重新啟動 Alfred，後續啟動的程序使用新策略。
- Python 功能實作依專案 tdd-crap 規則走 Red、Green、Refactor，完成修改函式的必要覆蓋率、複雜度與 CRAP 檢查；不得為新行為降低既有品質門檻。

## 查核依據與實作前確認

- [OpenAI 官方設定參考](https://learn.chatgpt.com/docs/config-file/config-reference)：提供 model_auto_compact_token_limit 與其 total／body_after_prefix 範圍設定。實作時確認目前 CLI 是否支援使用的選項。
- [Claude 官方環境變數](https://code.claude.com/docs/en/env-vars)：提供百分比、壓縮視窗及停用變數；[模型與自動壓縮設定](https://code.claude.com/docs/en/model-config#context-window-and-auto-compaction) 說明不同模型及版本的觸發與優先序。實作不能把所有版本都當成百分比設定必定有效。
- 2026-10-28 本機唯讀查核：Claude Code 2.1.311；PowerShell 的 codex.ps1 被執行原則擋下，尚未取得 Codex 版本。實作驗證應使用已安裝的原生入口或 codex.cmd，不修改系統執行原則。
- 目前有其他尚未提交的程式修改；核准任務以當時 Git commit 為基礎，需由使用者先整理開發基線。本 plan 不包含提交既有修改、核准任務、修改 .board 設定或啟動 Alfred。
