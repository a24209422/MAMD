---
id: P0017
created: 2026-10-30
area: [後端/派工, 後端/設定, 後端/成效, 前端/儀表板]
---

# 依任務難度自動選擇 writer 的模型與推理強度，被測試或審查退回時自動升一級

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 如果我也想要讓模型自己判斷任務難度，自己選擇模型和推理強度，應該怎麼做？ | 由寫 plan 的 agent 替每個任務標難度（簡單、一般、困難），使用者核准時看得到也能改；Alfred 依設定把難度對應到 writer 的 CLI、模型和推理強度；任務被測試或審查退回時，下一輪自動升一級；記下每一輪實際用的難度，成效依難度統計，用來校正分級 | D1 plan 新增「## 任務難度」段落，內容是 `\| 任務 \| 難度 \| 理由 \|` 表格；rtm_graph.py 解析成任務編號對應難度；難度只接受「簡單」「一般」「困難」；送出時檢查：段落裡的任務編號不在追溯矩陣是紅色問題，難度不是這三種是紅色問題，有任務沒標是黃色提示並視為「一般」；沒有這個段落的舊 plan 全部視為「一般」、不另外提示；程式碼區塊（```）裡的內容不算；這個段落仍屬於表格外的說明，照舊附給 writer 與 reviewer | 在追溯矩陣加一欄：欄位是固定的，改欄位會影響所有既有 plan、線路圖與解析；寫進任務文字（例如「T1 （困難）…」）：任務文字會出現在 status、事件與儀表板各處，混在裡面不好解析也不好改 | T1 解析並檢查 plan 的「任務難度」段落，補 tests/test_rtm_graph.py 與送出檢查的測試 | | 執行 python -m pytest -q 通過；新增的測試確認：三種難度解析正確；未知的任務編號或難度會擋下送出；漏標的任務出現黃色提示並當成「一般」；沒有這個段落的舊 plan 行為不變 |
| R1 | | D2 .board/config.json 的 writer 加選填的 `tiers`，鍵是三種難度，值可寫 cli、model、reasoningEffort、contextWindow、command，沒寫的欄位沿用 writer 本身，沒設定的難度直接用 writer 本身（範例見下方「設定範例」）；board_core.py 新增依難度取得 writer 設定的函式；configProblems 檢查 tiers 的鍵只能是三種難度，值的欄位沿用既有角色設定的檢查；沒有 tiers 時所有任務都用 writer 本身，舊設定不受影響 | 讓模型直接填模型名稱：成本無法控制，模型名稱也會過時；把分級寫死在程式裡：每個人的訂閱與可用模型不同，要能自己設定 | T2 writer 設定加上 tiers 與檢查，提供依難度取得 writer 設定的函式，補設定檢查的測試 | | 執行 python -m pytest -q 通過；新增的測試確認：tiers 只寫 model 時其他欄位沿用 writer；鍵不是三種難度或欄位格式錯誤時 configProblems 會回報；沒有 tiers 時三種難度都取得 writer 本身的設定 |
| R1 | | D3 conductor.py 啟動 writer 時，以「plan 標的難度」加上「這個任務被退回的次數」決定這一輪的難度：審查不通過、測試出現新增失敗各算一次，解衝突和使用者的 decide 不另外加，最高到「困難」；board_core.py 的任務狀態新增退回次數，由 reviewFailed 事件與 verdict 為 newFailure 的 testRun 事件推導，舊事件照樣推導得出；依這一輪的難度取得 writer 設定（含 command 解析），taskStarted 事件加記 tier；換了模型時沿用現有 writerSession 的判斷開新對話，prompt 照舊帶上一輪的審查意見或測試失敗；終端機印出這一輪的難度，升級時註明「由一般升級」 | 每個任務先叫小模型判斷難度：多一次呼叫，而且只看得到任務文字，資訊比看過需求與程式碼的規劃 agent 少；只靠失敗升級、不事先標：困難的任務會先白跑一輪；讓 writer 自己決定：模型在啟動前就要選定，模型也不擅長預估自己做不做得到 | T3 Alfred 依難度與退回次數選擇 writer 設定並記入事件，補 tests/test_conductor.py 與 tests/test_board_state_tasks.py | T1, T2 | 執行 python -m pytest -q 通過；新增的測試確認：標「簡單」的任務第一輪用簡單的設定，審查退回後第二輪用「一般」；「困難」被退回仍是「困難」；解衝突那一輪不升級；taskStarted 記有 tier；沒有 tiers 設定時行為和現在相同 |
| R1 | | D4 docs/AGENTS-snippet.md 的「plan 的寫法」加上「任務難度」段落的格式與判斷標準（見下方「難度的判斷標準」）；writer 不能執行 board.py init，所以同時直接修改本專案的 AGENTS.md agent-board 區塊、.claude/skills/agent-board-plan/SKILL.md 與 .agents/skills/agent-board-plan/SKILL.md，內容和 snippet 一致 | 只改程式不改規則：寫 plan 的 agent 不知道要標，所有任務都會變成「一般」 | T4 寫 plan 的規則加上任務難度的寫法與判斷標準，同步修改 AGENTS.md 與兩份 agent-board-plan skill，補對應的測試 | | 執行 python -m pytest -q 通過；AGENTS.md 與兩份 SKILL.md 看得到「任務難度」段落與判斷標準，三份內容和 docs/AGENTS-snippet.md 一致 |
| R1 | | D5 board_stats.py 的 collectStats 加上依難度的統計：每種難度的任務數、第一次審查就通過的比例、升級次數（某一輪的 tier 比同一任務前一輪高就算一次），依 taskStarted 的 tier 計算，舊事件沒有 tier 時記為「未標」；board.py 的 stats 細節多一行，例如「難度：簡單 3 個任務（第一次審查就通過 3/3）、一般 2 個（1/2，升級 1 次）」 | 只記錄不統計：看不出分級合不合理，沒辦法回頭校正設定 | T5 成效依難度統計並在 stats 顯示，補 tests/test_board_stats.py 與 tests/test_board_cli.py | T3 | 執行 python -m pytest -q 通過；新增的測試確認：依 tier 分組的任務數與通過比例正確、升級次數正確、舊事件記為「未標」；執行 python board.py stats 加上某份 plan 的編號，看得到「難度」這一行 |
| R1 | | D6 web/js/plan.js 的成效區塊在「模型」前面加「難度」一列，文字和 stats 細節那一行相同 | 由 board_api 回傳排好的文字：現有成效的每一列都由前端自己排版，維持同一種做法 | T6 儀表板「任務」頁的成效區塊加上「難度」，補 tests/test_web.py | T5 | 執行 python -m pytest -q 通過；開儀表板的「任務」頁並選一份核准過的 plan，成效區塊看得到「難度」，內容和 python board.py stats 該 plan 的那一行相同 |
| R1 | | D7 README：「換模型」加上 tiers 的欄位說明與設定範例；「測試把關」或「指令」的 decide 說明裡補上被退回時升一級的規則；成效指標表加上「難度」一列 | 只改程式不改 README：使用者不知道怎麼設定 tiers，也看不懂為什麼第二輪換了模型 | T7 更新 README 的 tiers 設定、升級規則與成效指標 | T3, T4, T5 | README 看得到 tiers 的設定範例、「被退回就升一級、最高到困難」的規則，以及成效指標表的「難度」一列 |

## 難度的判斷標準

寫 plan 的 agent 依下列標準替每個任務標難度，拿不準時標「一般」：

| 難度 | 什麼時候用 |
|---|---|
| 簡單 | 改一兩個檔案、照既有的寫法做、不需要新的設計，例如補說明、加一個欄位、照現有模式加一個測試 |
| 一般 | 要新增函式或類別、跨幾個檔案，但做法清楚 |
| 困難 | 跨模組、牽涉併發、狀態機、效能或安全，或做法有明顯取捨、容易漏掉邊界情況 |

「任務難度」段落的格式：標題是二級標題「任務難度」，底下一張三欄表格，欄位依序是「任務」「難度」「理由」，每個任務一列，例如：

```text
T1｜簡單｜只改 README
T2｜困難｜要改 conductor 的狀態推導，牽涉中斷與重試
```

（上面用「｜」示意每一列的三欄，實際要寫成 Markdown 表格。這裡不直接放標題和表格，避免實作 T1 之後，這份 plan 自己的範例被當成真的難度設定。）

## 設定範例

```json
{
  "writer": {
    "cli": "claude", "model": "opus", "reasoningEffort": "high",
    "tiers": {
      "簡單": {"model": "sonnet", "reasoningEffort": "medium"},
      "困難": {"reasoningEffort": "max"}
    }
  }
}
```

- 這個例子裡「一般」沒有設定，所以用 writer 本身（opus、high）。
- 實際可用的模型與推理強度以儀表板「模型」分頁列出的為準，上面的數值只是範例。

## 背景

- 需求來自使用者在對話中的提問：參考 Lauren Tan 在 Peter Yang 訪談中的做法，工程 bot 會依任務性質開不同模型與推理強度的雲端 agent。在 MAMD 裡，「派工的一方」對應到寫 plan 的 agent 和 Alfred。
- 對話中討論的原則：由派工的一方先預估，再依實際失敗的訊號升級；模型很不擅長事先判斷題目難不難，實際失敗比預測準確。
- 目前 writer 只有一組設定，所有任務都用同一個模型和推理強度，簡單的任務也用最貴的設定。

## 範圍

- 只調整 writer；reviewer 維持一組設定。
- 不包含：在儀表板「模型」分頁編輯 tiers（第一版直接改 .board/config.json）、依成效自動調整分級、讓模型改寫 plan 標的難度。
- P0016 也會修改 board_stats.py、board.py 的 stats 與 web/js/plan.js 的成效區塊；兩份 plan 的分支之後合併到同一條分支時，這幾個檔案可能需要解衝突。
