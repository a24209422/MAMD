from rtm_graph import analyzePlan, parseArea


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


def test_planHeaderAreaWithModules(writePlan):
  path = writePlan("0001-area.md", ["| R1 需求 | 理解 | D1 做法 | - | T1 任務 | | 驗證 |"],
                   extraMeta="area: [前端/縮圖顯示, 後端/縮圖]\n")
  assert parseArea(analyzePlan(path)["meta"]["area"]) == [
    {"side": "前端", "module": "縮圖顯示"}, {"side": "後端", "module": "縮圖"}]
