---
id: P0024
created: 2026-11-01
area: [後端/prompt, 工具/Skill]
---

# 調整規劃、writer、reviewer 規則，讓同類問題一次處理、無法達成的驗證交由使用者決定

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 你建議如何修改這個問題 | P0023 被審查退回 21 次，主因是驗證寫成涵蓋全部情況的條件，卻沒有列出要涵蓋的情況清單；writer 每輪只修被點名的反例，reviewer 每輪再找出同類的下一個。另有條件本質上無法列舉（P0023 T1 用規則遮蔽任意文字），只能等 reviewer 找不到反例才結束。要從規劃、writer、reviewer 三個角色調整規則：規劃時先列清單，同類問題一次處理，無法達成的條件交給使用者決定 | D1 在 docs/AGENTS-snippet.md「plan 的寫法」的驗證說明下新增兩條規則：（1）做法或驗證寫到「所有」「任何」「都要」「不包含」這類涵蓋全部情況的條件時，在 AI 理解或做法列出要涵蓋的有限清單，例如會派工的入口、plan 狀態、要檢查的欄位，writer 與 reviewer 都以這份清單對照；（2）列不出有限清單的條件，例如任意文字裡不含任何機密，不寫成驗證，改寫成可判斷的做法（不輸出該內容、偵測到就拒絕、或限定已知格式），並在不採用寫明取捨。AGENTS.md 與 agent-board-plan skill 都由這份檔案產生，不另外手改 | 在 submit 偵測「所有」「任何」等字眼並擋下：字面判斷容易誤擋，也抓不到沒用這些字的全面條件；只改 writer、reviewer 提示：清單沒在規劃時定好，兩邊各自列舉仍會不一致 | T1 在 plan 撰寫規則加入涵蓋清單與不可列舉條件的寫法 | | 開 docs/AGENTS-snippet.md 看到兩條新規則；在 MAMD 重跑 init 後，AGENTS.md、.claude/skills/agent-board-plan/SKILL.md 與 .agents/skills/agent-board-plan/SKILL.md 出現相同文字；執行 python -m pytest tests/test_board_cli.py -q 通過 |
| R1 | | D2 buildWritePrompt 附上一輪審查意見時，在「請修正必改的每一條」之後加上修正方式：先判斷每條必改屬於哪一類問題（例如同一個檢查漏了某個入口、某種狀態或某種輸入），在這個任務的範圍內找出同類的所有地方一起修並各自補測試，summary 列出這次修了哪些地方；必改指出目前做法本質上無法完整達成驗證、需要使用者決定時，status 填 blocked 並在 blockedReason 說明原因，不要只修單一反例。這段只在附審查意見時出現；第一輪、解衝突及只因測試退回而沒有附審查意見的提示維持原樣 | 要求 writer 掃描整個專案的同類寫法：超出任務範圍，容易順手重構；提高輪數上限或放寬審查：只會延後或掩蓋同類問題 | T2 writer 重寫提示改成同類問題一起修，並在做法無法達成時回報 blocked | | 執行 python -m pytest tests/test_board_prompts.py -q 通過；測試確認帶審查意見的重寫 prompt 含歸類、同類一起修、summary 列出位置與回報 blocked 的說明，第一輪與只因測試退回的 prompt 不含這段 |
| R1 | | D3 在 reviewerRules 新增兩條：（1）找到一個不符合需求或驗證的情況時，檢查同類的其他情況（其他呼叫入口、狀態、輸入格式），合併成同一條必改並列出所有位置；（2）判斷目前做法本質上無法完整達成驗證時（例如以列舉規則過濾任意文字），在必改寫明「做法無法完整達成驗證，需要使用者決定是否修改 plan」及原因，而不是只列一個反例。規則放在 reviewerRules，不改 buildReviewPrompt 的區段與順序 | 新增審查結論類型（例如 planIssue）讓流程自動停下：要改 conductor、事件與看板，現有 writer 回報 blocked 已能交給使用者決定；要求 reviewer 一次列出所有可能的反例：對無法列舉的條件做不到，只會讓審查更久 | T3 reviewer 規則加入同類情況合併列出與做法無法達成時的寫法 | T2 | 執行 python -m pytest tests/test_board_prompts.py -q 通過；測試確認 reviewerRules 含兩條新規則；既有三個審查 prompt 完整輸出測試不需修改仍通過 |

## 交付流程與範圍

- T1 只改 docs/AGENTS-snippet.md，與 T2、T3 不改同一個檔案，可同時進行；T3 和 T2 都改 board_prompts.py 與 tests/test_board_prompts.py，所以 T3 的前置寫 T2。
- 合併後由使用者在 MAMD 重跑 init，更新本專案的 AGENTS.md 與 agent-board-plan skill；其他已初始化的專案也要各自重跑 init 才會拿到新規則。writer 不自行執行 init，也不手改這些產生的檔案。
- 不在這份範圍：P0023 T1 遮蔽規則仍會漏掉的寫法（例如 credential=abc123、「密碼是 p@ssw0rd」）待使用者選定做法後另立 plan；不改 conductor 的輪數上限、事件格式或看板。
- Python 改動依既有 tdd-crap 流程執行。
