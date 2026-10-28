"""把 plan 裡的 RTM 表格畫成「需求 → 做法 → 任務 → 驗證」線路圖，並標出斷線的地方。

用法：python rtm_graph.py <plan.md> [輸出.html]
資料夾總覽改用 board.py index。
"""
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# 用關鍵字比對表頭，表頭多寫說明（例如「做法（ADR）」）也認得
columnKeywords = [
  ("understanding", "理解"),
  ("rejected", "不採用"),
  ("requirement", "需求"),
  ("decision", "做法"),
  ("task", "任務"),
  ("verify", "驗證"),
  ("dependsOn", "前置"),
]
kindNames = {"requirement": "需求", "decision": "做法", "task": "任務"}
severityNames = [("red", "紅"), ("orange", "橘"), ("yellow", "黃")]
emptyMarkers = {"", "-", "—", "無", "（無）"}
# 編號後面常被寫成「T1：內容」，冒號只是分隔，不能算進內容
idPattern = re.compile(r"^([A-Za-z]+\d+)\s*[：:]?\s*(.*)$")
separatorPattern = re.compile(r"^\s*\|?\s*:?-{3,}")
mermaidUrl = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs"

classStyles = {
  "plan": "fill:#2f3b52,stroke:#2f3b52,color:#ffffff,font-weight:bold",
  "requirement": "fill:#eaf1fb,stroke:#4a6fa5,color:#1b2a41",
  "decision": "fill:#f1ecfa,stroke:#7a5ea8,color:#2b1d45",
  "task": "fill:#f2f2f2,stroke:#777777,color:#222222",
  "verify": "fill:#ffffff,stroke:#999999,color:#222222,stroke-dasharray:4 3",
  "missing": "fill:#ffd6d6,stroke:#c00000,color:#7a0000,stroke-width:2px",
  "extra": "fill:#ffe2b8,stroke:#d97000,color:#6b3800,stroke-width:2px",
  "incomplete": "fill:#fff3b0,stroke:#a68a00,color:#4d4000,stroke-width:2px",
}

pageTemplate = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  body { font-family: "Microsoft JhengHei", "Noto Sans TC", sans-serif; margin: 24px; color: #222; background: #fff; }
  a { color: #2f5fa8; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .meta { color: #666; font-size: 13px; margin-bottom: 16px; }
  .legend { display: flex; gap: 20px; flex-wrap: wrap; font-size: 14px; margin-bottom: 12px; }
  .chip { display: inline-block; width: 12px; height: 12px; border: 2px solid; border-radius: 3px; vertical-align: -2px; margin-right: 6px; }
  .problems { list-style: none; margin: 0 0 20px; padding: 0; font-size: 14px; }
  .problems li { padding: 5px 10px; margin-bottom: 4px; border-left: 4px solid; }
  .red { background: #ffd6d6; border-color: #c00000; }
  .orange { background: #ffe2b8; border-color: #d97000; }
  .yellow { background: #fff3b0; border-color: #a68a00; }
  .ok { background: #e3f4e4; border-color: #2e7d32; }
  #diagram { overflow-x: auto; border: 1px solid #ddd; border-radius: 6px; padding: 12px; }
  details { margin-top: 16px; font-size: 13px; }
  pre { background: #f6f6f6; padding: 12px; overflow-x: auto; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="meta">__META__</div>
<div class="legend">
  <span><i class="chip red"></i>紅：需求缺做法或任務（漏做）</span>
  <span><i class="chip orange"></i>橘：沒有對應需求（多做）</span>
  <span><i class="chip yellow"></i>黃：缺驗證或 AI 理解（資訊不完整）</span>
</div>
<ul class="problems">
__PROBLEMS__
</ul>
<div id="diagram">圖載入中…（需要網路連線載入 Mermaid）</div>
<details><summary>Mermaid 原始碼（可貼到 Obsidian 或 mermaid.live）</summary><pre>__MERMAID_SOURCE__</pre></details>
<script type="module">
  import mermaid from "__MERMAID_URL__";
  const diagramCode = __CODE__;
  const target = document.getElementById("diagram");
  try {
    mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
    const { svg } = await mermaid.render("rtmGraph", diagramCode);
    target.innerHTML = svg;
  } catch (error) {
    target.textContent = "畫圖失敗：" + error.message;
  }
</script>
</body>
</html>
"""


def splitRow(line):
  body = line.strip()
  if body.startswith("|"):
    body = body[1:]
  if body.endswith("|") and not body.endswith("\\|"):
    body = body[:-1]
  # 儲存格內容可以用 \| 寫直線，不當成欄位分隔
  return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", body)]


def isEmpty(cell):
  return cell.strip() in emptyMarkers


def cleanCell(cell):
  return "" if isEmpty(cell) else cell.strip()


def formatList(value):
  if isinstance(value, list):
    return "、".join(value)
  return value or ""


def asList(value):
  if not value:
    return []
  return value if isinstance(value, list) else [value]


def parseArea(value):
  # area 的每個值寫成「端/模組」，模組可省略；Codex 常寫全形符號，所以「／」也接受，只切第一個斜線
  areas = []
  for item in asList(value):
    parts = re.split(r"[/／]", str(item), maxsplit=1)
    side = parts[0].strip()
    if side:
      areas.append({"side": side, "module": parts[1].strip() if len(parts) > 1 else ""})
  return areas


def parseFrontMatter(lines):
  # 只處理 plan 檔頭用到的簡單寫法（key: value 和 [a, b] 清單），不必另外裝 YAML 套件
  if not lines or lines[0].strip() != "---":
    return {}, lines
  meta = {}
  for index in range(1, len(lines)):
    line = lines[index]
    if line.strip() == "---":
      return meta, lines[index + 1:]
    key, separator, value = line.partition(":")
    if not separator:
      continue
    value = re.sub(r"\s+#.*$", "", value).strip().strip("\"'")
    if value.startswith("[") and value.endswith("]"):
      value = [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]
    meta[key.strip()] = value
  # 沒有結尾的 ---，當作沒有檔頭，避免整份內容被吃掉
  return {}, lines


def findRtmTableRange(lines):
  # plan 裡可能還有其他表格，同時有「需求」「任務」欄的才是追溯矩陣；回傳表頭到最後一列的範圍（不含結尾）
  for index, line in enumerate(lines[:-1]):
    if not line.strip().startswith("|") or not separatorPattern.match(lines[index + 1]):
      continue
    header = splitRow(line)
    if any("需求" in cell for cell in header) and any("任務" in cell for cell in header):
      end = index + 2
      while end < len(lines) and lines[end].strip().startswith("|"):
        end += 1
      return index, end
  return None


def findRtmTable(lines):
  tableRange = findRtmTableRange(lines)
  if tableRange is None:
    return None, []
  start, end = tableRange
  return splitRow(lines[start]), [splitRow(rowLine) for rowLine in lines[start + 2:end]]


def extractExtraText(body):
  # 標題那行和追溯矩陣以外的文字（規則表、契約、對照值等），整份交給寫程式和審查的 agent
  lines = list(body)
  tableRange = findRtmTableRange(lines)
  if tableRange is not None:
    start, end = tableRange
    # 表格前後都是空行時只留一行，避免拿掉表格後中間空出一大段
    if 0 < start and end < len(lines) and not lines[start - 1].strip() and not lines[end].strip():
      end += 1
    del lines[start:end]
  headingIndex = next((index for index, line in enumerate(lines) if line.startswith("# ")), None)
  if headingIndex is not None:
    del lines[headingIndex]
  return "\n".join(lines).strip()


def mapColumns(header):
  columnIndex = {}
  for index, cell in enumerate(header):
    for field, keyword in columnKeywords:
      if keyword in cell and field not in columnIndex:
        columnIndex[field] = index
        break
  return columnIndex


def parseIdCell(cell):
  if isEmpty(cell):
    return None, ""
  match = idPattern.match(cell.strip())
  if match:
    return match.group(1).upper(), match.group(2).strip()
  return None, cell.strip()


def parseIdList(cell):
  # 前置欄可以用逗號、頓號或空白分隔多個編號，大小寫都接受
  return [match.upper() for match in re.findall(r"[A-Za-z]+\d+", cell)]


def findDependencyCycle(tasks):
  graph = {task["id"]: task["dependsOn"] for task in tasks.values() if task["id"]}
  visiting, visited = [], set()

  def visit(taskId):
    if taskId in visiting:
      return visiting[visiting.index(taskId):] + [taskId]
    if taskId in visited or taskId not in graph:
      return None
    visiting.append(taskId)
    for dependencyId in graph[taskId]:
      cycle = visit(dependencyId)
      if cycle:
        return cycle
    visiting.pop()
    visited.add(taskId)
    return None

  for taskId in graph:
    cycle = visit(taskId)
    if cycle:
      return cycle
  return None


def escapeLabel(text):
  # Mermaid 標籤裡的引號、角括號要換成它的跳脫寫法，否則會被當成語法或 HTML
  return text.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")


def mermaidId(key):
  # 加前綴並只留英數字，避免編號撞到 Mermaid 的保留字（例如 end）
  return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", key)


class RtmGraph:
  def __init__(self, rootLabel):
    self.nodes = {"root": {"key": "root", "kind": "plan", "text": rootLabel}}
    self.edges = {}
    self.problems = {severity: [] for severity, _ in severityNames}
    self.gapCount = 0
    self.tasks = {}
    self.rows = []

  def upsertNode(self, kind, nodeId, text, note, rowNumber, withRequirement):
    key = nodeId or f"{kind}-row{rowNumber}"
    node = self.nodes.get(key)
    if node is None:
      node = {"key": key, "kind": kind, "id": nodeId, "text": text, "note": note,
              "rows": [], "withRequirement": False}
      self.nodes[key] = node
    elif text and node["text"] and text != node["text"]:
      self.problems["yellow"].append(f"{nodeId} 在第 {node['rows'][0]} 列和第 {rowNumber} 列寫的內容不一樣")
    # 同一個編號出現在多列代表同一個節點（例如一條需求拆成兩個任務），內容只要在其中一列寫
    node["text"] = node["text"] or text
    node["note"] = node["note"] or note
    node["rows"].append(rowNumber)
    node["withRequirement"] = node["withRequirement"] or withRequirement
    if not nodeId:
      self.problems["yellow"].append(f"第 {rowNumber} 列的{kindNames[kind]}沒有編號")
    return key

  def addGap(self, severity, text):
    self.gapCount += 1
    key = f"gap{self.gapCount}"
    self.nodes[key] = {"key": key, "kind": "gap", "severity": severity, "text": text}
    return key

  def addVerify(self, rowNumber, text):
    key = f"verify-row{rowNumber}"
    self.nodes[key] = {"key": key, "kind": "verify", "text": text}
    return key

  def addRow(self, rowNumber, cells):
    reqId, reqText = parseIdCell(cells.get("requirement", ""))
    decId, decText = parseIdCell(cells.get("decision", ""))
    taskId, taskText = parseIdCell(cells.get("task", ""))
    understanding = cleanCell(cells.get("understanding", ""))
    rejected = cleanCell(cells.get("rejected", ""))
    verify = cleanCell(cells.get("verify", ""))
    hasRequirement = bool(reqId or reqText)
    where = f"（第 {rowNumber} 列）"

    # 根節點像 git 的 main，需求是從它分出去的分支；沒有需求的改動像沒開分支直接 commit 到 main，用虛線從根節點拉下來
    if hasRequirement:
      headKey = self.upsertNode("requirement", reqId, reqText, understanding, rowNumber, True)
      self.edges[("root", headKey)] = "solid"
      subject = reqId or "需求"
    else:
      headKey = self.addGap("orange", "沒有對應需求")
      self.edges[("root", headKey)] = "extra"
      subject = " → ".join(nodeId for nodeId in (decId, taskId) if nodeId) or "這列"
      self.problems["orange"].append(f"{subject} 沒有對應需求{where}")
    chain = [headKey]

    decKey = self.upsertNode("decision", decId, decText, rejected, rowNumber, hasRequirement) if (decId or decText) else None
    taskKey = self.upsertNode("task", taskId, taskText, "", rowNumber, hasRequirement) if (taskId or taskText) else None
    self.rows.append({
      "rowNumber": rowNumber,
      "requirement": cleanCell(cells.get("requirement", "")),
      "understanding": understanding,
      "decision": cleanCell(cells.get("decision", "")),
      "rejected": rejected,
      "task": cleanCell(cells.get("task", "")),
      "dependsOn": cleanCell(cells.get("dependsOn", "")),
      "verify": verify,
    })
    if taskKey:
      task = self.tasks.setdefault(taskKey, {"id": taskId, "text": "", "dependsOn": [],
                                             "rowNumbers": [], "requirementIds": []})
      task["text"] = task["text"] or taskText
      task["rowNumbers"].append(rowNumber)
      if reqId and reqId not in task["requirementIds"]:
        task["requirementIds"].append(reqId)
      for dependencyId in parseIdList(cells.get("dependsOn", "")):
        if dependencyId not in task["dependsOn"]:
          task["dependsOn"].append(dependencyId)

    segments = [("做法", decKey), ("任務", taskKey)]
    missingNames = [name for name, key in segments if key is None]
    if missingNames:
      self.problems["red"].append(f"{subject} 缺{'、'.join(missingNames)}{where}")
    # 連續缺的段落合成一個色塊，一條線上只會出現一個紅塊，比較好讀
    pendingGaps = []
    for name, key in segments:
      if key is None:
        pendingGaps.append(name)
        continue
      if pendingGaps:
        chain.append(self.addGap("red", "缺：" + "、".join(pendingGaps)))
        pendingGaps = []
      chain.append(key)
    if pendingGaps:
      chain.append(self.addGap("red", "缺：" + "、".join(pendingGaps)))

    if verify:
      chain.append(self.addVerify(rowNumber, verify))
    elif taskKey:
      chain.append(self.addGap("yellow", "缺：驗證"))
      self.problems["yellow"].append(f"{taskId or '任務'} 缺驗證{where}")

    for fromKey, toKey in zip(chain, chain[1:]):
      self.edges[(fromKey, toKey)] = "solid"

  def finish(self):
    # AI 理解可能只寫在需求第一次出現的那列，所以等全部列讀完才檢查
    for node in self.nodes.values():
      if node["kind"] == "requirement" and not node["note"]:
        self.problems["yellow"].append(f"{node['id'] or node['text']} 缺 AI 理解（第 {node['rows'][0]} 列）")
    knownTaskIds = {task["id"] for task in self.tasks.values() if task["id"]}
    for task in self.tasks.values():
      for dependencyId in task["dependsOn"]:
        if dependencyId not in knownTaskIds:
          self.problems["red"].append(f"{task['id'] or '任務'} 的前置 {dependencyId} 不存在（第 {task['rowNumbers'][0]} 列）")
    cycle = findDependencyCycle(self.tasks)
    if cycle:
      self.problems["red"].append("任務互相等待：" + " → ".join(cycle))

  def nodeClass(self, node):
    if node["kind"] == "gap":
      return {"red": "missing", "orange": "extra", "yellow": "incomplete"}[node["severity"]]
    if node["kind"] in ("decision", "task") and not node["withRequirement"]:
      return "extra"
    if node["kind"] == "requirement" and not node["note"]:
      return "incomplete"
    return node["kind"]

  def nodeLabel(self, node):
    if node["kind"] in ("gap", "plan"):
      return escapeLabel(node["text"])
    if node["kind"] == "verify":
      return escapeLabel("驗證：" + node["text"])
    title = " ".join(part for part in (node["id"], node["text"]) if part)
    lines = [escapeLabel(title)]
    if node["kind"] == "requirement":
      lines.append(escapeLabel("理解：" + (node["note"] or "（未寫）")))
    elif node["kind"] == "decision" and node["note"]:
      lines.append(escapeLabel("不採用：" + node["note"]))
    return "<br>".join(lines)

  def toMermaid(self):
    lines = ["flowchart TD"]
    for node in self.nodes.values():
      lines.append(f'  {mermaidId(node["key"])}["{self.nodeLabel(node)}"]')
    extraLinkIndexes = []
    for index, ((fromKey, toKey), style) in enumerate(self.edges.items()):
      arrow = "-.->" if style == "extra" else "-->"
      lines.append(f"  {mermaidId(fromKey)} {arrow} {mermaidId(toKey)}")
      if style == "extra":
        extraLinkIndexes.append(str(index))
    # Mermaid 的 linkStyle 只能用連線的順序編號指定顏色
    if extraLinkIndexes:
      lines.append(f"  linkStyle {','.join(extraLinkIndexes)} stroke:#d97000,stroke-width:2px")
    for className, style in classStyles.items():
      lines.append(f"  classDef {className} {style}")
    groups = {}
    for node in self.nodes.values():
      groups.setdefault(self.nodeClass(node), []).append(mermaidId(node["key"]))
    for className, nodeIds in groups.items():
      lines.append(f"  class {','.join(nodeIds)} {className}")
    return "\n".join(lines)


def planId(plan):
  return formatList(plan["meta"].get("id")) or plan["path"].stem


def analyzePlan(planPath):
  lines = planPath.read_text(encoding="utf-8-sig").splitlines()
  meta, body = parseFrontMatter(lines)
  heading = next((line[2:].strip() for line in body if line.startswith("# ")), None)
  plan = {"path": planPath, "meta": meta, "title": heading or planPath.stem, "hasTitle": bool(heading),
          "graph": None, "rowCount": 0, "extraText": extractExtraText(body)}
  header, rows = findRtmTable(body)
  if header is None:
    return plan

  columnIndex = mapColumns(header)
  tableRows = []
  for rowNumber, row in enumerate(rows, start=1):
    cells = {field: row[index] if index < len(row) else "" for field, index in columnIndex.items()}
    if all(isEmpty(cell) for cell in cells.values()):
      continue
    tableRows.append((rowNumber, cells))
  # 沒有對應需求的列排到最後，橘色那條固定在最右邊，不管 AI 寫在表格第幾列，審查時都知道去哪找
  tableRows.sort(key=lambda item: isEmpty(item[1].get("requirement", "")))

  rootLabel = " ".join(part for part in (formatList(meta.get("id")), plan["title"]) if part)
  graph = RtmGraph(rootLabel)
  for rowNumber, cells in tableRows:
    graph.addRow(rowNumber, cells)
  graph.finish()
  plan["graph"] = graph
  plan["rowCount"] = len(rows)
  return plan


def renderPlanHtml(plan, backHref=None):
  graph = plan["graph"]
  meta = plan["meta"]
  problemItems = []
  for severity, name in severityNames:
    for message in graph.problems[severity]:
      problemItems.append(f'<li class="{severity}">{name}｜{html.escape(message)}</li>')
  if not problemItems:
    problemItems.append('<li class="ok">沒有發現斷線</li>')

  metaParts = [part for part in (formatList(meta.get("id")), formatList(meta.get("status"))) if part]
  if meta.get("area"):
    metaParts.append("模組：" + formatList(meta["area"]))
  if meta.get("branch"):
    metaParts.append("分支：" + formatList(meta["branch"]))
  metaParts.append("來源：" + str(plan["path"]))
  metaParts.append("產生時間：" + datetime.now().strftime("%Y-%m-%d %H:%M"))
  metaHtml = "　".join(html.escape(part) for part in metaParts)
  if backHref:
    metaHtml = f'<a href="{html.escape(backHref)}">← 回總覽</a>　' + metaHtml

  mermaidCode = graph.toMermaid()
  # 圖的原始碼用 JSON 字串交給 mermaid.render，避免 <br> 先被瀏覽器當 HTML 解析；
  # </ 要拆開，否則會提早結束 <script>
  codeJson = json.dumps(mermaidCode, ensure_ascii=False).replace("</", "<\\/")
  return (pageTemplate
    .replace("__TITLE__", html.escape(plan["title"]))
    .replace("__META__", metaHtml)
    .replace("__PROBLEMS__", "\n".join(problemItems))
    .replace("__MERMAID_SOURCE__", html.escape(mermaidCode))
    .replace("__MERMAID_URL__", mermaidUrl)
    .replace("__CODE__", codeJson))


def printSummary(planPath, rowCount, problems, outputPath):
  # 只印摘要給 AI 看，不印整張圖，省 context
  print(f"RTM 檢查：{planPath.name}（{rowCount} 列）")
  if not any(problems.values()):
    print("沒有發現斷線")
  for severity, name in severityNames:
    if problems[severity]:
      print(f"{name} {len(problems[severity])}：" + "；".join(problems[severity]))
  print(f"圖：{outputPath}")


def buildSingle(planPath, outputArg):
  plan = analyzePlan(planPath)
  if plan["graph"] is None:
    print(f"{planPath.name} 裡找不到追溯矩陣（要有同時含「需求」「任務」欄的表格）")
    return 1
  outputPath = Path(outputArg).resolve() if outputArg else planPath.with_suffix(".html")
  outputPath.write_text(renderPlanHtml(plan), encoding="utf-8")
  printSummary(planPath, plan["rowCount"], plan["graph"].problems, outputPath)
  return 0


def main():
  if len(sys.argv) < 2:
    print("用法：python rtm_graph.py <plan.md> [輸出.html]")
    return 1
  targetPath = Path(sys.argv[1]).resolve()
  if targetPath.is_dir():
    print("資料夾總覽改用：python board.py --project <專案資料夾> index")
    return 1
  return buildSingle(targetPath, sys.argv[2] if len(sys.argv) > 2 else None)


if __name__ == "__main__":
  sys.exit(main())
