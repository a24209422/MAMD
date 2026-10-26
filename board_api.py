"""把事件紀錄、狀態、plan、log、git 整理成儀表板 API 的回傳資料。不碰 HTTP，可以直接測試。"""
import re
from pathlib import Path

import board_git
import board_logs
from board import approveWarnings, planBlockers, stopReason
from board_core import (PlanStatus, TaskStatus, agentLanes, boardDir, configProblems, defaultContextWindows,
                        deriveState, loadConfig, loadPlans, planTasks, readEvents, readUserConfig, roleNames,
                        roleSpec, taskKey, tasksWithStatus)
from conductor import formatEvent, processAlive, readLockPid
from rtm_graph import asList, formatList, parseArea

recentEventCount = 50
userEventTypes = {"planApproved", "planRejected", "planAbandoned", "userDecision", "resumed"}
emptyProblems = {"red": [], "orange": [], "yellow": []}
reviewNamePattern = re.compile(r"^[A-Za-z]+\d+-[A-Za-z]+\d+-r\d+\.md$")
maxDiffChars = 200 * 1024


def loadSnapshot(projectDir):
  warnings = []
  plans = loadPlans(projectDir, warnings)
  events = readEvents(projectDir)
  config = loadConfig(projectDir)
  # 設定寫錯時 conductor 會拒絕啟動，儀表板也要說出原因
  warnings += configProblems(readUserConfig(projectDir))
  return {"plans": plans, "events": events, "config": config, "warnings": warnings,
          "state": deriveState(events, plans, config)}


def conductorStatus(projectDir):
  pid = readLockPid(projectDir)
  running = bool(pid) and processAlive(pid)
  return {"running": running, "pid": pid if running else None}


def eventWho(event):
  if event.get("agent"):
    return event["agent"]
  data = event.get("data") or {}
  if event["type"] in userEventTypes or (event["type"] == "paused" and data.get("reason") == stopReason):
    return "你"
  return "系統"


def eventView(event):
  # 沿用 conductor 終端機印的說明；時間另外給，前端自己排版
  timeText = (event.get("time") or "")[11:19]
  text = formatEvent(event)
  if timeText and text.startswith(timeText):
    text = text[len(timeText):]
  return {"time": event.get("time"), "type": event["type"], "plan": event.get("plan"), "task": event.get("task"),
          "agent": event.get("agent"), "round": event.get("round"), "who": eventWho(event), "text": text.strip()}


def agentRolesOf(config):
  return {agentId: role for role in roleNames for agentId in agentLanes(role, config)}


def busyTaskOf(state, agentId):
  for taskState in state["tasks"].values():
    if taskState["status"] == TaskStatus.writing and taskState["agent"] == agentId:
      return taskState, "write"
    if taskState["status"] == TaskStatus.reviewing and taskState["reviewAgent"] == agentId:
      return taskState, "review"
  return None, None


def runningLaunch(taskState, kind, spec):
  # 忙碌中的 agent 照它啟動時記下的 CLI、模型；中途換了設定也不會用錯格式解析 log。舊事件沒記的照設定
  if kind == "review":
    cli, model = taskState["reviewCli"], taskState["reviewModel"]
  else:
    cli, model = taskState["sessionCli"], taskState["sessionModel"]
  return (cli, model) if cli else (spec["cli"], spec["model"])


def contextWindowFor(spec, cli):
  # 設定的 contextWindow 是給設定裡的 CLI 用的；跑的是另一個 CLI 時用那個 CLI 的預設值
  return spec["contextWindow"] if cli == spec["cli"] else defaultContextWindows.get(cli)


def roleView(config, role):
  spec = roleSpec(config, role)
  return {"cli": spec["cli"], "model": spec["model"], "customCommand": bool(spec["command"])}


def agentView(projectDir, snapshot, agentId, role):
  state, config = snapshot["state"], snapshot["config"]
  spec = roleSpec(config, role)
  view = {"id": agentId, "role": role, "cli": spec["cli"], "model": spec["model"],
          "status": "閒置", "plan": None, "task": None, "round": None,
          "action": None, "lastActivity": None, "context": None}
  if state["paused"]:
    view["status"] = "已暫停"
    return view
  taskState, kind = busyTaskOf(state, agentId)
  if taskState is None:
    return view
  cli, model = runningLaunch(taskState, kind, spec)
  view.update(plan=taskState["plan"], task=taskState["task"], round=taskState["round"], cli=cli, model=model,
              context={"tokens": None, "window": contextWindowFor(spec, cli)})
  view["status"] = "審查中" if kind == "review" else ("解衝突中" if taskState["resolving"] else "寫程式中")
  logPath = board_logs.findLog(boardDir(projectDir) / "logs", taskState["plan"], taskState["task"],
                               taskState["round"], kind)
  if logPath:
    summary = board_logs.logSummary(logPath, cli, kind)
    view.update(action=summary["action"], lastActivity=summary["lastActivity"])
    view["context"]["tokens"] = summary["tokens"]
  return view


def taskView(projectDir, taskState):
  return {"plan": taskState["plan"], "task": taskState["task"], "title": taskState["text"],
          "status": taskState["status"], "round": taskState["round"], "maxRounds": taskState["maxRounds"],
          "mode": "resolve" if taskState["status"] == TaskStatus.writing and taskState["resolving"] else "write",
          "agent": taskState["agent"], "reviewAgent": taskState["reviewAgent"],
          "dependsOn": list(taskState["dependsOn"]),
          "reviews": [Path(notes).name for notes in taskState["reviews"] if notes],
          "lastTest": taskState["lastTest"], "needs": taskState["needs"],
          "worktree": str(board_git.taskWorktreePath(projectDir, taskState["plan"], taskState["task"]))}


def lastEventTime(events, planIdValue, taskId):
  for event in reversed(events):
    if event.get("plan") == planIdValue and event.get("task") == taskId:
      return event.get("time")
  return None


def needsYouView(projectDir, snapshot):
  items = []
  for taskState in tasksWithStatus(snapshot["state"], TaskStatus.needsYou):
    needs = taskState["needs"] or {}
    planIdValue, taskId = taskState["plan"], taskState["task"]
    items.append({"plan": planIdValue, "task": taskId, "title": taskState["text"], "kind": needs.get("kind"),
                  "reason": needs.get("reason", ""), "conflicts": list(needs.get("conflicts", [])),
                  "since": lastEventTime(snapshot["events"], planIdValue, taskId),
                  "taskWorktree": str(board_git.taskWorktreePath(projectDir, planIdValue, taskId)),
                  "planWorktree": str(board_git.planWorktreePath(projectDir, planIdValue))})
  return items


def planSummary(snapshot, planIdValue):
  plan, planState = snapshot["plans"][planIdValue], snapshot["state"]["plans"][planIdValue]
  meta = plan["meta"]
  problems = plan["graph"].problems if plan["graph"] else emptyProblems
  tasks = planTasks(snapshot["state"], planIdValue)
  return {"id": planIdValue, "title": planState["title"], "status": planState["status"],
          "area": parseArea(meta.get("area")), "created": formatList(meta.get("created")),
          "branch": formatList(meta.get("branch")), "supersedes": asList(meta.get("supersedes")),
          "supersededBy": [otherId for otherId, other in snapshot["plans"].items()
                           if planIdValue in asList(other["meta"].get("supersedes"))],
          "source": f"plans/{plan['path'].name}", "rejectReason": planState["rejectReason"],
          "problems": {severity: len(messages) for severity, messages in problems.items()},
          "tasks": {"total": len(tasks), "done": sum(task["status"] == TaskStatus.done for task in tasks)}}


def stateView(projectDir):
  snapshot = loadSnapshot(projectDir)
  state = snapshot["state"]
  events = [event for event in snapshot["events"] if event.get("type")]
  agents = agentRolesOf(snapshot["config"])
  return {
    "project": str(Path(projectDir).resolve()),
    "paused": state["paused"],
    "pauseReason": state["pauseReason"],
    "conductor": conductorStatus(projectDir),
    "warnings": snapshot["warnings"],
    "roles": {role: roleView(snapshot["config"], role) for role in roleNames},
    "needsYou": needsYouView(projectDir, snapshot),
    "plans": [planSummary(snapshot, planIdValue) for planIdValue in snapshot["plans"]],
    "tasks": [taskView(projectDir, taskState) for taskState in state["tasks"].values()],
    "agents": [agentView(projectDir, snapshot, agentId, role) for agentId, role in agents.items()],
    "recentEvents": [eventView(event) for event in reversed(events[-recentEventCount:])],
  }


def graphView(graph):
  nodes = []
  for node in graph.nodes.values():
    item = {"key": node["key"], "kind": node["kind"], "id": node.get("id"), "text": node.get("text") or "",
            "note": node.get("note") or "", "class": graph.nodeClass(node)}
    if node["kind"] == "gap":
      item["severity"] = node["severity"]
    nodes.append(item)
  edges = [{"from": fromKey, "to": toKey, "style": style} for (fromKey, toKey), style in graph.edges.items()]
  return {"nodes": nodes, "edges": edges}


def planView(projectDir, planIdValue):
  snapshot = loadSnapshot(projectDir)
  plan = snapshot["plans"].get(planIdValue)
  if plan is None:
    return None
  view = planSummary(snapshot, planIdValue)
  graph = plan["graph"]
  problems = graph.problems if graph else emptyProblems
  view.update(meta=plan["meta"], graph=graphView(graph) if graph else None,
              problems={severity: list(messages) for severity, messages in problems.items()},
              blockers=planBlockers(plan),
              approveWarnings=approveWarnings(projectDir, plan) if graph and view["status"] == PlanStatus.pending else [])
  return view


def reviewText(projectDir, name):
  # 檔名格式固定，擋掉 ../ 之類讀到 reviews 以外的檔案
  if not reviewNamePattern.match(name):
    return None
  path = boardDir(projectDir) / "reviews" / name
  if not path.is_file():
    return None
  return {"name": name, "text": path.read_text(encoding="utf-8", errors="replace")}


def lastJobOf(events, agentId):
  for event in reversed(events):
    if event.get("agent") == agentId and event.get("type") in ("taskStarted", "reviewStarted"):
      kind = "write" if event["type"] == "taskStarted" else "review"
      return event.get("plan"), event.get("task"), event.get("round"), kind, (event.get("data") or {}).get("cli")
  return None


def agentLogView(projectDir, agentId):
  snapshot = loadSnapshot(projectDir)
  role = agentRolesOf(snapshot["config"]).get(agentId)
  if role is None:
    return None
  spec = roleSpec(snapshot["config"], role)
  job = lastJobOf(snapshot["events"], agentId)
  if job is None:
    return {"agent": agentId, "file": None}
  planIdValue, taskId, roundNumber, kind, cli = job
  busyTask, _ = busyTaskOf(snapshot["state"], agentId)
  view = {"agent": agentId, "plan": planIdValue, "task": taskId, "round": roundNumber, "kind": kind,
          "file": None, "running": busyTask is not None, "messages": [], "tools": [], "context": None,
          "truncated": False}
  logPath = board_logs.findLog(boardDir(projectDir) / "logs", planIdValue, taskId, roundNumber, kind)
  if logPath is None:
    return view
  # log 是當時的 CLI 寫的；之後改了設定，也要照當時的格式解析
  cli = cli or spec["cli"]
  detail = board_logs.logDetail(logPath, cli, kind)
  view.update(file=logPath.name, messages=detail["messages"], tools=detail["tools"], truncated=detail["truncated"],
              context={"tokens": detail["tokens"], "window": contextWindowFor(spec, cli)})
  return view


def mergedCommitOf(events, planIdValue, taskId):
  for event in reversed(events):
    if event.get("type") == "taskMerged" and event.get("plan") == planIdValue and event.get("task") == taskId:
      return (event.get("data") or {}).get("commit")
  return None


def taskDiffView(projectDir, planIdValue, taskId):
  snapshot = loadSnapshot(projectDir)
  taskState = snapshot["state"]["tasks"].get(taskKey(planIdValue, taskId))
  if taskState is None:
    return None
  view = {"base": None, "stat": "", "diff": "", "pending": "", "truncated": False, "message": ""}
  mergedCommit = mergedCommitOf(snapshot["events"], planIdValue, taskId)
  if mergedCommit:
    # 合併後任務分支已經在 plan 分支裡，直接比對分支會是空的，改看合併 commit 帶進來的改動
    view["base"], rangeArgs = "merge", [f"{mergedCommit}^1", mergedCommit]
  elif board_git.branchExists(projectDir, board_git.taskBranch(planIdValue, taskId)):
    view["base"] = "branch"
    rangeArgs = [f"{board_git.planBranch(planIdValue)}...{board_git.taskBranch(planIdValue, taskId)}"]
  else:
    view["message"] = "找不到這個任務的分支（可能已 cleanup）"
    return view
  try:
    view["stat"] = board_git.runGit(projectDir, "-c", "core.quotePath=false", "diff", "--stat", *rangeArgs)
    diff = board_git.runGit(projectDir, "-c", "core.quotePath=false", "diff", *rangeArgs)
  except board_git.GitError as error:
    view["message"] = f"取不到 diff：{error}"
    return view
  view["truncated"] = len(diff) > maxDiffChars
  view["diff"] = diff[:maxDiffChars]
  worktree = board_git.taskWorktreePath(projectDir, planIdValue, taskId)
  if taskState["status"] == TaskStatus.writing and worktree.exists():
    # 寫程式中的改動還沒 commit，分支上看不到，另外列出 worktree 的狀態
    status = board_git.runGit(worktree, "-c", "core.quotePath=false", "status", "--short", check=False)
    pending = board_git.runGit(worktree, "-c", "core.quotePath=false", "diff", "HEAD", check=False)
    view["pending"] = "\n\n".join(part for part in (status, pending) if part)[:maxDiffChars]
  return view
