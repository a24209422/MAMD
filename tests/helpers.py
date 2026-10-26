testConfig = {"maxRounds": 3, "maxParallel": 3}


def makeEvent(eventType, plan=None, task=None, roundNumber=None, agent=None, **data):
  return {"time": "2026-10-25T10:00:00+08:00", "plan": plan, "task": task, "type": eventType,
          "agent": agent, "round": roundNumber, "data": data}


def approvedEvent(planIdValue, tasks):
  return makeEvent("planApproved", plan=planIdValue, baseCommit="abc123", tasks=[
    {"id": taskId, "text": f"{taskId} 的內容", "dependsOn": list(dependsOn), "requirementIds": ["R1"]}
    for taskId, dependsOn in tasks
  ])


def makePlans(*planIds, supersedes=None):
  supersedes = supersedes or {}
  return {planIdValue: {"title": f"{planIdValue} 標題",
                        "meta": {"id": planIdValue, "supersedes": supersedes.get(planIdValue, "")}}
          for planIdValue in planIds}


import json
import sys
import time
from pathlib import Path

fakesDir = Path(__file__).resolve().parent / "fakes"


def fakeRole(cli, **extra):
  return {"cli": cli, "command": [sys.executable, str(fakesDir / f"fake_{cli}.py")], **extra}


def fakeConfig(**overrides):
  config = {"writer": fakeRole("claude"), "reviewer": fakeRole("codex"),
            "pollSeconds": 0.05, "timeoutMinutes": 1, "maxParallel": 3, "maxRounds": 3, "testCommand": ""}
  config.update(overrides)
  return config


def runUntil(conductor, predicate, timeout=60):
  deadline = time.time() + timeout
  state = conductor.currentState()[1]
  while time.time() < deadline:
    conductor.tick()
    state = conductor.currentState()[1]
    if predicate(state):
      return state
    time.sleep(0.05)
  summary = {key: (task["status"], task["round"], task["needs"]) for key, task in state["tasks"].items()}
  raise AssertionError(f"等不到預期狀態：{summary}")


def readCalls(scenarioDir, kind, planIdValue="P0001", taskId="T1"):
  paths = scenarioDir.glob(f"call-{kind}-{planIdValue}-{taskId}-*.json")
  ordered = sorted(paths, key=lambda path: int(path.stem.rsplit("-", 1)[1]))
  return [json.loads(path.read_text(encoding="utf-8")) for path in ordered]
