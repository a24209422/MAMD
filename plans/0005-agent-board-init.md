---
id: P0005
created: 2026-10-27
area: [工具/初始化, 工具/Skill]
---

# 提供 agent-board-init，一次設定規劃與 TDD skill 並支援換電腦重新初始化

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 我希望的是，打agent-board-init，就會設定agent-board-plan和tdd-crap | 使用者呼叫 agent-board-init 後，由同一流程安裝兩個 skill、寫入專案規則並設定 Python 專案的 TDD，不必分別執行 install_skill.py 與 init --tdd | D1 提供隨 repo 交付、可由 agent 發現的 agent-board-init skill，將機械性的檢查與安裝交給共用初始化腳本；入口以 AI 對話中的 skill 呼叫為準，使用目前專案作為設定目標 | 只在說明文件列出多個手動指令：沒有達成一個入口完成設定；依賴使用者自行建立 shell alias 或 PATH：新電腦 clone 後仍不能使用 | T1 建立 agent-board-init 的可發現入口、共用腳本介面與隨 repo 交付的資源配置，補入口及資源完整性測試 | | 在 clone 下來的 repo 中開啟支援的 agent，可以發現並呼叫 agent-board-init；入口明確指向目前專案與隨 repo 交付的資源，沒有固定磁碟機或使用者路徑 |
| R1 | | D2 共用初始化流程將 agent-board-plan 與完整 tdd-crap 安裝到目標專案可讀取的位置，沿用既有來源與管理標記；同時建立 AGENTS.md、必要忽略規則及可再次呼叫的初始化入口；預先檢查同名自訂 skill，避免安裝一半才失敗 | 只安裝 plan skill、仍要求另外安裝全域 tdd-crap：保留原本的分裂流程；只複製 SKILL.md：缺少 quality.py、輔助腳本及套件清單；直接覆蓋使用者自訂同名 skill：會遺失既有內容 | T2 實作一次設定兩個 skill 與專案規則的流程，保留使用者既有內容並支援安全重跑，擴充初始化與安裝測試 | T1 | 在沒有全域 tdd-crap 的環境只呼叫一次入口，目標專案出現兩個完整 skill 與初始化規則；重跑不產生重複段落；自訂同名 skill 發生衝突時明確指出檔案，且既有內容不變 |
| R1 | | D3 Python 專案由入口啟用 tddCrap 與品質檢查，從專案設定判定來源、測試目錄與 Python 環境；安裝缺少的 pytest、coverage、radon 到選定的專案環境；品質檢查與 writer prompt 優先使用專案內 skill，保留舊專案全域安裝的相容性 | 寫死 src、tests 或系統 python：平鋪專案、虛擬環境及換電腦後容易失效；只複製 skill 卻未啟用 TDD：自動開發不會使用；遇到套件缺少就改成 skipTests：無法滿足 TDD 需求 | T3 接通 TDD 設定、環境準備、skill 路徑解析與 writer／reviewer 指引，補初始化及提示詞回歸測試 | T2 | 對 src/tests 與根目錄 Python 專案分別只呼叫初始化入口，確認 tddCrap 為 true、檢查指令使用正確環境與目錄；Claude 與 Codex writer 都取得實際可讀取的 skill；行為測試可跑出失敗再通過，品質檢查確實執行；既有一般測試與略過模式的明確選項仍可使用 |
| R2 我之後推到github上在其他電腦clone下來後，也是同樣只要打agent-board-init就可以設定 | 換電腦後不能依賴原電腦的使用者目錄、全域 skill、虛擬環境或 D:\MAMD-stable；clone MAMD 或已初始化的目標專案後，均有可發現的初始化入口與可取得的初始化資源 | D4 將入口、必要模板與可攜的工具來源資訊納入 Git；重新初始化時定位 clone 中的工具，必要時依來源資訊取得工具到本機管理的位置，再以新電腦的實際位置產生設定；機器專屬的環境與 .board 執行狀態不隨 Git 搬移 | 只更新本機全域 skill 或 PowerShell profile：GitHub 不會帶走設定；將目前的絕對路徑寫入版本控制：其他電腦無法解析；要求另一台電腦先手動完成一串 skill 安裝指令：沒有達成單一入口 | T4 完成 clone 後的工具定位與首次設定流程，讓初始化入口及必要來源資訊隨目標專案交付，加入隔離使用者目錄與搬移路徑測試 | T3 | 將 MAMD 及已初始化的範例專案分別 clone 到不同且含空白的路徑，在沒有原有全域 skill、工具路徑及虛擬環境的環境呼叫 agent-board-init，兩個 skill 與 TDD 設定均完成；生成內容不引用舊電腦路徑，重跑仍成功 |
| R2 | | D5 README 以 agent-board-init 作為一般入口，交代 clone、呼叫位置、兩個 skill 的用途、環境先決條件及進階選項；使用隔離環境驗證完整流程，並執行既有初始化、安裝與 conductor 提示詞回歸 | 文件仍以分開安裝 skill 與手動選 init 模式作為主要教學：使用者仍需記住多組指令；只用開發者已安裝過 skill 的環境驗證：無法證明新電腦可用 | T5 更新使用說明並完成首次初始化、重跑、換電腦 clone 的端到端驗證與既有測試回歸 | T4 | 按 README 從乾淨 clone 操作，不執行額外的 skill 安裝指令即可完成設定；回報實際指令、結果及未執行的真人 agent 驗證，不能以靜態檔案存在代替可呼叫與可執行的驗證 |

## 呼叫方式與使用者體驗

- 目前依前文討論 skill 的脈絡，將 agent-board-init 規劃為在 AI 對話中呼叫的初始化 skill；不同 agent 的實際呼叫語法須在 README 寫清楚。它不是要求使用者預先建立的終端機 alias。
- 使用者追加詢問「這個agent-board-plan是codex和claude都會呼叫的嗎」；本計畫明確以 Codex 與 Claude 皆能直接發現及呼叫 agent-board-init、agent-board-plan 為驗收目標，不能只產生 Claude skill，再以 Codex 會讀 AGENTS.md 當作完成雙方支援。
- 初始化入口及必要資源必須隨 Git 可取得；不得把第一次使用建立在「已先手動安裝 agent-board-init」這個循環前提上。MAMD clone 本身應可發現入口，初始化後的目標專案也須帶有入口。
- 一般成功路徑只呼叫 agent-board-init，即完成 agent-board-plan、tdd-crap 與 Python 專案的 TDD 設定；不要求使用者另外記住 install_skill.py 或 --tdd。
- 可以保留來源／測試目錄、工具位置等進階覆寫選項。僅在無法合理判定目錄、環境或遇到既有自訂內容衝突時詢問必要資訊，不在正常重跑時反覆確認。
- 原有 Python、Git 與使用中的 agent 需可執行；clone 與登入等既有前提須交代清楚。首次下載工具或套件失敗時回報未完成項目與重試方式，不宣稱初始化成功。

## Codex 與 Claude 的入口契約

| Agent | 專案內 skill 目錄 | 初始化呼叫 | 規劃呼叫 |
|---|---|---|---|
| Codex CLI／IDE | .agents/skills/ | $agent-board-init | $agent-board-plan 加上需求 |
| Claude Code | .claude/skills/ | /agent-board-init | /agent-board-plan 加上需求 |

- T1 須交付兩種 agent 可發現的初始化入口；T2 須設定兩邊的 agent-board-plan 與 tdd-crap。兩邊內容使用同一份維護來源，安裝時處理必要的相對路徑與主機格式差異，避免規則分叉。
- T3 的技能路徑解析與提示詞須支援兩個專案目錄；T4、T5 的乾淨 clone 驗證須分別覆蓋 Codex 與 Claude 的入口，不能以其中一個 agent 的成功替代另一個。
- 新增或更新後如需刷新技能清單或重新開啟 agent，需在結果與 README 中說明實際操作；尚未由真人 agent 驗證的部分須明列。
- Codex 的專案技能目錄及 $skill 呼叫方式依據 [OpenAI Docs：Build skills](https://learn.chatgpt.com/docs/build-skills)。目前 repo 的 board.py 僅建立 .claude/skills/agent-board-plan，這是此次要補齊的差異。

## 規劃、實作與審查的分工

- 使用者追加詢問「如果agent-board-plan設定成claude、codex都可以讀，那麼會影響到後續的寫程式和審查嗎」；兩種 agent 均可使用規劃 skill，不應因此改變 writer／reviewer 的任務、權限或既有 CLI 與模型設定。
- agent-board-plan 保持由使用者明確呼叫。Claude 保留 disable-model-invocation: true；Codex 透過 agents/openai.yaml 設定 allow_implicit_invocation: false，避免 writer 或 reviewer 自動載入規劃 skill。agent-board-init 同樣只在使用者明確要求初始化時執行，不在寫程式或審查時自動重設專案。
- AGENTS.md 中「先寫 plan、不要自己寫程式」須清楚限定為接需求與規劃的角色；自動流程的 writer 依已核准任務實作並執行 TDD，reviewer 依需求、diff 與測試結果唯讀審查，兩者不重新撰寫或送出 plan。
- 沿用並明確化 board_prompts.py 現有角色分工；規劃角色的限制不套用至 writer／reviewer，但使用者專屬核准操作、不可手改 .board/ 等共通限制仍有效，不能籠統要求忽略整份 AGENTS.md。
- T2 須產生有明確角色適用範圍的規則與技能呼叫政策；T3 須驗證 writer 可進行實作、reviewer 保持唯讀，且只在設定啟用 TDD 時取得對應品質檢查指引；T5 回歸驗證兩種 CLI 的規劃入口、實作與審查流程，包含沒有自動呼叫 init／plan 或更動既有模型設定。

## 資源與環境契約

- agent-board-plan 使用目前 repo 的規劃規則來源；tdd-crap 包含 SKILL.md、scripts/quality.py、scripts/_quality_entries.py 與 requirements.txt，不能只交付名稱或說明文字。
- Python 環境應沿用專案選定的環境；沒有可用專案環境時由初始化流程準備。初始化與 conductor 執行品質檢查必須使用一致的 Python，不靠原電腦 PATH 的偶然順序。
- 來源及測試目錄先讀專案設定與既有目錄；辨識不明時才詢問。不可為了讓初始化成功而把原始碼目錄任意擴大，或把測試當成來源碼掃描。
- tdd-crap 目前只適用 Python。其他語言可以設定兩個 skill，但不能套用 Python 品質檢查後宣稱已支援該語言的 TDD；需清楚說明限制並沿用使用者明確選定的測試方式。
- 保留既有模型、並行度與其他不相干設定；若既有測試設定與預設 TDD 衝突，顯示要變更的內容並取得明確選擇，不靜默覆蓋。
- 專案內優先解析 skill 路徑；既有依賴全域 tdd-crap 的專案須相容。可攜來源資訊與應提交的檔案須明確列出，.board/、虛擬環境及本機快取維持忽略。
- 重跑只更新本工具管理的內容，保留其他 AGENTS.md 規則及使用者自訂 skill；不替使用者 commit、push 或核准 plan。

## 實作與驗收邊界

- T1 → T2 → T3 → T4 → T5 依序執行；共用 board.py、安裝流程、提示詞與文件的改動均沿此前置關係，避免同檔並行修改。
- 初始化腳本與 skill 以 repo 內的維護來源為準，不依賴本機未納入 Git 的副本；新增 skill 的實作須遵守適用的 skill 建立規則。
- 自動驗證使用臨時 Git repo、隔離使用者目錄、可控套件／工具取得替身，避免覆蓋開發者實際安裝。另以乾淨環境驗證真實套件安裝與完整呼叫流程，缺少網路或 agent 可用性時據實記錄限制。
- 使用者已明確要求「這次先不要經過多agent的步驟，你直接做，做完記得驗證」。本次由目前 agent 直接實作與驗證，不經 Alfred 派工，不代替使用者執行核准或變更 plan 事件狀態；本文件保留為需求與驗收依據。
- 實作採隨目標專案交付 .agent-board/runtime 的方式提供工具，clone 後不需另外下載 MAMD 程式碼或依賴原電腦安裝位置；.agent-board/setup.json 保存可攜偏好，本機 .venv 與 .board/config.json 由初始化重建。
