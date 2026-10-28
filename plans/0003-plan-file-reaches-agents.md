---
id: P0003
created: 2026-10-28
area: [後端/核准, 後端/prompt]
---

# 送出 plan 通過檢查時自動 commit plan 檔，並把 plan 表格以外的說明交給寫程式和審查的 agent

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 「我要審查都通過後自動commit」（使用者選的時間點：送出 plan 通過檢查時） | submit 沒有紅色問題時，流程自動 commit 這份 plan 檔，而且只 commit 這一個檔案，主資料夾其他已暫存或沒暫存的改動都不動；plan 檔跟 HEAD 一樣時不另外 commit；commit 失敗（例如 git hook 擋下、沙箱不能寫 .git）時不送出，印出 git 的錯誤和使用者可以自己執行的 git add、git commit 指令，自己 commit 之後再 submit 就會通過；核准時如果 plan 檔在送出後又被改過、跟 HEAD 不同，擋下並提示重新 submit，免得 plan 分支拿到舊版；終端機和儀表板的核准都經過 cmdApprove | D1 board_git 新增兩個函式：檢查單一檔案是否已 commit 而且跟 HEAD 一樣（git ls-files --error-unmatch 加上 git diff --quiet HEAD -- 路徑），以及只 commit 單一檔案（git add -- 路徑，再 git commit -m 訊息 -- 路徑，訊息是「P0003 送出 plan：標題」這種格式）；cmdSubmit 在紅色問題檢查通過後、寫 planSubmitted 之前 commit；cmdApprove 在紅色問題檢查之後檢查 plan 檔，跟 HEAD 不同就回傳 1；README「使用」說明送出時會自動 commit plan 檔 | 核准時才 commit：使用者指定在送出通過檢查時；用 git commit -a 或 git add 全部：會把主資料夾其他改動一起 commit；只擋下、要使用者自己 commit：使用者要的是自動 commit | T1 submit 通過檢查時自動只 commit plan 檔、commit 失敗時不送出，approve 擋下送出後又被改過的 plan 檔，board_git 新增兩個函式，README 補說明；新增 CLI 和儀表板核准的測試，既有測試受影響的一併調整 | | 執行 python -m pytest -q 通過，新增的測試確認：submit 一份沒被追蹤的 plan 後，最新的 commit 只有這個 plan 檔，主資料夾其他已暫存和沒暫存的改動都還在、沒有被 commit；plan 檔沒變時再 submit 不會多一個 commit；commit 失敗時 submit 回傳 1、印出 git add 和 git commit 指令、events.jsonl 沒有 planSubmitted；送出後改了 plan 檔再 approve 會回傳 1、提示重新 submit、沒有 planApproved；儀表板按核准時顯示同一段訊息 |
| R2 「兩項都寫成 P0003」第 2 項：writer prompt 附上表格外的段落：plan 裡表格以外的內容也一起交給 writer，就不必依賴 worktree 裡有沒有 plan 檔 | plan 檔裡除了標題和追溯矩陣表格以外的文字（例如表格下方的規則表、契約、對照值），由流程從主資料夾的 plan 檔讀出，附進每個任務的 prompt；寫程式（含被退回的修正輪）和解衝突的 prompt 都附；審查的 prompt 也附，reviewer 才能照同一份規格判斷（這點是 AI 補充的，原本的建議只寫 writer）；沒有表格以外的文字時不加這一段 | D2 rtm_graph 的 analyzePlan 另外記下 body 裡去掉「# 標題」那行和追溯矩陣（表頭、分隔線、資料列）之後的文字，前後空白去掉，plan 裡其他不是追溯矩陣的表格照原樣保留 | 只附跟這個任務有關的段落：表格外的段落沒有任務編號，分不出屬於哪個任務，猜錯反而漏給；叫 writer 自己去讀主資料夾的 plan 檔：Claude writer 讀 worktree 外面的路徑會被拒絕 | T2 analyzePlan 取出 plan 裡表格以外的說明文字，補 tests/test_rtm_graph.py（表格前後都有文字、只有表格、有其他非追溯矩陣的表格） | | 執行 python -m pytest -q 通過，新增的測試確認：表格前後的文字都取得到、標題那行和追溯矩陣的列都不在裡面、其他表格保留、只有表格時是空的 |
| R2 | | D3 board_prompts 的寫程式、解衝突、審查 prompt 在「plan 裡和這個任務有關的列」後面加一段「plan 裡表格以外的說明（整份 plan 共用）」；超過 30000 個字元就截斷並明講，做法跟 diff 太長時一樣；README 說明表格外的內容會附給每個任務的 agent | 把整份 plan 檔原文附上：表格列已經單獨附過，整張表會重複，而且其他任務的列會讓 writer 誤以為要一起做 | T3 寫程式、解衝突、審查的 prompt 附上 plan 表格以外的說明，超過上限時截斷並註明，README 加上說明；補 tests/test_board_prompts.py 和用假 agent 檢查收到的 prompt | T1, T2 | 執行 python -m pytest -q 通過，新增的測試確認：plan 表格下方有一段規則表時，假 agent 收到的寫程式、解衝突、審查 prompt 都有這段；沒有表格外文字時 prompt 跟改之前一樣；超過 30000 字元時截斷並註明 |

## 需求來源

- 使用者先回覆「好，兩項都寫成 P0003」，之後把第 1 項改成「我要審查都通過後自動commit」，並選了時間點「送出 plan 通過檢查時」。R2 引號後面是 Claude 在 2026-10-28 對話中提出的建議原文，起因是 P0002 的 T5 因為 plan 檔沒 commit、writer 只拿到表格列而回報卡住。
- 這份 plan 和 P0002 都會改 board.py、board_prompts.py、README；建議 P0002 合併進 main 之後再核准這份，核准時的 HEAD 才會包含 P0002 的改動。
