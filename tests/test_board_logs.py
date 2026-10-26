import json

import board_logs

claudeRecords = [
  {"type": "system", "subtype": "init", "session_id": "s1"},
  {"type": "assistant", "message": {"content": [{"type": "text", "text": "先看 calc.py"}],
                                    "usage": {"input_tokens": 2, "cache_read_input_tokens": 30000,
                                              "cache_creation_input_tokens": 700, "output_tokens": 10}}},
  {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Write",
                                                 "input": {"file_path": "D:\\w\\P0001-T1\\calc.py", "content": "x = 1"}}],
                                    "usage": {"input_tokens": 2, "cache_read_input_tokens": 33570,
                                              "cache_creation_input_tokens": 596, "output_tokens": 16}}},
  {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1",
                                            "content": "The file has been updated"}]}},
  {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu2", "name": "StructuredOutput",
                                                 "input": {"status": "done"}}]}},
  {"type": "result", "subtype": "success", "structured_output": {"status": "done", "summary": "新增 calc.py"}},
]
codexRecords = [
  {"type": "thread.started", "thread_id": "t1"},
  {"type": "turn.started"},
  {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution", "command": "git diff",
                                      "aggregated_output": "diff --git a/calc.py", "exit_code": 0}},
  {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "{\"verdict\":\"pass\"}"}},
  {"type": "turn.completed", "usage": {"input_tokens": 18094, "cached_input_tokens": 0, "output_tokens": 94}},
]
claudeTokens = 2 + 33570 + 596


def writeLog(directory, name, records, tail=""):
  directory.mkdir(parents=True, exist_ok=True)
  path = directory / name
  path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records) + tail,
                  encoding="utf-8")
  return path


def test_claudeSummaryUsesLastToolAndUsage(tmp_path):
  summary = board_logs.logSummary(writeLog(tmp_path, "a.log", claudeRecords), "claude")
  assert summary["action"] == "Write calc.py"
  assert summary["tokens"] == claudeTokens
  assert summary["lastActivity"][:4].isdigit() and "T" in summary["lastActivity"]


def test_codexSummaryUsesLastItemAndTurnUsage(tmp_path):
  summary = board_logs.logSummary(writeLog(tmp_path, "b.log", codexRecords), "codex")
  assert summary["action"] == "回覆審查結果"
  assert summary["tokens"] == 18094


def test_codexSummaryBeforeAnyItem(tmp_path):
  summary = board_logs.logSummary(writeLog(tmp_path, "c.log", codexRecords[:2]), "codex")
  assert (summary["action"], summary["tokens"]) == ("審查中", None)


def test_incompleteLastLineIsIgnored(tmp_path):
  path = writeLog(tmp_path, "d.log", claudeRecords[:3], tail='{"type": "assistant", "mess')
  assert board_logs.logSummary(path, "claude")["action"] == "Write calc.py"


def test_summaryReadsOnlyTail(tmp_path, monkeypatch):
  monkeypatch.setattr(board_logs, "tailBytes", 600)
  oldTool = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "old", "name": "Read",
                                                           "input": {"file_path": "old.py"}}]}}
  chinese = {"type": "assistant", "message": {"content": [{"type": "text", "text": "中" * 30}]}}
  path = writeLog(tmp_path, "e.log", [oldTool] * 50 + [chinese, claudeRecords[2]])
  assert board_logs.logSummary(path, "claude")["action"] == "Write calc.py"
  text, cut = board_logs.readTail(path, 600)
  assert cut is True and len(text.encode("utf-8")) <= 600


def test_claudeDetailPairsToolResults(tmp_path):
  detail = board_logs.logDetail(writeLog(tmp_path, "f.log", claudeRecords), "claude")
  assert [message["text"] for message in detail["messages"]] == ["先看 calc.py", "新增 calc.py"]
  assert detail["tools"] == [{
    "name": "Write", "target": "calc.py",
    "input": json.dumps({"file_path": "D:\\w\\P0001-T1\\calc.py", "content": "x = 1"}, ensure_ascii=False),
    "result": "The file has been updated", "truncated": False}]
  assert detail["tokens"] == claudeTokens
  assert detail["truncated"] is False


def test_detailClipsLongFields(tmp_path, monkeypatch):
  monkeypatch.setattr(board_logs, "maxFieldChars", 10)
  tool = board_logs.logDetail(writeLog(tmp_path, "g.log", claudeRecords), "claude")["tools"][0]
  assert len(tool["result"]) == 10 and tool["truncated"] is True


def test_codexDetailListsCommandsAndMessages(tmp_path):
  detail = board_logs.logDetail(writeLog(tmp_path, "h.log", codexRecords), "codex")
  assert detail["messages"] == [{"text": "{\"verdict\":\"pass\"}"}]
  assert detail["tools"] == [{"name": "執行", "target": "git diff", "input": "git diff",
                              "result": "diff --git a/calc.py", "truncated": False}]
  assert detail["tokens"] == 18094


def test_findLogPicksLatestMatchingFile(tmp_path):
  for name in ["P0001-T1-r1-write-20261026-100000.log", "P0001-T1-r1-write-20261026-110000.log",
               "P0001-T1-r10-write-20261026-120000.log", "P0001-T1-r1-review1-20261026-100500.log",
               "P0001-T1-r1-review2-20261026-100900.log"]:
    writeLog(tmp_path, name, [])
  assert board_logs.findLog(tmp_path, "P0001", "T1", 1, "write").name == "P0001-T1-r1-write-20261026-110000.log"
  assert board_logs.findLog(tmp_path, "P0001", "T1", 1, "review").name == "P0001-T1-r1-review2-20261026-100900.log"
  assert board_logs.findLog(tmp_path, "P0001", "T2", 1, "write") is None
  assert board_logs.findLog(tmp_path / "missing", "P0001", "T1", 1, "write") is None
