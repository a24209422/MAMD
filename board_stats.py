"""從事件紀錄和推導出的狀態算出每份 plan 的成效；只讀事件，不讀 log 或 git。"""
from datetime import datetime

from board_core import TaskStatus, planTasks, taskKey

decisionActions = ("retry", "done", "abandon")


def parseTime(text):
  return datetime.fromisoformat(text)


def approvedPlanIds(events):
  planIds = []
  for event in events:
    if event.get("type") == "planApproved" and event.get("plan") not in planIds:
      planIds.append(event.get("plan"))
  return planIds


def countType(events, eventType):
  return sum(1 for event in events if event.get("type") == eventType)


def taskRounds(events):
  rounds = {}
  for event in events:
    if event.get("task") and isinstance(event.get("round"), int):
      key = taskKey(event.get("plan"), event["task"])
      rounds[key] = max(rounds.get(key, 0), event["round"])
  return rounds


def firstReviewResults(events):
  results = {}
  for event in events:
    if event.get("type") in ("reviewPassed", "reviewFailed"):
      results.setdefault(taskKey(event.get("plan"), event.get("task")), event["type"])
  return results


def eventsOfType(events, eventType):
  return [event for event in events if event.get("type") == eventType]


def failedRunCount(runs):
  return sum(1 for event in runs if not event["data"].get("passed"))


def testRunReturnCount(runs):
  # 舊的 testRun 沒有 verdict，只算次數和失敗，不算退回
  return sum(1 for event in runs if event["data"].get("verdict") == "newFailure")


def mergeRunReturnCount(event):
  outcomes = event["data"].get("outcomes") or {}
  return sum(1 for outcome in outcomes.values() if outcome.get("result") in ("newFailure", "stillFailing"))


def testStats(events):
  runs = eventsOfType(events, "testRun")
  mergeRuns = eventsOfType(events, "mergeTestRun")
  # 逐一測沿用同一個 commit 的結果時沒有真的跑，不算次數；一次涵蓋好幾個任務也只算一次
  ranAfterMerge = [event for event in mergeRuns if not event["data"].get("reused")]
  mergeReturned = sum(mergeRunReturnCount(event) for event in mergeRuns)
  return {"runs": len(runs) + len(ranAfterMerge),
          "failed": failedRunCount(runs + ranAfterMerge),
          "returned": testRunReturnCount(runs) + mergeReturned,
          "reused": len(mergeRuns) - len(ranAfterMerge)}


def decisionStats(events):
  decisions = [event for event in events if event.get("type") == "userDecision"]
  counts = {action: 0 for action in decisionActions}
  for event in decisions:
    action = event["data"].get("action")
    if action in counts:
      counts[action] += 1
  return {"total": len(decisions), **counts}


def waitSeconds(events):
  total = 0
  lastTimes = {}
  for event in events:
    if not event.get("task"):
      continue
    key = taskKey(event.get("plan"), event["task"])
    if event.get("type") == "userDecision" and key in lastTimes:
      total += (parseTime(event["time"]) - lastTimes[key]).total_seconds()
    lastTimes[key] = parseTime(event["time"])
  return int(total)


def roleModels(events, eventType, defaultCli=None):
  models = []
  for event in events:
    if event.get("type") != eventType:
      continue
    data = event.get("data") or {}
    # 和 board_core 一樣：寫程式的舊事件沒記 CLI 就是 Claude
    combo = {"cli": data.get("cli", defaultCli), "model": data.get("model"),
             "reasoningEffort": data.get("reasoningEffort")}
    if combo not in models:
      models.append(combo)
  return models


def durationStats(events, now):
  approved = next((event for event in events if event.get("type") == "planApproved"), None)
  if not approved:
    return {"seconds": 0, "inProgress": False}
  completed = next((event for event in events if event.get("type") == "planCompleted"), None)
  # 指標定義：沒有 planCompleted 就算到現在並標進行中，放棄或被取代的 plan 也一樣
  end = parseTime(completed["time"]) if completed else (now or datetime.now().astimezone())
  return {"seconds": int((end - parseTime(approved["time"])).total_seconds()), "inProgress": not completed}


def countStatus(tasks, status):
  return sum(1 for task in tasks if task["status"] == status)


def averageRounds(events, tasks):
  rounds = taskRounds(events)
  taskRoundValues = [rounds[key] for key in (taskKey(task["plan"], task["task"]) for task in tasks) if key in rounds]
  return round(sum(taskRoundValues) / len(taskRoundValues), 2) if taskRoundValues else None


def firstReviewStats(events):
  firstResults = firstReviewResults(events)
  return {"passed": sum(1 for result in firstResults.values() if result == "reviewPassed"),
          "reviewed": len(firstResults)}


def totalDuration(durations):
  return {"seconds": sum(item["seconds"] for item in durations),
          "inProgress": any(item["inProgress"] for item in durations)}


def collectStats(events, tasks, durations):
  passed = countType(events, "reviewPassed")
  failed = countType(events, "reviewFailed")
  written = countType(events, "taskWritten")
  tests = testStats(events)
  return {
    "tasks": {"done": countStatus(tasks, TaskStatus.done),
              "abandoned": countStatus(tasks, TaskStatus.abandoned),
              "total": len(tasks)},
    "averageRounds": averageRounds(events, tasks),
    "written": written,
    "reviews": {"total": passed + failed, "passed": passed, "failed": failed},
    "firstReviewPassed": firstReviewStats(events),
    "tests": tests,
    "returnedAfterWrite": {"returned": failed + tests["returned"], "written": written},
    "decisions": decisionStats(events),
    "waiting": countStatus(tasks, TaskStatus.needsYou),
    "waitSeconds": waitSeconds(events),
    "agentFailed": countType(events, "agentFailed"),
    "conflicts": countType(events, "taskConflicted"),
    "planRejected": countType(events, "planRejected"),
    "duration": totalDuration(durations),
    "models": {"writer": roleModels(events, "taskStarted", "claude"), "reviewer": roleModels(events, "reviewStarted")},
  }


def planStats(events, state, planIdValue, now=None):
  planEvents = [event for event in events if event.get("plan") == planIdValue]
  duration = durationStats(planEvents, now)
  return collectStats(planEvents, planTasks(state, planIdValue), [duration])


def boardStats(events, state, now=None):
  """回傳核准過的每份 plan 的指標，加上全部 plan 的總計。"""
  planIds = approvedPlanIds(events)
  plans = {planIdValue: planStats(events, state, planIdValue, now) for planIdValue in planIds}
  planEvents = [event for event in events if event.get("plan") in planIds]
  tasks = [task for planIdValue in planIds for task in planTasks(state, planIdValue)]
  total = collectStats(planEvents, tasks, [stats["duration"] for stats in plans.values()])
  return {"plans": plans, "total": total}
