from rtm_graph import analyzePlan, buildSingle, formatList, main, parseArea


def test_dependsOnParsedIntoTasks(writePlan):
  plan = analyzePlan(writePlan("0001-demo.md", [
    "| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |",
    "| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |",
  ]))
  tasks = plan["graph"].tasks
  assert tasks["T1"]["text"] == "任務一"
  assert tasks["T2"]["dependsOn"] == ["T1"]
  assert tasks["T2"]["requirementIds"] == ["R1"]
  assert plan["graph"].problems["red"] == []


def test_multipleDependenciesWithMixedSeparators(writePlan):
  plan = analyzePlan(writePlan("0001-demo.md", [
    "| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |",
    "| R1 | | D1 | | T2 任務二 | | 驗證二 |",
    "| R1 | | D1 | | T3 任務三 | t1、T2 | 驗證三 |",
  ]))
  assert plan["graph"].tasks["T3"]["dependsOn"] == ["T1", "T2"]


def test_unknownDependencyIsRed(writePlan):
  plan = analyzePlan(writePlan("0001-demo.md", [
    "| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | T9 | 驗證一 |",
  ]))
  assert any("T9 不存在" in message for message in plan["graph"].problems["red"])


def test_dependencyCycleIsRed(writePlan):
  plan = analyzePlan(writePlan("0001-demo.md", [
    "| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | T2 | 驗證一 |",
    "| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |",
  ]))
  assert any("互相等待" in message for message in plan["graph"].problems["red"])


def test_rowsKeepAllColumns(writePlan):
  plan = analyzePlan(writePlan("0001-demo.md", [
    "| R1 需求一 | 理解一 | D1 做法一 | 逐張呼叫：太慢 | T1 任務一 | | 驗證一 |",
    "| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |",
  ]))
  rows = plan["graph"].rows
  assert rows[0]["rejected"] == "逐張呼叫：太慢"
  assert rows[0]["verify"] == "驗證一"
  assert rows[1]["dependsOn"] == "T1"
  assert rows[1]["understanding"] == ""


def test_colonAfterIdIsNotPartOfText(writePlan):
  # Codex 常寫成「T1：內容」，冒號不能變成任務內容的一部分
  plan = analyzePlan(writePlan("0001-demo.md", [
    "| R1：需求一 | 理解一 | D1: 做法一 | - | T1：任務一 | | 驗證一 |",
    "| R1 | | D1 | | T2 ： 任務二 | T1 | 驗證二 |",
  ]))
  graph = plan["graph"]
  assert graph.tasks["T1"]["text"] == "任務一"
  assert graph.tasks["T2"]["text"] == "任務二"
  assert graph.nodes["R1"]["text"] == "需求一"
  assert graph.nodes["D1"]["text"] == "做法一"


def test_planRemembersWhetherItHasTitle(writePlan, tmp_path):
  assert analyzePlan(writePlan("0001-demo.md", ["| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |"]))["hasTitle"] is True
  untitled = tmp_path / "0002-untitled.md"
  untitled.write_text("---\nid: P0002\n---\n\n| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |\n"
                      "|---|---|---|---|---|---|---|\n| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |\n",
                      encoding="utf-8")
  plan = analyzePlan(untitled)
  assert plan["hasTitle"] is False
  assert plan["title"] == "0002-untitled"


rtmTable = ("| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |\n|---|---|---|---|---|---|---|\n"
            "| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |\n"
            "| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |\n")


def writeRawPlan(tmp_path, body):
  path = tmp_path / "0001-demo.md"
  path.write_text("---\nid: P0001\n---\n\n" + body, encoding="utf-8")
  return path


def test_extraTextKeepsTextBeforeAndAfterTable(tmp_path):
  plan = analyzePlan(writeRawPlan(tmp_path, "# 測試 plan\n\n表格前的說明\n\n" + rtmTable + "\n## 契約\n\n- 回傳值是 0\n"))
  assert plan["extraText"] == "表格前的說明\n\n## 契約\n\n- 回傳值是 0"
  assert "測試 plan" not in plan["extraText"]
  assert "需求" not in plan["extraText"]
  assert "|" not in plan["extraText"]
  assert plan["graph"].tasks["T2"]["dependsOn"] == ["T1"]


def test_extraTextEmptyWhenOnlyTable(tmp_path):
  assert analyzePlan(writeRawPlan(tmp_path, "# 測試 plan\n\n" + rtmTable))["extraText"] == ""


def test_extraTextKeepsOtherTables(tmp_path):
  ruleTable = "| 輸入 | 輸出 |\n|---|---|\n| a | 1 |\n| b | 2 |"
  plan = analyzePlan(writeRawPlan(tmp_path, "# 測試 plan\n\n" + rtmTable + "\n## 規則表\n\n" + ruleTable + "\n"))
  assert plan["extraText"] == "## 規則表\n\n" + ruleTable
  assert plan["rowCount"] == 2


def test_parseAreaSplitsSideAndModule():
  assert parseArea(["前端/縮圖顯示", "後端"]) == [
    {"side": "前端", "module": "縮圖顯示"}, {"side": "後端", "module": ""}]


def test_parseAreaAcceptsFullWidthSlashAndSplitsOnce():
  assert parseArea(["後端／縮圖/背景"]) == [{"side": "後端", "module": "縮圖/背景"}]
  assert parseArea([" 前端 / 上傳 "]) == [{"side": "前端", "module": "上傳"}]


def test_parseAreaHandlesMissingAndSingleValue():
  assert parseArea(None) == []
  assert parseArea("") == []
  assert parseArea("前端/上傳") == [{"side": "前端", "module": "上傳"}]


def graphOf(writePlan, rows):
  return analyzePlan(writePlan("0001-demo.md", rows))["graph"]


def test_rowWithoutRequirementHangsDashedFromRoot(writePlan):
  graph = graphOf(writePlan, [
    "| | | D2 做法二 | | T2 任務二 | | 驗證二 |",
    "| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |",
  ])
  # 沒有需求的列不管寫在第幾列都排到最後
  assert [row["rowNumber"] for row in graph.rows] == [2, 1]
  assert graph.problems["orange"] == ["D2 → T2 沒有對應需求（第 1 列）"]
  assert graph.nodes["gap1"] == {"key": "gap1", "kind": "gap", "severity": "orange", "text": "沒有對應需求"}
  assert list(graph.edges.items()) == [
    (("root", "R1"), "solid"), (("R1", "D1"), "solid"), (("D1", "T1"), "solid"), (("T1", "verify-row2"), "solid"),
    (("root", "gap1"), "extra"), (("gap1", "D2"), "solid"), (("D2", "T2"), "solid"), (("T2", "verify-row1"), "solid"),
  ]
  assert graph.nodes["D2"]["withRequirement"] is False
  assert graph.tasks["T2"]["requirementIds"] == []
  assert graph.nodeClass(graph.nodes["T2"]) == "extra"


def test_rowWithoutAnyIdIsCalledThisRow(writePlan):
  graph = graphOf(writePlan, ["| | | 做法三 | | | | |"])
  assert graph.problems["orange"] == ["這列 沒有對應需求（第 1 列）"]
  assert graph.problems["red"] == ["這列 缺任務（第 1 列）"]
  assert graph.problems["yellow"] == ["第 1 列的做法沒有編號"]
  assert graph.nodes["decision-row1"]["text"] == "做法三"


def test_missingDecisionAndTaskMergeIntoOneGap(writePlan):
  graph = graphOf(writePlan, ["| R1 需求一 | 理解一 | | | | | |"])
  assert graph.problems["red"] == ["R1 缺做法、任務（第 1 列）"]
  assert graph.nodes["gap1"]["text"] == "缺：做法、任務"
  assert graph.nodes["gap1"]["severity"] == "red"
  assert list(graph.edges.items()) == [(("root", "R1"), "solid"), (("R1", "gap1"), "solid")]
  # 沒有任務就不要求驗證
  assert graph.problems["yellow"] == []
  assert graph.tasks == {}


def test_missingDecisionGapSitsBeforeTask(writePlan):
  graph = graphOf(writePlan, ["| R1 需求一 | 理解一 | | | T1 任務一 | | 驗證一 |"])
  assert graph.problems["red"] == ["R1 缺做法（第 1 列）"]
  assert graph.nodes["gap1"]["text"] == "缺：做法"
  assert list(graph.edges) == [("root", "R1"), ("R1", "gap1"), ("gap1", "T1"), ("T1", "verify-row1")]


def test_missingTaskGapSitsAfterDecision(writePlan):
  graph = graphOf(writePlan, ["| R1 需求一 | 理解一 | D1 做法一 | | | | 驗證一 |"])
  assert graph.problems["red"] == ["R1 缺任務（第 1 列）"]
  assert graph.nodes["gap1"]["text"] == "缺：任務"
  assert list(graph.edges) == [("root", "R1"), ("R1", "D1"), ("D1", "gap1"), ("gap1", "verify-row1")]


def test_taskWithoutVerifyGetsYellowGap(writePlan):
  graph = graphOf(writePlan, ["| R1 需求一 | 理解一 | D1 做法一 | | T1 任務一 | | |"])
  assert graph.problems["yellow"] == ["T1 缺驗證（第 1 列）"]
  assert graph.nodes["gap1"] == {"key": "gap1", "kind": "gap", "severity": "yellow", "text": "缺：驗證"}
  assert list(graph.edges)[-1] == ("T1", "gap1")
  assert graph.problems["red"] == []


def test_taskWithoutIdAndVerifyIsCalledTask(writePlan):
  graph = graphOf(writePlan, ["| R1 需求一 | 理解一 | D1 做法一 | | 任務一 | | |"])
  assert graph.problems["yellow"] == ["第 1 列的任務沒有編號", "任務 缺驗證（第 1 列）"]
  assert graph.tasks["task-row1"]["id"] is None
  assert graph.tasks["task-row1"]["text"] == "任務一"


def test_requirementWithoutIdIsCalledRequirement(writePlan):
  graph = graphOf(writePlan, ["| 需求一 | 理解一 | D1 做法一 | | | | |"])
  assert graph.problems["red"] == ["需求 缺任務（第 1 列）"]
  assert graph.problems["yellow"] == ["第 1 列的需求沒有編號"]
  assert ("root", "requirement-row1") in graph.edges


def test_taskAcrossRowsCollectsUniqueRequirementsAndDependencies(writePlan):
  graph = graphOf(writePlan, [
    "| R1 需求一 | 理解一 | D1 做法一 | | T1 任務一 | T2 | 驗證一 |",
    "| R1 | | D1 | | T1 | T2, T3 | 驗證二 |",
    "| R2 需求二 | 理解二 | D1 | | T1 | | 驗證三 |",
    "| R2 | | D1 | | T2 任務二 | | 驗證四 |",
    "| R2 | | D1 | | T3 任務三 | | 驗證五 |",
  ])
  task = graph.tasks["T1"]
  assert task["text"] == "任務一"
  assert task["rowNumbers"] == [1, 2, 3]
  assert task["requirementIds"] == ["R1", "R2"]
  assert task["dependsOn"] == ["T2", "T3"]
  assert graph.problems["red"] == []


def test_understandingInLaterRowCounts(writePlan):
  graph = graphOf(writePlan, [
    "| R1 需求一 | | D1 做法一 | | T1 任務一 | | 驗證一 |",
    "| R1 | 理解一 | D1 | | T2 任務二 | | 驗證二 |",
  ])
  assert graph.problems["yellow"] == []


def test_requirementWithoutUnderstandingIsYellow(writePlan):
  graph = graphOf(writePlan, [
    "| R1 需求一 | | D1 做法一 | | T1 任務一 | | 驗證一 |",
    "| 需求二 | | D1 | | T2 任務二 | | 驗證二 |",
  ])
  assert graph.problems["yellow"] == ["第 2 列的需求沒有編號", "R1 缺 AI 理解（第 1 列）", "需求二 缺 AI 理解（第 2 列）"]
  assert graph.nodeClass(graph.nodes["R1"]) == "incomplete"


def test_unknownDependencyOfTaskWithoutId(writePlan):
  graph = graphOf(writePlan, ["| R1 需求一 | 理解一 | D1 做法一 | | 任務一 | T9 | 驗證一 |"])
  assert graph.problems["red"] == ["任務 的前置 T9 不存在（第 1 列）"]


def test_dependencyCycleListsPath(writePlan):
  graph = graphOf(writePlan, [
    "| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | T2 | 驗證一 |",
    "| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |",
  ])
  assert graph.problems["red"] == ["任務互相等待：T1 → T2 → T1"]


def test_planWithoutTableHasNoGraph(tmp_path):
  plan = analyzePlan(writeRawPlan(tmp_path, "# 測試 plan\n\n只有說明\n"))
  assert plan["graph"] is None
  assert plan["rowCount"] == 0
  assert plan["title"] == "測試 plan"
  assert plan["extraText"] == "只有說明"


def test_blankRowsSkippedButCountedAndShortRowsPadded(writePlan):
  plan = analyzePlan(writePlan("0001-demo.md", [
    "| R1 需求一 | 理解一 | D1 做法一 |",
    "| | | | | | | |",
  ]))
  graph = plan["graph"]
  assert plan["rowCount"] == 2
  assert [row["rowNumber"] for row in graph.rows] == [1]
  assert graph.rows[0]["verify"] == ""
  assert graph.problems["red"] == ["R1 缺任務（第 1 列）"]


def test_rootLabelJoinsIdAndTitle(writePlan, tmp_path):
  assert graphOf(writePlan, ["| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |"]).nodes["root"]["text"] == "P0001 測試 plan"
  noId = tmp_path / "0003-noid.md"
  noId.write_text("# 沒有檔頭\n\n" + rtmTable, encoding="utf-8")
  plan = analyzePlan(noId)
  assert plan["meta"] == {}
  assert plan["graph"].nodes["root"]["text"] == "沒有檔頭"


def test_planHeaderAreaWithModules(writePlan):
  path = writePlan("0001-area.md", ["| R1 需求 | 理解 | D1 做法 | - | T1 任務 | | 驗證 |"],
                   extraMeta="area: [前端/縮圖顯示, 後端/縮圖]\n")
  assert parseArea(analyzePlan(path)["meta"]["area"]) == [
    {"side": "前端", "module": "縮圖顯示"}, {"side": "後端", "module": "縮圖"}]


def test_formatListJoinsListsAndHandlesMissing():
  assert formatList(["前端", "後端"]) == "前端、後端"
  assert formatList("P0001") == "P0001"
  assert formatList(None) == ""


def test_mainWithoutArgumentsPrintsUsage(monkeypatch, capsys):
  monkeypatch.setattr("sys.argv", ["rtm_graph.py"])
  assert main() == 1
  assert capsys.readouterr().out == "用法：python rtm_graph.py <plan.md> [輸出.html]\n"


def test_mainWritesHtmlNextToPlanAndPrintsCleanSummary(writePlan, monkeypatch, capsys):
  planPath = writePlan("0001-demo.md", ["| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |"])
  monkeypatch.setattr("sys.argv", ["rtm_graph.py", str(planPath)])
  assert main() == 0
  htmlPath = planPath.resolve().with_suffix(".html")
  assert "<title>測試 plan</title>" in htmlPath.read_text(encoding="utf-8")
  assert capsys.readouterr().out == f"RTM 檢查：0001-demo.md（1 列）\n沒有發現斷線\n圖：{htmlPath}\n"


def test_mainWritesGivenOutputAndListsProblemsBySeverity(writePlan, tmp_path, monkeypatch, capsys):
  planPath = writePlan("0001-demo.md", [
    "| R1 需求一 | | D1 做法一 | | | | |",
    "| | | | | | | |",
  ])
  outputPath = tmp_path / "out" / "graph.html"
  outputPath.parent.mkdir()
  monkeypatch.setattr("sys.argv", ["rtm_graph.py", str(planPath), str(outputPath)])
  assert main() == 0
  assert outputPath.exists()
  assert not planPath.with_suffix(".html").exists()
  # 空白列不畫進圖，但列數照表格實際列數算；沒有問題的嚴重度不印
  assert capsys.readouterr().out == (
    "RTM 檢查：0001-demo.md（2 列）\n"
    "紅 1：R1 缺任務（第 1 列）\n"
    "黃 1：R1 缺 AI 理解（第 1 列）\n"
    f"圖：{outputPath.resolve()}\n")


def test_buildSingleRejectsPlanWithoutTable(tmp_path, capsys):
  planPath = writeRawPlan(tmp_path, "# 測試 plan\n\n只有說明\n")
  assert buildSingle(planPath, None) == 1
  assert not planPath.with_suffix(".html").exists()
  assert capsys.readouterr().out == "0001-demo.md 裡找不到追溯矩陣（要有同時含「需求」「任務」欄的表格）\n"
