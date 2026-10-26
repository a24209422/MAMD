import board_core
from board_index import buildIndex, checkPlans
from rtm_graph import analyzePlan

goodRow = "| R1 需求 | 理解 | D1 做法 | - | T1 任務 | | 驗證 |"


def test_indexGroupsByDerivedStatus(tmp_path, writePlan):
  writePlan("0001-first.md", [goodRow], planId="P0001", title="第一份")
  writePlan("0002-second.md", [goodRow], planId="P0002", title="第二份")
  board_core.appendEvent(tmp_path, "planSubmitted", plan="P0002")
  indexPath, plans, warnings = buildIndex(tmp_path)
  page = indexPath.read_text(encoding="utf-8")
  assert indexPath == tmp_path / "plans" / "_review" / "index.html"
  assert "待核准（1）" in page and "草稿（1）" in page
  assert page.index("待核准（1）") < page.index("草稿（1）")
  assert (tmp_path / "plans" / "_review" / "0001-first.html").exists()
  assert warnings == []


def test_closedStatusesAreCollapsed(tmp_path, writePlan):
  writePlan("0001-first.md", [goodRow], planId="P0001")
  board_core.appendEvent(tmp_path, "planApproved", plan="P0001", data={"baseCommit": "x", "tasks": []})
  board_core.appendEvent(tmp_path, "planAbandoned", plan="P0001")
  page = buildIndex(tmp_path)[0].read_text(encoding="utf-8")
  assert "<details><summary>放棄（1）</summary>" in page


def test_checkPlansWarnings(tmp_path, writePlan):
  first = writePlan("0001-first.md", [goodRow], planId="P0001")
  duplicate = writePlan("0002-dup.md", [goodRow], planId="P0001")
  superseding = writePlan("0003-new.md", [goodRow], planId="P0003", extraMeta="supersedes: P0099\n")
  warnings = checkPlans([analyzePlan(path) for path in (first, duplicate, superseding)])
  assert "0001-first.md 和 0002-dup.md 的 id 都是 P0001" in warnings
  assert "P0003 寫了取代 P0099，但找不到 P0099" in warnings
  assert not any("status" in warning for warning in warnings)


def test_rtmGraphRefusesDirectory(tmp_path, monkeypatch, capsys):
  import rtm_graph
  monkeypatch.setattr("sys.argv", ["rtm_graph.py", str(tmp_path)])
  assert rtm_graph.main() == 1
  assert "board.py" in capsys.readouterr().out
