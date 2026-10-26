import json
import os
import subprocess
import sys

import pytest

import board
import board_api
import board_core
import board_git
from board_core import appendEvent

twoTasks = ["| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |",
            "| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |"]
claudeLog = [
  {"type": "assistant", "message": {"content": [{"type": "text", "text": "先看 calc.py"}],
                                    "usage": {"input_tokens": 2, "cache_read_input_tokens": 30000,
                                              "cache_creation_input_tokens": 700, "output_tokens": 10}}},
  {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Write",
                                                 "input": {"file_path": "D:\\w\\P0001-T1\\calc.py", "content": "x"}}],
                                    "usage": {"input_tokens": 2, "cache_read_input_tokens": 33570,
                                              "cache_creation_input_tokens": 596, "output_tokens": 16}}},
  {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1",
                                            "content": "The file has been updated"}]}},
  {"type": "result", "subtype": "success", "structured_output": {"status": "done", "summary": "新增 calc.py"}},
]
codexLog = [
  {"type": "thread.started", "thread_id": "t1"},
  {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "{\"verdict\":\"pass\"}"}},
  {"type": "turn.completed", "usage": {"input_tokens": 18094, "cached_input_tokens": 0, "output_tokens": 94}},
]


def writeConfig(projectDir, **config):
  (projectDir / ".board").mkdir(exist_ok=True)
  (projectDir / ".board" / "config.json").write_text(json.dumps(config), encoding="utf-8")


def writeLog(projectDir, name, records):
  logsDir = projectDir / ".board" / "logs"
  logsDir.mkdir(parents=True, exist_ok=True)
  (logsDir / name).write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
                              encoding="utf-8")


def agentsById(view):
  return {agent["id"]: agent for agent in view["agents"]}


@pytest.fixture
def approvedProject(gitProject, writePlan):
  writePlan("0001-upload.md", twoTasks, planId="P0001", title="批次上傳", extraMeta="area: [前端/上傳, 後端]\n",
            projectDir=gitProject)
  assert board.cmdSubmit(gitProject, "P0001") == 0
  assert board.cmdApprove(gitProject, "P0001", confirm=lambda prompt: "y") == 0
  return gitProject


def test_agentsFollowMaxParallelAndModel(approvedProject):
  writeConfig(approvedProject, maxParallel=2, writer={"cli": "claude", "model": "sonnet"})
  view = board_api.stateView(approvedProject)
  assert list(agentsById(view)) == ["writer", "writer-sub1", "reviewer", "reviewer-sub1"]
  assert {agent["status"] for agent in view["agents"]} == {"閒置"}
  writer, reviewer = agentsById(view)["writer"], agentsById(view)["reviewer"]
  assert (writer["role"], writer["cli"], writer["model"]) == ("writer", "claude", "sonnet")
  assert (reviewer["role"], reviewer["cli"], reviewer["model"]) == ("reviewer", "codex", None)
  assert writer["context"] is None


def test_busyWriterShowsActionAndContext(approvedProject):
  appendEvent(approvedProject, "taskStarted", plan="P0001", task="T1", agent="writer", roundNumber=1,
              data={"mode": "write"})
  writeLog(approvedProject, "P0001-T1-r1-write-20261026-100000.log", claudeLog)
  claude = agentsById(board_api.stateView(approvedProject))["writer"]
  assert claude["status"] == "寫程式中"
  assert (claude["plan"], claude["task"], claude["round"]) == ("P0001", "T1", 1)
  assert claude["action"] == "Write calc.py"
  assert claude["context"] == {"tokens": 34168, "window": 200000}
  assert claude["lastActivity"]


def test_busyWithoutLogHasNullTokens(approvedProject):
  appendEvent(approvedProject, "taskStarted", plan="P0001", task="T1", agent="writer-sub1", roundNumber=1,
              data={"mode": "write"})
  agent = agentsById(board_api.stateView(approvedProject))["writer-sub1"]
  assert agent["status"] == "寫程式中" and agent["action"] is None
  assert agent["context"] == {"tokens": None, "window": 200000}


def test_reviewerAndResolvingStatuses(approvedProject):
  appendEvent(approvedProject, "taskStarted", plan="P0001", task="T1", agent="writer", roundNumber=1,
              data={"mode": "write"})
  appendEvent(approvedProject, "taskWritten", plan="P0001", task="T1", agent="writer", roundNumber=1,
              data={"commit": "abc", "mode": "write"})
  appendEvent(approvedProject, "reviewStarted", plan="P0001", task="T1", agent="reviewer", roundNumber=1)
  writeLog(approvedProject, "P0001-T1-r1-review1-20261026-100500.log", codexLog)
  appendEvent(approvedProject, "taskConflicted", plan="P0001", task="T2", roundNumber=1, data={"conflicts": ["a.txt"]})
  appendEvent(approvedProject, "taskStarted", plan="P0001", task="T2", agent="writer-sub1", roundNumber=2,
              data={"mode": "resolve"})
  view = board_api.stateView(approvedProject)
  agents = agentsById(view)
  assert agents["reviewer"]["status"] == "審查中" and agents["reviewer"]["action"] == "回覆審查結果"
  assert agents["reviewer"]["context"] == {"tokens": 18094, "window": 475000}
  assert agents["writer-sub1"]["status"] == "解衝突中"
  tasks = {task["task"]: task for task in view["tasks"]}
  assert tasks["T2"]["mode"] == "resolve" and tasks["T1"]["mode"] == "write"
  assert tasks["T1"]["worktree"].endswith("P0001-T1")


def test_pausedMarksEveryAgent(approvedProject):
  appendEvent(approvedProject, "paused", data={"reason": "額度用完"})
  view = board_api.stateView(approvedProject)
  assert view["paused"] is True and view["pauseReason"] == "額度用完"
  assert {agent["status"] for agent in view["agents"]} == {"已暫停"}


def test_needsYouHasReasonConflictsAndWorktrees(approvedProject):
  appendEvent(approvedProject, "needsYou", plan="P0001", task="T1", roundNumber=1,
              data={"kind": "mergeConflict", "reason": "合併衝突：a.txt", "conflicts": ["a.txt"]})
  item = board_api.stateView(approvedProject)["needsYou"][0]
  assert (item["plan"], item["task"], item["kind"], item["conflicts"]) == ("P0001", "T1", "mergeConflict", ["a.txt"])
  assert item["title"] == "任務一" and item["reason"] == "合併衝突：a.txt"
  assert item["since"] == board_core.readEvents(approvedProject)[-1]["time"]
  assert item["taskWorktree"].endswith("P0001-T1") and item["planWorktree"].endswith("P0001")


def test_recentEventsNewestFirstWithWho(approvedProject):
  for _ in range(55):
    appendEvent(approvedProject, "testRun", plan="P0001", task="T1", agent="writer", roundNumber=1,
                data={"passed": True})
  appendEvent(approvedProject, "paused", data={"reason": board.stopReason})
  appendEvent(approvedProject, "paused", data={"reason": "額度用完"})
  events = board_api.stateView(approvedProject)["recentEvents"]
  assert len(events) == 50
  assert [event["who"] for event in events[:3]] == ["系統", "你", "writer"]
  assert events[2]["text"] == "P0001 T1 writer 跑測試（第 1 輪，通過）"
  assert board_api.eventWho({"type": "planApproved", "agent": None, "data": {}}) == "你"


def test_planSummaryAreaCountsAndSupersede(approvedProject, writePlan):
  writePlan("0002-next.md", twoTasks, planId="P0002", title="下一版", extraMeta="supersedes: P0001\n",
            projectDir=approvedProject)
  plans = {plan["id"]: plan for plan in board_api.stateView(approvedProject)["plans"]}
  first = plans["P0001"]
  assert first["area"] == [{"side": "前端", "module": "上傳"}, {"side": "後端", "module": ""}]
  assert first["title"] == "批次上傳" and first["status"] == "進行中"
  assert first["tasks"] == {"total": 2, "done": 0}
  assert first["problems"] == {"red": 0, "orange": 0, "yellow": 0}
  assert first["source"] == "plans/0001-upload.md" and first["supersededBy"] == ["P0002"]
  assert plans["P0002"]["supersedes"] == ["P0001"] and plans["P0002"]["status"] == "草稿"


def test_unreadablePlanBecomesWarning(approvedProject):
  (approvedProject / "plans" / "0003-utf16.md").write_text("---\nid: P0003\n---\n", encoding="utf-16")
  assert board_api.stateView(approvedProject)["warnings"][0].startswith("0003-utf16.md 無法讀取")


def test_conductorStatusFollowsLockPid(approvedProject):
  assert board_api.stateView(approvedProject)["conductor"] == {"running": False, "pid": None}
  lockPath = approvedProject / ".board" / "conductor.lock"
  lockPath.write_text(str(os.getpid()), encoding="utf-8")
  assert board_api.conductorStatus(approvedProject) == {"running": True, "pid": os.getpid()}
  finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
  lockPath.write_text(finished.stdout.strip(), encoding="utf-8")
  assert board_api.conductorStatus(approvedProject)["running"] is False


def test_planViewUsesRtmGraph(approvedProject, writePlan):
  writePlan("0002-extra.md", twoTasks + ["| | | D2 額外做法 | | T3 額外任務 | | 驗證三 |"], planId="P0002",
            title="多一個任務", projectDir=approvedProject)
  assert board.cmdSubmit(approvedProject, "P0002") == 0
  view = board_api.planView(approvedProject, "P0002")
  plan = board_core.loadPlans(approvedProject)["P0002"]
  assert [node["key"] for node in view["graph"]["nodes"]] == list(plan["graph"].nodes)
  assert view["problems"] == plan["graph"].problems
  assert any(node["class"] == "extra" for node in view["graph"]["nodes"])
  assert {"from": "root", "to": "R1", "style": "solid"} in view["graph"]["edges"]
  assert view["status"] == "待核准" and view["blockers"] == []
  assert view["approveWarnings"] and view["approveWarnings"][0].startswith("橘｜")
  assert board_api.planView(approvedProject, "P0001")["approveWarnings"] == []
  assert board_api.planView(approvedProject, "P0099") is None


def test_planViewWithoutTable(approvedProject):
  (approvedProject / "plans" / "0004-empty.md").write_text("---\nid: P0004\n---\n\n# 空的\n", encoding="utf-8")
  view = board_api.planView(approvedProject, "P0004")
  assert view["graph"] is None and view["blockers"] == ["找不到追溯矩陣表格"]
  assert view["problems"] == {"red": [], "orange": [], "yellow": []}


def test_reviewTextOnlyAcceptsReviewNames(approvedProject):
  reviewsDir = approvedProject / ".board" / "reviews"
  reviewsDir.mkdir(parents=True, exist_ok=True)
  (reviewsDir / "P0001-T1-r1.md").write_text("# 審查\n必改：無", encoding="utf-8")
  assert board_api.reviewText(approvedProject, "P0001-T1-r1.md") == {"name": "P0001-T1-r1.md", "text": "# 審查\n必改：無"}
  assert board_api.reviewText(approvedProject, "../config.json") is None
  assert board_api.reviewText(approvedProject, "P0001-T1-r1.txt") is None
  assert board_api.reviewText(approvedProject, "P0001-T1-r2.md") is None


def test_agentLogViewUsesLastJob(approvedProject):
  appendEvent(approvedProject, "taskStarted", plan="P0001", task="T1", agent="writer", roundNumber=1,
              data={"mode": "write"})
  appendEvent(approvedProject, "taskWritten", plan="P0001", task="T1", agent="writer", roundNumber=1,
              data={"commit": "abc", "mode": "write"})
  writeLog(approvedProject, "P0001-T1-r1-write-20261026-100000.log", claudeLog)
  view = board_api.agentLogView(approvedProject, "writer")
  assert (view["plan"], view["task"], view["round"], view["kind"]) == ("P0001", "T1", 1, "write")
  assert view["running"] is False and view["file"] == "P0001-T1-r1-write-20261026-100000.log"
  assert [message["text"] for message in view["messages"]] == ["先看 calc.py", "新增 calc.py"]
  assert view["tools"][0]["target"] == "calc.py"
  assert view["context"] == {"tokens": 34168, "window": 200000}
  assert board_api.agentLogView(approvedProject, "reviewer") == {"agent": "reviewer", "file": None}
  assert board_api.agentLogView(approvedProject, "writer-sub9") is None


def test_taskDiffViewBranchPendingMergedAndMissing(approvedProject):
  board_git.ensurePlanWorktree(approvedProject, "P0001", board_git.headCommit(approvedProject))
  taskPath = board_git.ensureTaskWorktree(approvedProject, "P0001", "T1")
  (taskPath / "說明.txt").write_text("第一版\n", encoding="utf-8")
  board_git.commitAll(taskPath, "T1 第一版")
  view = board_api.taskDiffView(approvedProject, "P0001", "T1")
  assert view["base"] == "branch" and "說明.txt" in view["stat"] and "+第一版" in view["diff"]
  assert view["pending"] == "" and view["message"] == ""
  appendEvent(approvedProject, "taskStarted", plan="P0001", task="T1", agent="writer", roundNumber=2,
              data={"mode": "write"})
  (taskPath / "說明.txt").write_text("第二版\n", encoding="utf-8")
  pending = board_api.taskDiffView(approvedProject, "P0001", "T1")["pending"]
  assert "說明.txt" in pending and "+第二版" in pending
  merged, commit, _ = board_git.mergeTask(approvedProject, "P0001", "T1", "合併 T1")
  assert merged
  appendEvent(approvedProject, "taskMerged", plan="P0001", task="T1", roundNumber=2, data={"commit": commit})
  view = board_api.taskDiffView(approvedProject, "P0001", "T1")
  assert view["base"] == "merge" and "+第一版" in view["diff"]
  assert board_api.taskDiffView(approvedProject, "P0001", "T2")["message"] == "找不到這個任務的分支（可能已 cleanup）"
  assert board_api.taskDiffView(approvedProject, "P0001", "T9") is None


def test_swappedRolesUseEachCliFormat(approvedProject):
  writeConfig(approvedProject, writer={"cli": "codex", "model": "gpt-6.1-sol"}, reviewer={"cli": "claude", "model": "opus"})
  appendEvent(approvedProject, "taskStarted", plan="P0001", task="T1", agent="writer", roundNumber=1,
              data={"mode": "write", "cli": "codex"})
  writeLog(approvedProject, "P0001-T1-r1-write-20261026-100000.log", codexLog)
  agents = agentsById(board_api.stateView(approvedProject))
  assert (agents["writer"]["cli"], agents["writer"]["model"]) == ("codex", "gpt-6.1-sol")
  assert agents["writer"]["action"] == "回覆寫程式結果"
  assert agents["writer"]["context"] == {"tokens": 18094, "window": 475000}
  assert (agents["reviewer"]["cli"], agents["reviewer"]["model"]) == ("claude", "opus")
  # 改回預設設定後，舊的 log 仍照事件記下的 CLI 解析
  writeConfig(approvedProject)
  assert board_api.agentLogView(approvedProject, "writer")["messages"] == [{"text": "{\"verdict\":\"pass\"}"}]


def test_configProblemsBecomeWarnings(approvedProject):
  writeConfig(approvedProject, claudeCommand=["claude"])
  assert board_api.stateView(approvedProject)["warnings"] == [
    "claudeCommand 已經不用了，請改寫成 writer.command（見 README 的「換模型」）"]
