# P0014 T12 復原與完整品質驗收操作說明

這份說明給使用者照著操作：把 P0020 的測試修正帶進 P0014，讓 T12 再跑一輪，最後確認完整品質檢查真的通過。

- P0020 完成，只代表 `plan/P0020` 分支有修正。`plan/P0014` 與 T12 的 worktree 不會自動拿到，要照下面的步驟手動整合。
- 本文件的所有步驟都由使用者執行。`decide`、`stop`、`resume` 這類指令只能由使用者執行，agent 不會代勞。
- 以下路徑以主專案 `D:\MAMD`、worktree 根目錄 `D:\MAMD-worktrees` 為例。換電腦時，請換成 `<專案>` 與 `<專案>-worktrees`。

## 1. 這次修正了什麼

只改了 `tests/test_conductor.py` 這一個檔案，正式程式沒有改：

| 改動 | 用途 |
|---|---|
| import 多加 `fakesDir` | 給下面的延遲啟動 reviewer 找到 `tests/fakes/fake_codex.py` |
| 新增 `killJobs` | 重啟的 conductor 不在 fixture 的清理清單裡，測試失敗時要靠它結束子程序 |
| 新增 `firstReviewCallRecorded` 與 `test_firstReviewCallRecordedNeedsCompleteJson` | `call-review-P0001-T1-1.json` 必須能完整解析，才算第一次審查呼叫已記錄；寫到一半的檔案不算 |
| 新增 `pauseAfterFirstReviewCall` | 先等到「審查中」，再等第一次 reviewer 呼叫記錄下來，之後才送出 `paused` 事件 |
| 修改 `test_noReviewUntilBaselineForChangedCommandIsRecorded` | 改用 `pauseAfterFirstReviewCall`，重啟後的流程用 `try/finally` 包起來並呼叫 `killJobs`；原本的基準與審查順序斷言都保留 |
| 新增 `gatedReviewer` 與 `test_pauseAfterDelayedReviewerStartRecordsFirstCall` | 讓 reviewer 子程序停在閘門前，穩定重現「已派工、reviewer 還沒記錄呼叫」的時間點；重啟後應該只多一次 `pass` 呼叫 |

原因：「審查中」狀態在父程序派工時就寫入，但假 reviewer 要等子程序實際啟動後，才遞增呼叫次數並寫出呼叫紀錄。舊測試一看到「審查中」就暫停，重啟後可能又拿到第一次的 `hang`，最後卡在審查中。

## 2. 開始前的確認

1. 確認 P0020 已完成：

   ```
   python board.py status P0020
   ```

   P0020 的 T1、T2 都要是完成狀態。

2. 確認 P0014 目前沒有任務在跑。下面的指令中，P0014 只有 T12 停在等你處理，其他任務都是完成，沒有撰寫中、審查中、合併中或已合併待測試：

   ```
   python board.py status P0014
   ```

   記下 T12 那一行 `｜` 後面的原因，第 5 步會用到。

3. 找出要帶進 P0014 的修正 commit，也就是 `plan/P0020` 上「合併 P0020 T1」那個 merge commit：

   ```
   git log --format="%H %s" --grep="合併 P0020 T1" plan/P0020
   ```

   下面用 `<修正commit>` 代表這個 hash。如果 T1 有多輪，以最新一筆為準。

4. 確認 P0014 的 plan worktree 和 T12 worktree 都是乾淨的，兩個指令都不應該有輸出：

   ```
   git -C D:/MAMD-worktrees/P0014 status --short
   git -C D:/MAMD-worktrees/P0014-T12 status --short
   ```

   如果有輸出，先弄清楚那些改動是什麼，不要直接丟掉。T12 再跑一輪時，流程可能會清理任務 worktree，沒有 commit 的改動會不見。

## 3. 把修正整合進 `plan/P0014`

`plan/P0014` 已經 checkout 在 `D:\MAMD-worktrees\P0014`，所以要在那個 worktree 裡操作。

建議用 cherry-pick 只帶進這次的測試修正：

```
git -C D:/MAMD-worktrees/P0014 cherry-pick -m 1 <修正commit>
```

- `-m 1` 的意思是取這個 merge commit 相對於 `plan/P0020` 前一個 commit 的差異，也就是只有 `tests/test_conductor.py` 的改動。
- 不建議合併整個 `plan/P0020` 或只合併 `main`。前者會把和 P0014 無關的改動帶進來。後者即使 `main` 已經有修正，`plan/P0014` 和 T12 worktree 裡仍然是舊測試。

### 有衝突時

P0014 的 T3、T4 也在 `tests/test_conductor.py` 補過測試，所以可能會有衝突。解法是兩邊都保留：

- import 那一行：保留 P0014 加進來的名稱，再加上 `fakesDir`。
- P0014 新增的測試：全部保留，不改斷言。
- 本次修正的函式與測試：完整保留，包括 `pauseAfterFirstReviewCall`、`try/finally` 裡的 `killJobs`，以及 `assert baselines[1] < reviews[1]` 等斷言。
- 不可以用刪測試、`skip`、`xfail` 或 `--deselect` 來解衝突。

解完之後：

```
git -C D:/MAMD-worktrees/P0014 add tests/test_conductor.py
git -C D:/MAMD-worktrees/P0014 cherry-pick --continue
```

最後確認 plan worktree 是乾淨的（沒有輸出），而且已經包含修正：

```
git -C D:/MAMD-worktrees/P0014 status --short
git -C D:/MAMD-worktrees/P0014 grep -n -e "def pauseAfterFirstReviewCall" -e "def test_pauseAfterDelayedReviewerStartRecordsFirstCall" -- tests/test_conductor.py
```

grep 應該兩個函式都找到。

記下目前 plan 分支最新的 commit，第 6 步驗收時會用到：

```
git -C D:/MAMD-worktrees/P0014 rev-parse HEAD
```

下面用 `<P0014修正commit>` 代表這個 hash。這個 commit 在 T1～T11 的合併之後，所以只要某個 commit 包含它，就同時包含 P0014 既有的改動和本次修正。

## 4. 讓 `task/P0014-T12` 也包含修正

T12 的測試是在它自己的 worktree 裡跑的，所以 T12 也必須包含修正。

1. 先檢查 T12 分支是不是 `plan/P0014` 的祖先：

   ```
   git -C D:/MAMD-worktrees/P0014-T12 merge-base --is-ancestor task/P0014-T12 plan/P0014
   ```

   接著查看結束代碼：Git Bash 用 `echo $?`，PowerShell 用 `$LASTEXITCODE`。

2. 結束代碼是 `0` 代表可以快轉，執行：

   ```
   git -C D:/MAMD-worktrees/P0014-T12 merge --ff-only plan/P0014
   ```

3. 結束代碼是 `1` 代表 T12 有還沒合併的 commit，不能快轉。這時改用一般合併，保留 T12 的改動：

   ```
   git -C D:/MAMD-worktrees/P0014-T12 merge --no-ff -m "P0014 T12 合併含 P0020 修正的 plan 分支" plan/P0014
   ```

   有衝突的話照第 3 節的原則兩邊都保留，解完後 `git add` 並 `git commit`。

   不能省略這一步。T12 如果是被合併後測試退回的修正輪，流程開始撰寫時會對 plan 分支執行 `merge --ff-only`；分支已經分岔時，這個動作會失敗。

4. 如果 `D:\MAMD-worktrees\P0014-T12` 不存在，可以跳過這一步。流程開始撰寫 T12 時，會從最新的 `plan/P0014` 開出 worktree。

5. 確認 T12 worktree 是乾淨的，並且包含修正：

   ```
   git -C D:/MAMD-worktrees/P0014-T12 status --short
   git -C D:/MAMD-worktrees/P0014-T12 grep -n "def pauseAfterFirstReviewCall" -- tests/test_conductor.py
   ```

6. （選做）在 T12 worktree 單獨跑一次相關測試，確認修正生效：

   ```
   cd D:/MAMD-worktrees/P0014-T12
   D:/MAMD/.venv/Scripts/python.exe -m pytest tests/test_conductor.py -q -k "ReviewCall or BaselineForChangedCommand or DelayedReviewerStart"
   ```

   應該全部通過。這只是相關檢查，完整檢查仍然交給 Alfred。

## 5. 在看板對 T12 選「再跑一輪」

由使用者操作。可以在看板網頁按 T12 的「再跑一輪」，或執行下面的指令：

```
python board.py decide P0014 T12 retry --note "P0020 已修正 test_noReviewUntilBaselineForChangedCommandIsRecorded 的啟動競態，plan/P0014 與這個 worktree 都已包含修正。writer 只跑相關測試、修正品質超標的函式並如實交付；完整 testCommand 由 Alfred 在交付後執行，不能把交付前沒跑完整驗證當成 blocked 的理由。品質沒過時讀 .quality/report.json 找 FAIL 的函式修正，不得用 skip、--deselect、pragma 或調低門檻。"
```

依第 2 步記下的原因，再跑一輪的效果不一樣：

| T12 停下的原因 | 選再跑一輪後 |
|---|---|
| 「合併後的完整測試無法判定有沒有新增的失敗…」（合併後測試分不出結果） | 不再找 writer，直接對 `plan/P0014` 最新的 commit 重跑完整 testCommand。第 3 步加進來的 cherry-pick commit 會包含在內 |
| 輪數用完、測試沒過、agent 回報卡住等其他原因 | 開新的一輪，T12 writer 依 `--note` 修正並交付，之後流程跑測試、審查、合併 |

T12 writer 的分工：

- 只做相關檢查、修正品質問題，並如實交付。
- 不要自己在背景跑整套測試。
- 完整 testCommand 由 Alfred 執行，包含 `test_noReviewUntilBaselineForChangedCommandIsRecorded`，不可以排除。

## 6. P0014 的驗收條件

流程把 T12 或 P0014 標成完成，不等於 P0014 已經驗收。原因：

- 判定為 `preexisting` 只代表沒有比基準多出 pytest 失敗。
- `qualityChecked` 為 `false` 代表 pytest 沒有全過，所以根本沒有跑每個函式的品質檢查。

這兩種情況流程都可能讓任務繼續往下走，但都還沒達成 P0014 的目標。

在主專案根目錄執行下面的指令，查看 P0014 最新一次完整測試紀錄。這個指令只讀 `.board/events.jsonl`，不會修改它：

```
python -c "import json; rows=[json.loads(l) for l in open('.board/events.jsonl', encoding='utf-8') if l.strip()]; e=[r for r in rows if r['plan']=='P0014' and r['type'] in ('testRun','mergeTestRun')][-1]; d=e['data']; print(e['type'], e['task'], d.get('commit')); print('command:', d.get('command')); print('passed:', d.get('passed'), 'verdict:', d.get('verdict')); print('qualityChecked:', d.get('qualityChecked'), 'qualityFailures:', d.get('qualityFailures'), 'failures:', d.get('failures')); print(d.get('tail', '').splitlines()[-1:])"
```

全部符合下列條件，P0014 才算完成：

1. 紀錄的 `commit` 包含 P0014 全部改動與本次修正：
   - `git merge-base --is-ancestor <P0014修正commit> <commit>` 的結束代碼要是 `0`。這代表 T1～T11 和本次修正都在裡面。
   - 如果是 `testRun`（T12 worktree 的測試），`<commit>` 也要是 T12 最後一輪交付的 commit；`git log -1 --format=%H task/P0014-T12` 的結果要和它相同。
   - 如果是 `mergeTestRun`（合併後測試），`<commit>` 要是合併 T12 之後 `plan/P0014` 最新的 commit；`git rev-parse plan/P0014` 的結果要和它相同。
2. `command` 就是專案設定的完整 testCommand，沒有 `--deselect`、`-k` 或其他排除測試的參數。
3. `passed` 為 `true`（pytest 全部通過，品質檢查也通過）。
4. `qualityChecked` 為 `true`，`qualityFailures` 為空的 `[]`，`failures` 也是空的。
5. tail 最後一行是 `PASSED: ...report.json`。如果查看的是任務 worktree 留下的 `.quality/report.json`，裡面的 `status` 要是 `passed`，`functions` 裡每一筆的 `failures` 都要是空的，也就是沒有任何 FAIL。

下面幾種情況都還沒達成驗收：

- 只有 `verdict` 是 `preexisting`。
- `qualityChecked` 為 `false`。
- 還有任何 `品質門檻 ...` 名稱。

## 7. 還有品質 FAIL 時

- T12 又停在等你處理：照第 5 步再跑一輪，在 `--note` 裡列出 FAIL 的函式，交給 T12 修正，之後由流程再測。
- T12 已經被流程標成完成，但第 6 步的紀錄還有品質 FAIL：看板不能對已完成的任務再跑一輪。這時不要把 P0014 當成已驗收，請規劃角色另開 plan，修剩下的 FAIL 函式。
- 不論哪種情況，都不能用排除測試、加 `# pragma: no cover`、調低門檻或只消除警告的方式代替驗收。
