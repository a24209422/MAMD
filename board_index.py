"""plans/_review/ 總覽頁：每份 plan 的線路圖，加上依事件推導出的狀態分組。"""
import html
from datetime import datetime
from pathlib import Path

from board_core import PlanStatus, deriveState, loadConfig, readEvents
from rtm_graph import analyzePlan, asList, formatList, planId, renderPlanHtml, severityNames

reviewDirName = "_review"
# 前四種還要你處理或還在跑，總覽頁直接展開；後三種已經結束，預設收合
openStatuses = [PlanStatus.pending, PlanStatus.rejected, PlanStatus.active, PlanStatus.draft]
closedStatuses = [PlanStatus.completed, PlanStatus.abandoned, PlanStatus.superseded]


indexTemplate = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plan 總覽</title>
<style>
  body { font-family: "Microsoft JhengHei", "Noto Sans TC", sans-serif; margin: 24px; color: #222; background: #fff; }
  a { color: #2f5fa8; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2, summary { font-size: 16px; margin: 24px 0 8px; }
  summary { cursor: pointer; }
  .meta { color: #666; font-size: 13px; margin-bottom: 16px; }
  #search { width: 100%; max-width: 420px; box-sizing: border-box; padding: 8px 10px; font-size: 14px; border: 1px solid #bbb; border-radius: 6px; margin-bottom: 12px; }
  .warnings { list-style: none; margin: 0 0 16px; padding: 0; font-size: 14px; }
  .warnings li { padding: 5px 10px; margin-bottom: 4px; border-left: 4px solid #a68a00; background: #fff3b0; }
  .tableWrap { overflow-x: auto; }
  /* 每個狀態是一張獨立的表格，固定欄寬才會上下對齊 */
  table { border-collapse: collapse; width: 100%; min-width: 900px; table-layout: fixed; font-size: 14px; }
  th, td { border-bottom: 1px solid #e3e3e3; padding: 6px 8px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }
  th { background: #f6f6f6; font-weight: normal; color: #555; }
  .colId { width: 70px; }
  .colArea { width: 110px; }
  .colBranch { width: 190px; }
  .colCreated { width: 100px; }
  .colCount { width: 40px; text-align: center; }
  .colNote { width: 150px; }
  td.count { text-align: center; }
  td.note { color: #666; }
  .red { background: #ffd6d6; color: #7a0000; }
  .orange { background: #ffe2b8; color: #6b3800; }
  .yellow { background: #fff3b0; color: #4d4000; }
  .zero { color: #bbb; }
</style>
</head>
<body>
<h1>Plan 總覽</h1>
<div class="meta">來源：__SOURCE__　共 __TOTAL__ 份　產生時間：__TIME__</div>
<input id="search" type="search" placeholder="搜尋 ID、標題、模組、分支、狀態…">
__WARNINGS__
__SECTIONS__
<script>
  const searchBox = document.getElementById("search");
  searchBox.addEventListener("input", () => {
    const keyword = searchBox.value.trim().toLowerCase();
    document.querySelectorAll("tr[data-search]").forEach((row) => {
      row.hidden = keyword !== "" && !row.dataset.search.includes(keyword);
    });
    // 搜尋時把收合的區塊打開，符合的 plan 才不會藏在「完成」裡看不到
    if (keyword) {
      document.querySelectorAll("details").forEach((section) => { section.open = true; });
    }
  });
</script>
</body>
</html>
"""


def renderPlanTable(plans, supersededBy, statusOf):
  rows = []
  for plan in plans:
    meta = plan["meta"]
    currentId = planId(plan)
    notes = [f"取代 {oldId}" for oldId in asList(meta.get("supersedes"))]
    if currentId in supersededBy:
      notes.append(f"被 {supersededBy[currentId]} 取代")
    if plan["graph"] is None:
      notes.append("找不到追溯矩陣")

    title = html.escape(plan["title"])
    titleCell = f'<a href="{html.escape(plan["href"])}">{title}</a>' if plan.get("href") else title
    countCells = []
    for severity, _ in severityNames:
      count = len(plan["graph"].problems[severity]) if plan["graph"] else 0
      countCells.append(f'<td class="count {severity if count else "zero"}">{count}</td>')
    searchText = " ".join([currentId, plan["title"], formatList(meta.get("area")),
                           formatList(meta.get("branch")), statusOf(plan)]).lower()
    rows.append(
      f'<tr data-search="{html.escape(searchText)}">'
      f"<td>{html.escape(currentId)}</td>"
      f'<td class="title">{titleCell}</td>'
      f"<td>{html.escape(formatList(meta.get('area')))}</td>"
      f"<td>{html.escape(formatList(meta.get('branch')))}</td>"
      f"<td>{html.escape(formatList(meta.get('created')))}</td>"
      + "".join(countCells)
      + f'<td class="note">{html.escape("；".join(notes))}</td></tr>'
    )
  header = ('<tr><th class="colId">ID</th><th>標題</th><th class="colArea">模組</th><th class="colBranch">分支</th>'
            '<th class="colCreated">建立</th><th class="colCount">紅</th><th class="colCount">橘</th>'
            '<th class="colCount">黃</th><th class="colNote">備註</th></tr>')
  return f'<div class="tableWrap"><table>{header}{"".join(rows)}</table></div>'


def checkPlans(plans):
  warnings = []
  idOwners = {}
  for plan in plans:
    fileName = plan["path"].name
    if not plan["meta"].get("id"):
      warnings.append(f"{fileName} 檔頭沒有 id")
    if plan["graph"] is None:
      warnings.append(f"{fileName} 找不到追溯矩陣")
    currentId = planId(plan)
    if currentId in idOwners:
      warnings.append(f"{idOwners[currentId]} 和 {fileName} 的 id 都是 {currentId}")
    idOwners.setdefault(currentId, fileName)
  for plan in plans:
    for oldId in asList(plan["meta"].get("supersedes")):
      if oldId not in idOwners:
        warnings.append(f"{planId(plan)} 寫了取代 {oldId}，但找不到 {oldId}")
  return warnings


def renderIndexHtml(plansDir, plans, warnings, statusOf):
  supersededBy = {}
  for plan in plans:
    for oldId in asList(plan["meta"].get("supersedes")):
      supersededBy[oldId] = planId(plan)
  groups = {}
  for plan in plans:
    groups.setdefault(statusOf(plan), []).append(plan)

  sections = []
  for status in openStatuses + closedStatuses:
    groupPlans = groups.get(status)
    if not groupPlans:
      continue
    # 新的 plan 排上面，最近在做的最好找
    groupPlans = sorted(groupPlans, key=lambda plan: plan["path"].name, reverse=True)
    heading = f"{status}（{len(groupPlans)}）"
    table = renderPlanTable(groupPlans, supersededBy, statusOf)
    if status in closedStatuses:
      sections.append(f"<details><summary>{heading}</summary>{table}</details>")
    else:
      sections.append(f"<h2>{heading}</h2>{table}")

  warningsHtml = ""
  if warnings:
    warningsHtml = '<ul class="warnings">' + "".join(f"<li>{html.escape(warning)}</li>" for warning in warnings) + "</ul>"
  return (indexTemplate
    .replace("__SOURCE__", html.escape(str(plansDir)))
    .replace("__TOTAL__", str(len(plans)))
    .replace("__TIME__", datetime.now().strftime("%Y-%m-%d %H:%M"))
    .replace("__WARNINGS__", warningsHtml)
    .replace("__SECTIONS__", "\n".join(sections)))


def buildIndex(projectDir):
  plansDir = Path(projectDir) / "plans"
  outputDir = plansDir / reviewDirName
  outputDir.mkdir(parents=True, exist_ok=True)
  # 直接掃檔案而不是用 loadPlans，重複的 id 才不會被字典合併掉而查不出來
  plans = [analyzePlan(path) for path in sorted(plansDir.glob("*.md"))]
  state = deriveState(readEvents(projectDir), {planId(plan): plan for plan in plans}, loadConfig(projectDir))

  def statusOf(plan):
    return state["plans"][planId(plan)]["status"]

  for plan in plans:
    if plan["graph"] is not None:
      outputPath = outputDir / f"{plan['path'].stem}.html"
      outputPath.write_text(renderPlanHtml(plan, backHref="index.html"), encoding="utf-8")
      plan["href"] = outputPath.name
  warnings = checkPlans(plans)
  indexPath = outputDir / "index.html"
  indexPath.write_text(renderIndexHtml(plansDir, plans, warnings, statusOf), encoding="utf-8")
  return indexPath, plans, warnings
