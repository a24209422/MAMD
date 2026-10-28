"""agent-board 指令列：寫 plan 的 agent（Codex 或 Claude）用的 submit／status，以及你操作用的指令。

用法：python board.py [--project 專案資料夾] <指令> ...
"""
import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import board_git
from board_core import (PlanStatus, TaskStatus, appendEvent, boardDir, deriveState, loadConfig, loadPlans,
                        planTasks, readEvents, taskKey, testConfigProblems)
from board_index import buildIndex
from board_prompts import tddCrapDir
from board_stats import approvedPlanIds, boardStats, planStats, taskRounds

humanOnlyCommands = {"approve", "reject", "abandon", "decide", "stop", "resume", "cleanup"}
# board_api 要從暫停事件的原因分辨是不是你按的 stop，兩邊共用同一個字串
stopReason = "你執行了 stop"
boardPyPath = Path(__file__).resolve()
snippetPath = boardPyPath.parent / "docs" / "AGENTS-snippet.md"
installSkillPath = boardPyPath.parent / "install_skill.py"
tddPackages = ["pytest", "coverage", "radon"]
agentsStartMarker = "<!-- agent-board:start（board.py init 產生，重跑 init 會整段換成最新版） -->"
agentsEndMarker = "<!-- agent-board:end -->"
requiredIgnores = [".board/", "plans/_review/"]
planSkillPath = Path(".claude") / "skills" / "agent-board-plan" / "SKILL.md"
planSkillMarker = "<!-- agent-board:skill（board.py init 產生，重跑 init 會整份換成最新版） -->"
# disable-model-invocation：只有使用者打 /agent-board-plan 才會用，寫程式的 Claude 不會自己叫出來
planSkillFrontmatter = ("---\nname: agent-board-plan\n"
                        "description: 照 agent-board 的流程把需求寫成 plan 並送出，只寫 plan、不寫程式\n"
                        "disable-model-invocation: true\n---")


def hasWindowsConsole(stream):
  import ctypes
  import msvcrt
  from ctypes import wintypes
  try:
    handle = msvcrt.get_osfhandle(stream.fileno())
  except (AttributeError, OSError, ValueError):
    return False
  mode = wintypes.DWORD()
  return ctypes.windll.kernel32.GetConsoleMode(wintypes.HANDLE(handle), ctypes.byref(mode)) != 0


def isHuman():
  # agent 執行指令時 stdin／stdout 都是管線而不是終端機，用這點擋掉 agent 自己核准
  if os.name == "nt":
    # Windows 上 NUL 裝置的 isatty() 也會回傳 True，`<NUL >NUL` 就能繞過；只有真正的主控台 GetConsoleMode 才會成功
    return hasWindowsConsole(sys.stdin) and hasWindowsConsole(sys.stdout)
  return sys.stdin.isatty() and sys.stdout.isatty()


def currentState(projectDir, loadErrors=None):
  plans = loadPlans(projectDir, loadErrors)
  return plans, deriveState(readEvents(projectDir), plans, loadConfig(projectDir))


def printLoadWarnings(loadErrors):
  for message in loadErrors:
    print("警告｜" + message)


def planBlockers(plan):
  graph = plan["graph"]
  if graph is None:
    return ["找不到追溯矩陣表格"]
  blockers = list(graph.problems["red"])
  if not plan.get("hasTitle"):
    # 沒有標題時，進度和總覽頁只能顯示檔名，看不出這份 plan 在做什麼
    blockers.append("缺少標題：在表格前面加一行「# 標題」")
  blockers += [f"任務「{task['text']}」沒有編號，無法排程" for task in graph.tasks.values() if not task["id"]]
  if not graph.tasks:
    blockers.append("沒有任何任務")
  return blockers


def approveWarnings(projectDir, plan):
  graph = plan["graph"]
  warnings = [f"橘｜{message}" for message in graph.problems["orange"]]
  warnings += [f"黃｜{message}" for message in graph.problems["yellow"]]
  if board_git.hasTrackedChanges(projectDir):
    warnings.append("主資料夾有還沒 commit 的改動，這些改動不會進 plan 分支")
  return warnings


def cmdSubmit(projectDir, planIdValue):
  loadErrors = []
  plans, state = currentState(projectDir, loadErrors)
  printLoadWarnings(loadErrors)
  plan = plans.get(planIdValue)
  if plan is None or plan["graph"] is None:
    print(f"找不到 {planIdValue} 的追溯矩陣（plans/ 裡要有 id 是它、而且有 RTM 表格的檔案）")
    return 1
  status = state["plans"][planIdValue]["status"]
  if status not in (PlanStatus.draft, PlanStatus.rejected, PlanStatus.pending):
    print(f"{planIdValue} 目前是「{status}」，不能送出")
    return 1
  problems = plan["graph"].problems
  blockers = planBlockers(plan)
  if blockers:
    print(f"{planIdValue} 有問題，沒有送出：")
    for message in blockers:
      print("  紅｜" + message)
    return 1
  appendEvent(projectDir, "planSubmitted", plan=planIdValue,
              data={"red": 0, "orange": len(problems["orange"]), "yellow": len(problems["yellow"])})
  print(f"{planIdValue} 已送出，等使用者核准。")
  for severity, name in (("orange", "橘"), ("yellow", "黃")):
    for message in problems[severity]:
      print(f"  {name}｜{message}")
  return 0


def cmdStatus(projectDir, planIdValue=None):
  loadErrors = []
  _, state = currentState(projectDir, loadErrors)
  printLoadWarnings(loadErrors)
  if state["paused"]:
    print(f"已暫停：{state['pauseReason']}")
  for planState in state["plans"].values():
    if planIdValue and planState["id"] != planIdValue:
      continue
    tasks = planTasks(state, planState["id"])
    line = f"{planState['id']} {planState['title']}｜{planState['status']}"
    if tasks:
      line += f"｜完成 {sum(task['status'] == TaskStatus.done for task in tasks)}/{len(tasks)}"
    if planState["status"] == PlanStatus.rejected and planState["rejectReason"]:
      line += f"｜退回原因：{planState['rejectReason']}"
    print(line)
    for task in tasks:
      who = {TaskStatus.writing: task["agent"], TaskStatus.reviewing: task["reviewAgent"]}.get(task["status"])
      detail = f"  {task['task']} {task['status']}（第 {task['round']}/{task['maxRounds']} 輪{'，' + who if who else ''}）"
      if task["needs"]:
        detail += f"｜{task['needs']['reason']}"
      print(detail)
  return 0


def durationText(seconds):
  hours, rest = divmod(int(seconds), 3600)
  minutes, seconds = divmod(rest, 60)
  parts = [f"{hours} 小時"] if hours else []
  parts += [f"{minutes} 分"] if hours or minutes else []
  return " ".join(parts + [f"{seconds} 秒"])


def elapsedText(duration):
  return durationText(duration["seconds"]) + ("（進行中）" if duration["inProgress"] else "")


def averageText(value):
  return "-" if value is None else f"{value:g}"


def decisionsText(decisions):
  counts = "、".join(f"{action} {decisions[action]}" for action in ("retry", "done", "abandon") if decisions[action])
  return f"{decisions['total']}（{counts}）" if counts else str(decisions["total"])


def modelsText(models):
  def combos(items):
    names = [" ".join(str(value) for value in combo.values() if value) for combo in items]
    return "、".join(name for name in names if name) or "-"
  return f"writer：{combos(models['writer'])}；reviewer：{combos(models['reviewer'])}"


def statsSummary(stats):
  tasks = stats["tasks"]
  return "｜".join([
    f"任務 {tasks['done']}／{tasks['abandoned']}／{tasks['total']}",
    f"平均輪數 {averageText(stats['averageRounds'])}",
    f"第一次審查就通過 {stats['firstReviewPassed']['passed']}/{stats['firstReviewPassed']['reviewed']}",
    f"寫完後被退回 {stats['returnedAfterWrite']['returned']}/{stats['returnedAfterWrite']['written']}",
    f"你的決定 {stats['decisions']['total']}",
    f"耗時 {elapsedText(stats['duration'])}",
  ])


def statsDetail(stats):
  tasks, reviews, tests = stats["tasks"], stats["reviews"], stats["tests"]
  return [
    f"任務：完成 {tasks['done']}／放棄 {tasks['abandoned']}／全部 {tasks['total']}",
    f"平均輪數：{averageText(stats['averageRounds'])}",
    f"寫完次數：{stats['written']}",
    f"審查：{reviews['total']} 次（通過 {reviews['passed']}、不通過 {reviews['failed']}）",
    f"第一次審查就通過：{stats['firstReviewPassed']['passed']}/{stats['firstReviewPassed']['reviewed']}",
    f"測試：{tests['runs']} 次，失敗 {tests['failed']}，退回 {tests['returned']}",
    f"寫完後被退回：{stats['returnedAfterWrite']['returned']}/{stats['returnedAfterWrite']['written']}",
    f"你的決定：{decisionsText(stats['decisions'])}",
    f"還在等你：{stats['waiting']}",
    f"等你的時間：{durationText(stats['waitSeconds'])}",
    f"agent 失敗、合併衝突：{stats['agentFailed']}、{stats['conflicts']}",
    f"plan 退回：{stats['planRejected']}",
    f"耗時：{elapsedText(stats['duration'])}",
    f"模型：{modelsText(stats['models'])}",
  ]


def cmdStats(projectDir, planIdValue=None):
  loadErrors = []
  plans = loadPlans(projectDir, loadErrors)
  printLoadWarnings(loadErrors)
  events = readEvents(projectDir)
  state = deriveState(events, plans, loadConfig(projectDir))

  def heading(planId):
    planState = state["plans"].get(planId)
    return f"{planId} {planState['title']}｜{planState['status']}" if planState else planId

  if planIdValue:
    if planIdValue not in approvedPlanIds(events):
      print(f"{planIdValue} 還沒核准過，沒有成效可以算")
      return 1
    print(heading(planIdValue))
    for line in statsDetail(planStats(events, state, planIdValue)):
      print("  " + line)
    rounds = taskRounds([event for event in events if event.get("plan") == planIdValue])
    print("  每個任務的輪數：")
    for task in planTasks(state, planIdValue):
      roundNumber = rounds.get(taskKey(planIdValue, task["task"]))
      print(f"    {task['task']} {f'{roundNumber} 輪' if roundNumber else '還沒開始'}｜{task['status']}")
    return 0
  stats = boardStats(events, state)
  if not stats["plans"]:
    print("還沒有核准過的 plan")
    return 0
  for planId, planStatsValue in stats["plans"].items():
    print(f"{heading(planId)}｜{statsSummary(planStatsValue)}")
  print(f"總計（{len(stats['plans'])} 份）｜{statsSummary(stats['total'])}")
  return 0


def cmdApprove(projectDir, planIdValue, confirm=input):
  plans, state = currentState(projectDir)
  planState = state["plans"].get(planIdValue)
  if not planState or planState["status"] != PlanStatus.pending:
    print(f"{planIdValue} 不是「待核准」，不能核准")
    return 1
  # submit 之後 plan 檔仍可能被改；核准時要用和 submit 相同的標準再擋一次，否則沒有編號的任務會進到快照
  blockers = planBlockers(plans[planIdValue])
  if blockers:
    print(f"{planIdValue} 送出後又被改出問題，不能核准，請寫 plan 的 agent 修好再送出：")
    for message in blockers:
      print("  紅｜" + message)
    return 1
  graph = plans[planIdValue]["graph"]
  warnings = approveWarnings(projectDir, plans[planIdValue])
  if warnings:
    print("核准前請確認：")
    for warning in warnings:
      print("  " + warning)
    if confirm("確定核准？輸入 y 繼續：").strip().lower() != "y":
      print("已取消")
      return 1
  tasks = [{"id": task["id"], "text": task["text"], "dependsOn": task["dependsOn"],
            "requirementIds": task["requirementIds"]} for task in graph.tasks.values()]
  appendEvent(projectDir, "planApproved", plan=planIdValue,
              data={"baseCommit": board_git.headCommit(projectDir), "branch": board_git.planBranch(planIdValue),
                    "tasks": tasks})
  print(f"{planIdValue} 已核准，Alfred 會開始執行。")
  return 0


def cmdReject(projectDir, planIdValue, reason):
  _, state = currentState(projectDir)
  planState = state["plans"].get(planIdValue)
  if not planState or planState["status"] != PlanStatus.pending:
    print(f"{planIdValue} 不是「待核准」，不能退回")
    return 1
  appendEvent(projectDir, "planRejected", plan=planIdValue, data={"reason": reason})
  print(f"{planIdValue} 已退回。請寫 plan 的 agent（Codex 或 Claude）照原因修改後再送出。")
  return 0


def cmdAbandon(projectDir, planIdValue):
  _, state = currentState(projectDir)
  planState = state["plans"].get(planIdValue)
  if not planState or planState["status"] in (PlanStatus.completed, PlanStatus.abandoned):
    print(f"{planIdValue} 不存在或已經結束，不能放棄")
    return 1
  appendEvent(projectDir, "planAbandoned", plan=planIdValue)
  print(f"{planIdValue} 已放棄，進行中的 agent 會被結束。")
  return 0


def cmdDecide(projectDir, planIdValue, taskId, action, note=""):
  _, state = currentState(projectDir)
  taskState = state["tasks"].get(taskKey(planIdValue, taskId))
  if not taskState or taskState["status"] != TaskStatus.needsYou:
    print(f"{planIdValue} {taskId} 不是「等你處理」")
    return 1
  appendEvent(projectDir, "userDecision", plan=planIdValue, task=taskId, data={"action": action, "note": note})
  print(f"{planIdValue} {taskId}：已記錄「{action}」")
  return 0


def cmdStop(projectDir):
  _, state = currentState(projectDir)
  if state["paused"]:
    print(f"已經是暫停狀態：{state['pauseReason']}")
    return 1
  appendEvent(projectDir, "paused", data={"reason": stopReason})
  print("已暫停，Alfred 會結束所有 agent。用 resume 繼續。")
  return 0


def cmdResume(projectDir):
  _, state = currentState(projectDir)
  if not state["paused"]:
    print("目前沒有暫停")
    return 1
  appendEvent(projectDir, "resumed")
  print("已繼續。")
  return 0


def cmdCleanup(projectDir, planIdValue, confirm=input):
  _, state = currentState(projectDir)
  planState = state["plans"].get(planIdValue)
  if not planState or planState["status"] not in (PlanStatus.completed, PlanStatus.abandoned, PlanStatus.superseded):
    print(f"{planIdValue} 還沒結束，不能清理")
    return 1
  if confirm(f"會刪除 {planIdValue} 的所有 worktree 和 task/{planIdValue}-* 分支（plan/{planIdValue} 保留），輸入 y 繼續：").strip().lower() != "y":
    print("已取消")
    return 1
  for name in board_git.removePlanWorktrees(projectDir, planIdValue):
    print("已刪除 " + name)
  return 0


def cmdIndex(projectDir):
  indexPath, plans, warnings = buildIndex(projectDir)
  print(f"總覽：{indexPath}（{len(plans)} 份）")
  for warning in warnings:
    print("  警告｜" + warning)
  return 0


def readKeepingNewlines(path):
  # newline="" 讀寫才不會改掉原檔的換行；使用者的檔案是 CRLF 就維持 CRLF
  if not path.exists():
    return ""
  with path.open(encoding="utf-8", newline="") as handle:
    return handle.read()


def writeKeepingNewlines(path, text):
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("w", encoding="utf-8", newline="") as handle:
    handle.write(text)


def lineEnding(text):
  return "\r\n" if "\r\n" in text else "\n"


def withTrailingNewline(text, eol):
  return text if not text or text.endswith(("\n", "\r")) else text + eol


def agentsMarkers(lines):
  starts = [index for index, line in enumerate(lines) if line.startswith("<!-- agent-board:start")]
  ends = [index for index, line in enumerate(lines) if line.strip() == agentsEndMarker]
  return starts, ends


def agentsProblem(text):
  lines = text.splitlines()
  starts, ends = agentsMarkers(lines)
  if starts or ends:
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
      return "AGENTS.md 的 agent-board 標記不完整或重複，請整理成一組 start、end 再執行 init"
    return None
  for lineNumber, line in enumerate(lines, start=1):
    if line.startswith("## agent-board 流程"):
      # 手動貼上的規則沒有結尾標記，判斷不出範圍，硬換可能刪到使用者自己寫的內容
      return f"AGENTS.md 第 {lineNumber} 行已經有手動貼上的 agent-board 規則，請先刪掉那一段再執行 init"
  return None


def renderedSnippet():
  # 每個人 clone 的位置不同，寫 plan 的 agent 要照規則裡的路徑執行 submit，寫入時換成這份 board.py 的實際位置
  boardPy = f'"{boardPyPath}"' if " " in str(boardPyPath) else str(boardPyPath)
  return snippetPath.read_text(encoding="utf-8").strip("\n").replace("{{boardPy}}", boardPy)


def updatedAgents(text):
  eol = lineEnding(text)
  block = eol.join([agentsStartMarker, *renderedSnippet().split("\n"), agentsEndMarker]) + eol
  lines = text.splitlines(keepends=True)
  starts, ends = agentsMarkers([line.rstrip("\r\n") for line in lines])
  if starts:
    return "".join(lines[:starts[0]]) + block + "".join(lines[ends[0] + 1:])
  if not text:
    return block
  return withTrailingNewline(text, eol) + eol + block


def planSkillText():
  return "\n".join([planSkillFrontmatter, "", planSkillMarker, "", "使用者在 /agent-board-plan 後面寫的就是需求。", "",
                    renderedSnippet()]) + "\n"


def planSkillProblem(text):
  # 同名的 skill 沒有 init 的標記，可能是使用者自己寫的，不覆蓋
  if text and planSkillMarker not in text:
    return f"{planSkillPath.as_posix()} 已經存在但不是 init 產生的，請先改名或刪掉再執行 init"
  return None


def updatedGitignore(text):
  eol = lineEnding(text)
  # 「/.board」「.board」和「.board/」對 git 都是同一個資料夾，不重複加
  present = {line.strip().strip("/") for line in text.splitlines()}
  missing = [entry for entry in requiredIgnores if entry.strip("/") not in present]
  if not missing:
    return text, []
  return withTrailingNewline(text, eol) + "".join(entry + eol for entry in missing), missing


def qualityCommand(sources, testsDir):
  def quoted(text):
    return f'"{text}"' if " " in text else text
  script = quoted((tddCrapDir / "scripts" / "quality.py").as_posix())
  sourceArgs = " ".join(f"--source {quoted(source)}" for source in sources)
  return f"python {script} {sourceArgs} -- {quoted(testsDir)}"


def missingPackages():
  return [name for name in tddPackages if importlib.util.find_spec(name) is None]


def readRawConfig(projectDir):
  # 不用 loadConfig：它會補上所有預設值，寫回去時會把預設值也寫進使用者的設定檔
  path = boardDir(projectDir) / "config.json"
  if not path.exists():
    return {}, None
  try:
    config = json.loads(path.read_text(encoding="utf-8"))
  except (UnicodeDecodeError, json.JSONDecodeError):
    config = None
  if not isinstance(config, dict):
    return None, ".board/config.json 不是正確的 JSON 物件，請先修正"
  return config, None


def tddProblems(projectDir, sources, testsDir, config, command):
  problems = []
  if not (tddCrapDir / "scripts" / "quality.py").is_file():
    problems.append(f"找不到 tdd-crap skill：{tddCrapDir}，先執行 python {installSkillPath} 安裝")
  for folder in [*sources, testsDir]:
    if not (projectDir / folder).is_dir():
      problems.append(f"找不到資料夾 {folder}：用 --source、--tests 指定原始碼和測試的位置")
  existing = (config or {}).get("testCommand")
  if existing and existing != command:
    problems.append(f".board/config.json 已經有 testCommand：{existing}\n"
                    f"    init 要設成：{command}\n"
                    "    要改用 tdd-crap 的話，先刪掉原本的 testCommand 再執行 init")
  return problems


def cmdInit(projectDir, tdd=False, sources=None, testsDir=None, testCommand=None, skipTests=False):
  projectDir = Path(projectDir)
  sources, testsDir = list(sources or ["src"]), testsDir or "tests"
  if not projectDir.is_dir():
    print(f"找不到專案資料夾：{projectDir}")
    return 1
  problems = []
  if board_git.runGit(projectDir, "rev-parse", "--is-inside-work-tree", check=False) != "true":
    problems.append("不是 git repo：先執行 git init，並 commit 一次")
  elif not board_git.runGit(projectDir, "rev-parse", "--verify", "-q", "HEAD", check=False):
    problems.append("還沒有任何 commit：先 commit 一次，Alfred 要從目前的 commit 開 plan 分支")
  agentsPath, ignorePath = projectDir / "AGENTS.md", projectDir / ".gitignore"
  skillPath = projectDir / planSkillPath
  agentsText, skillText = readKeepingNewlines(agentsPath), readKeepingNewlines(skillPath)
  problems += [problem for problem in [agentsProblem(agentsText), planSkillProblem(skillText)] if problem]
  config, configProblem = readRawConfig(projectDir)
  problems += [configProblem] if configProblem else []
  command = None
  explicitChoices = int(bool(tdd)) + int(testCommand is not None) + int(bool(skipTests))
  if explicitChoices > 1:
    problems.append("--tdd、--test-command、--skip-tests 只能選一個")
  newConfig = dict(config or {})
  if tdd:
    command = qualityCommand(sources, testsDir)
    if not configProblem:
      problems += tddProblems(projectDir, sources, testsDir, config, command)
    newConfig.update(tddCrap=True, testCommand=command)
    newConfig.pop("skipTests", None)
  elif testCommand is not None:
    if not isinstance(testCommand, str) or not testCommand.strip():
      problems.append("--test-command 要提供非空白的測試指令")
    elif newConfig.get("tddCrap") and testCommand.strip() != newConfig.get("testCommand"):
      problems.append("目前已啟用 TDD；一般 --test-command 不會覆蓋 TDD 的品質檢查指令")
    else:
      newConfig["testCommand"] = testCommand.strip()
      newConfig.pop("skipTests", None)
  elif skipTests:
    if newConfig.get("tddCrap"):
      problems.append("目前已啟用 TDD，不能用 --skip-tests 略過測試")
    else:
      newConfig["testCommand"] = ""
      newConfig["skipTests"] = True
  if not configProblem:
    problems += testConfigProblems(newConfig, requireChoice=True)
  # 全部檢查完才動手，有問題時專案維持原狀，修好再跑一次就好
  if problems:
    print("沒有修改任何檔案：")
    for problem in problems:
      print("  " + problem)
    return 1
  changed = []
  newAgents = updatedAgents(agentsText)
  if newAgents == agentsText:
    print("AGENTS.md：已是最新")
  else:
    if not agentsText:
      action = "已建立"
    elif agentsMarkers(agentsText.splitlines())[0]:
      action = "已換成最新版的規則"
    else:
      action = "已在最後加上規則"
    writeKeepingNewlines(agentsPath, newAgents)
    changed.append("AGENTS.md")
    print(f"AGENTS.md：{action}")
  newIgnore, missing = updatedGitignore(readKeepingNewlines(ignorePath))
  if missing:
    writeKeepingNewlines(ignorePath, newIgnore)
    changed.append(".gitignore")
    print(f".gitignore：補上 {'、'.join(missing)}")
  else:
    print(".gitignore：已是最新")
  newSkill, skillName = planSkillText(), planSkillPath.as_posix()
  if newSkill == skillText:
    print(f"{skillName}：已是最新")
  else:
    writeKeepingNewlines(skillPath, newSkill)
    changed.append(skillName)
    print(f"{skillName}：{'已換成最新版' if skillText else '已建立'}"
          "（在 Claude Code 打 /agent-board-plan 加上需求，就由 Claude 寫 plan）")
  if newConfig != config:
    writeKeepingNewlines(boardDir(projectDir) / "config.json", json.dumps(newConfig, ensure_ascii=False, indent=2) + "\n")
  if tdd:
    print(f".board/config.json：已開啟 tddCrap，品質檢查指令是 {command}")
    absent = missingPackages()
    if absent:
      # 檢查的是執行 init 的這個 Python；conductor 跑品質檢查時用的是 PATH 上的 python
      print(f"提醒：{sys.executable} 沒有安裝 {'、'.join(absent)}，請執行：")
      print(f"  python -m pip install -r {tddCrapDir / 'requirements.txt'}")
  elif newConfig.get("skipTests"):
    print("測試設定：已明確略過自動測試，這不代表測試通過")
  else:
    print(f"測試指令：{newConfig['testCommand']}")
  print("若 Alfred 已在執行，測試設定變更後請重啟 Alfred 才會生效")
  print("提醒：測試產物（例如 __pycache__/、.pytest_cache/）請自己加進 .gitignore，否則會被 commit 進任務分支")
  if changed:
    print("接下來請 commit：")
    print(f"  git add {' '.join(changed)}")
    print('  git commit -m "啟用 agent-board"')
  else:
    print("AGENTS.md、.gitignore、agent-board-plan skill 都沒有改動，不用 commit")
  return 0


def buildParser():
  parser = argparse.ArgumentParser(prog="board.py", description="agent-board 指令")
  parser.add_argument("--project", default=".", help="專案資料夾，預設是目前資料夾")
  sub = parser.add_subparsers(dest="command", required=True)
  sub.add_parser("submit").add_argument("plan")
  sub.add_parser("status").add_argument("plan", nargs="?")
  sub.add_parser("stats").add_argument("plan", nargs="?")
  sub.add_parser("approve").add_argument("plan")
  reject = sub.add_parser("reject")
  reject.add_argument("plan")
  reject.add_argument("--reason", required=True)
  sub.add_parser("abandon").add_argument("plan")
  decide = sub.add_parser("decide")
  decide.add_argument("plan")
  decide.add_argument("task")
  decide.add_argument("action", choices=["retry", "done", "abandon"])
  decide.add_argument("--note", default="")
  sub.add_parser("stop")
  sub.add_parser("resume")
  sub.add_parser("cleanup").add_argument("plan")
  sub.add_parser("index")
  init = sub.add_parser("init")
  testing = init.add_mutually_exclusive_group()
  testing.add_argument("--tdd", action="store_true", help="寫程式的 agent 照 tdd-crap 寫程式（只適用 Python 專案）")
  testing.add_argument("--test-command", help="專案的測試指令，例如 python -m pytest -q；明確指定時更新測試設定")
  testing.add_argument("--skip-tests", action="store_true", help="明確略過自動測試，不宣稱測試通過")
  init.add_argument("--source", action="append", help="原始碼資料夾，可以重複，預設 src")
  init.add_argument("--tests", help="測試資料夾，預設 tests")
  return parser


def main(argv=None):
  if not sys.stdout.isatty():
    # 被 agent 用管線讀取時，Windows 預設編碼不是 UTF-8，中文會變亂碼
    sys.stdout.reconfigure(encoding="utf-8")
  args = buildParser().parse_args(argv)
  projectDir = Path(args.project).resolve()
  if args.command in humanOnlyCommands and not isHuman():
    print(f"「{args.command}」只能由使用者在終端機執行，agent 不能執行。")
    return 2
  if args.command == "init" and not args.tdd and (args.source or args.tests):
    print("--source、--tests 要和 --tdd 一起用")
    return 2
  commands = {
    "submit": lambda: cmdSubmit(projectDir, args.plan),
    "status": lambda: cmdStatus(projectDir, args.plan),
    "stats": lambda: cmdStats(projectDir, args.plan),
    "approve": lambda: cmdApprove(projectDir, args.plan),
    "reject": lambda: cmdReject(projectDir, args.plan, args.reason),
    "abandon": lambda: cmdAbandon(projectDir, args.plan),
    "decide": lambda: cmdDecide(projectDir, args.plan, args.task, args.action, args.note),
    "stop": lambda: cmdStop(projectDir),
    "resume": lambda: cmdResume(projectDir),
    "cleanup": lambda: cmdCleanup(projectDir, args.plan),
    "index": lambda: cmdIndex(projectDir),
    "init": lambda: cmdInit(projectDir, args.tdd, args.source, args.tests, args.test_command, args.skip_tests),
  }
  return commands[args.command]()


if __name__ == "__main__":
  sys.exit(main())
