import contextlib
import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import board
import board_core
import server
from board_core import appendEvent
from modules.system_monitor import api as monitorApi
from modules.system_monitor import metrics

toolDir = Path(__file__).resolve().parent.parent
token = "test-token"
cleanRows = ["| R1 需求一 | 理解一 | D1 做法一 | - | T1 任務一 | | 驗證一 |",
             "| R1 | | D1 | | T2 任務二 | T1 | 驗證二 |"]
warningRows = cleanRows + ["| | | D2 額外做法 | | T3 額外任務 | | 驗證三 |"]
gib = 1024 ** 3
sampledAt = "2026-10-27T10:00:00.000+08:00"
monitorAssets = {"monitor.html": "text/html; charset=utf-8", "monitor.css": "text/css; charset=utf-8",
                 "monitor.js": "text/javascript; charset=utf-8"}


@contextlib.contextmanager
def serving(projectDir):
  httpServer = server.makeServer(projectDir, 0, token)
  thread = threading.Thread(target=httpServer.serve_forever, daemon=True)
  thread.start()
  try:
    yield httpServer
  finally:
    httpServer.shutdown()
    httpServer.server_close()


@pytest.fixture
def running(gitProject):
  with serving(gitProject) as httpServer:
    yield gitProject, httpServer.server_address[1]


@pytest.fixture
def monitored(gitProject):
  with serving(gitProject) as httpServer:
    yield httpServer.RequestHandlerClass.dashboard, httpServer.server_address[1]


def fakeCollector(projectDir, cpu=lambda: (0, 0, 0), memory=lambda: (16 * gib, 4 * gib),
                  disk=lambda path: (500 * gib, 125 * gib, 375 * gib)):
  # 固定替身資料，不讀真實主機；時鐘不動，同一個採集器一直回傳同一份快取
  return metrics.Collector(projectDir, cpu, memory, disk, clock=lambda: 0.0,
                           now=lambda: datetime(2026, 10, 27, 10, 0, tzinfo=timezone(timedelta(hours=8))))


@pytest.fixture
def assetDir(tmp_path, monkeypatch):
  folder = tmp_path / "monitor-web"
  folder.mkdir()
  for name in monitorAssets:
    (folder / name).write_text(f"/* {name} */", encoding="utf-8")
  # 白名單以外的檔案就算放在同一個資料夾也不能下載
  (folder / "notes.md").write_text("secret", encoding="utf-8")
  monkeypatch.setattr(monitorApi, "webDir", folder)
  return folder


def call(port, method, path, body=None, headers=None, rawBody=None):
  connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
  payload = rawBody if rawBody is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
  allHeaders = {"Content-Type": "application/json"}
  allHeaders.update(headers or {})
  connection.request(method, path, body=payload, headers=allHeaders)
  response = connection.getresponse()
  data = response.read()
  connection.close()
  if response.getheader("Content-Type", "").startswith("application/json"):
    return response.status, json.loads(data.decode("utf-8"))
  return response.status, data


def fetch(port, path):
  connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
  connection.request("GET", path)
  response = connection.getresponse()
  data = response.read()
  connection.close()
  return response.status, response.getheader("Content-Type"), data


def post(port, path, body=None, withToken=token):
  return call(port, "POST", path, body if body is not None else {}, headers={"X-Board-Token": withToken})


def eventTypes(projectDir):
  return [event["type"] for event in board_core.readEvents(projectDir)]


def test_servesPageAndState(running):
  projectDir, port = running
  status, page = call(port, "GET", "/")
  assert status == 200 and b"<html" in page.lower()
  status, state = call(port, "GET", "/api/state")
  assert status == 200 and state["project"] == str(projectDir.resolve())
  assert [agent["id"] for agent in state["agents"]][:1] == ["writer"]


def test_unknownRoutesAndNamesAre404(running):
  _, port = running
  assert call(port, "GET", "/api/plans/P9999")[0] == 404
  assert call(port, "GET", "/api/reviews/..%2Fconfig.json")[0] == 404
  assert call(port, "GET", "/api/agents/nobody/log")[0] == 404
  assert call(port, "GET", "/web/index.html")[0] == 404
  assert call(port, "GET", "/js/secret.js")[0] == 404


def test_foreignHostIsRejected(running):
  _, port = running
  assert call(port, "GET", "/api/state", headers={"Host": "evil.example"})[0] == 403


def test_postNeedsCorrectToken(running):
  projectDir, port = running
  assert call(port, "POST", "/api/stop", {})[0] == 403
  assert post(port, "/api/stop", withToken="wrong")[0] == 403
  assert eventTypes(projectDir) == []


def test_wrongTokenReadsBodyBeforeReplying(running):
  # 內容晚一點才到時，server 要先讀完再回 403；沒讀完就關連線，Windows 會送 RST，用戶端收不到 403
  _, port = running
  with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
    client.sendall(f"POST /api/stop HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                   "Content-Length: 2\r\nX-Board-Token: wrong\r\n\r\n".encode("ascii"))
    time.sleep(0.3)
    client.sendall(b"{}")
    time.sleep(0.3)
    response = client.makefile("rb").read()
  assert response.startswith(b"HTTP/1.0 403")


def test_rejectsLargeAndInvalidBody(running):
  _, port = running
  headers = {"X-Board-Token": token}
  assert call(port, "POST", "/api/stop", headers=headers, rawBody=b"x" * (65 * 1024))[0] == 413
  assert call(port, "POST", "/api/stop", headers=headers, rawBody=b"not json")[0] == 400
  assert call(port, "POST", "/api/stop", headers=headers, rawBody=b"[1, 2]")[0] == 400


def test_approveAsksForConfirmationWhenWarnings(running, writePlan):
  projectDir, port = running
  writePlan("0001-ok.md", warningRows, planId="P0001", projectDir=projectDir)
  assert board.cmdSubmit(projectDir, "P0001") == 0
  status, first = post(port, "/api/plans/P0001/approve")
  assert status == 200 and first["ok"] is False and first["needsConfirm"] is True
  assert first["warnings"][0].startswith("橘｜")
  assert "planApproved" not in eventTypes(projectDir)
  _, changed = post(port, "/api/plans/P0001/approve", {"confirm": True, "warnings": ["別的警告"]})
  assert changed["needsConfirm"] is True and "planApproved" not in eventTypes(projectDir)
  _, confirmed = post(port, "/api/plans/P0001/approve", {"confirm": True, "warnings": first["warnings"]})
  assert confirmed["ok"] is True and "已核准" in confirmed["message"]
  assert eventTypes(projectDir)[-1] == "planApproved"


def test_approveWithoutWarningsAndBusinessError(running, writePlan):
  projectDir, port = running
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=projectDir)
  assert board.cmdSubmit(projectDir, "P0001") == 0
  _, approved = post(port, "/api/plans/P0001/approve")
  assert approved["ok"] is True
  status, again = post(port, "/api/plans/P0001/approve")
  assert status == 200 and again["ok"] is False and "不是「待核准」" in again["message"]


def test_rejectNeedsReason(running, writePlan):
  projectDir, port = running
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=projectDir)
  assert board.cmdSubmit(projectDir, "P0001") == 0
  assert post(port, "/api/plans/P0001/reject", {"reason": "  "})[0] == 400
  _, rejected = post(port, "/api/plans/P0001/reject", {"reason": "缺驗證"})
  assert rejected["ok"] is True
  assert board_core.readEvents(projectDir)[-1]["data"] == {"reason": "缺驗證"}


def test_decideAbandonStopAndResume(running, writePlan):
  projectDir, port = running
  writePlan("0001-ok.md", cleanRows, planId="P0001", projectDir=projectDir)
  assert board.cmdSubmit(projectDir, "P0001") == 0
  assert board.cmdApprove(projectDir, "P0001", confirm=lambda prompt: "y") == 0
  appendEvent(projectDir, "needsYou", plan="P0001", task="T1", roundNumber=1, data={"kind": "blocked", "reason": "卡住"})
  assert post(port, "/api/tasks/P0001/T1/decide", {"action": "maybe"})[0] == 400
  assert post(port, "/api/tasks/P0001/T1/decide", {"action": "retry", "note": 3})[0] == 400
  _, decided = post(port, "/api/tasks/P0001/T1/decide", {"action": "retry", "note": " 改 a.py "})
  assert decided["ok"] is True
  assert board_core.readEvents(projectDir)[-1]["data"] == {"action": "retry", "note": "改 a.py"}
  assert post(port, "/api/stop")[1]["ok"] is True
  assert post(port, "/api/stop")[1]["ok"] is False
  assert post(port, "/api/resume")[1]["ok"] is True
  assert post(port, "/api/plans/P0001/abandon")[1]["ok"] is True
  assert eventTypes(projectDir)[-4:] == ["userDecision", "paused", "resumed", "planAbandoned"]


def test_portInUseRaises(gitProject):
  first = server.makeServer(gitProject, 0, token)
  try:
    with pytest.raises(OSError):
      server.makeServer(gitProject, first.server_address[1], token)
  finally:
    first.server_close()


def test_refusesToStartWithoutConsole(gitProject):
  completed = subprocess.run([sys.executable, str(toolDir / "server.py"), str(gitProject), "--port", "0"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             stdin=subprocess.PIPE, timeout=60)
  assert completed.returncode == 2
  assert "只能由使用者在終端機啟動" in completed.stdout


def test_saveRoleWritesConfigAndRejectsBadInput(running, monkeypatch):
  import board_models
  monkeypatch.setattr(board_models, "discoverModels", lambda *args: [{"id": "gpt-6.1-sol", "efforts": []}])
  projectDir, port = running
  configFile = projectDir / ".board" / "config.json"
  assert post(port, "/api/roles/writer", {"cli": "codex"}, withToken="wrong")[0] == 403
  assert post(port, "/api/roles/boss", {"cli": "codex"})[0] == 404
  assert post(port, "/api/roles/writer", {"cli": 3})[0] == 400
  status, payload = post(port, "/api/roles/writer", {"cli": "codex", "model": "gpt&calc"})
  assert status == 400 and payload["ok"] is False and "模型名稱" in payload["message"]
  assert not configFile.exists()
  status, payload = post(port, "/api/roles/writer", {"cli": "codex", "model": " gpt-6.1-sol "})
  assert status == 200 and payload["ok"] is True
  assert payload["message"] == "已儲存：writer 改用 Codex（gpt-6.1-sol），從下一個新啟動的 agent 開始用"
  assert json.loads(configFile.read_text(encoding="utf-8")) == {"writer": {"cli": "codex", "model": "gpt-6.1-sol"}}
  _, state = call(port, "GET", "/api/state")
  assert state["roles"]["writer"] == {"cli": "codex", "model": "gpt-6.1-sol", "customCommand": False, "reasoningEffort": None}


def test_modelCatalogAndEffortValidation(running, monkeypatch):
  import board_models
  projectDir, port = running
  calls = []
  def discover(project, cli, command, **kwargs):
    calls.append((cli, command))
    return [{"id": "new-model", "efforts": [{"value": "high"}], "displayName": "New Model"}]
  monkeypatch.setattr(board_models, "discoverModels", discover)
  assert post(port, "/api/models/writer", {"cli": "codex"}, withToken="wrong")[0] == 403
  assert post(port, "/api/models/writer", {"cli": "bad"})[0] == 400
  status, payload = post(port, "/api/models/writer", {"cli": "codex"})
  assert status == 200 and payload["models"][0]["id"] == "new-model"
  post(port, "/api/models/writer", {"cli": "codex", "refresh": True})
  assert len(calls) == 2
  for choice in [{"model": "typo"}, {"model": "new-model", "reasoningEffort": "ultra"},
                 {"model": "new-model", "reasoningEffort": True}, {"reasoningEffort": "high"}]:
    assert post(port, "/api/roles/writer", {"cli": "codex", **choice})[0] == 400
  status, payload = post(port, "/api/roles/writer", {"cli": "codex", "model": "new-model", "reasoningEffort": "high"})
  assert status == 200 and payload["ok"] is True
  assert board_core.readUserConfig(projectDir)["writer"]["reasoningEffort"] == "high"
  assert call(port, "GET", "/api/state")[1]["roles"]["writer"]["reasoningEffort"] == "high"


def test_catalogUsesRoleCommandAndShowsCliErrors(running, monkeypatch):
  import board_models
  projectDir, port = running
  board_core.writeUserConfig(projectDir, {"writer": {"cli": "claude", "command": ["custom-claude"]}})
  calls = []
  def fail(project, cli, command, **kwargs):
    calls.append(command)
    raise board_core.BoardError("請先登入")
  monkeypatch.setattr(board_models, "discoverModels", fail)
  _, payload = post(port, "/api/models/writer", {"cli": "claude"})
  assert payload["ok"] is False and payload["message"] == "請先登入"
  assert calls == [["custom-claude"]]
  assert post(port, "/api/models/writer", {"cli": "codex"})[0] == 400
  assert post(port, "/api/roles/writer", {"cli": "claude", "model": "opus"})[0] == 400
  assert post(port, "/api/roles/writer", {"cli": "claude"})[0] == 200


def test_eachServerHasItsOwnCollectorForProject(gitProject):
  first, second = server.Dashboard(gitProject, token), server.Dashboard(gitProject, token)
  assert first.systemMonitor.collector is not second.systemMonitor.collector
  assert first.systemMonitor.collector.projectDir == gitProject.resolve()


def test_systemMonitorReturnsFixedFields(monitored):
  dashboard, port = monitored
  dashboard.systemMonitor.collector = fakeCollector(dashboard.projectDir)
  status, contentType, data = fetch(port, "/api/system-monitor")
  assert status == 200 and contentType == "application/json; charset=utf-8"
  payload = json.loads(data.decode("utf-8"))
  assert payload == {
    "sampleId": 1, "sampledAt": sampledAt, "intervalSeconds": 3,
    "cpu": {"status": "warming-up", "updatedAt": sampledAt, "message": "取樣中", "percent": None},
    "memory": {"status": "ok", "updatedAt": sampledAt, "message": None, "totalBytes": 16 * gib,
               "usedBytes": 12 * gib, "availableBytes": 4 * gib, "percent": 75.0},
    "disk": {"status": "ok", "updatedAt": sampledAt, "message": None, "totalBytes": 500 * gib,
             "usedBytes": 125 * gib, "freeBytes": 375 * gib, "percent": 25.0,
             "volume": metrics.volumeLabel(dashboard.projectDir)},
  }
  # 不同頁面共用這個 server 的採集器：快取期間再讀是同一份取樣
  assert call(port, "GET", "/api/system-monitor?from=console")[1] == payload


def test_systemMonitorIsReadOnly(monitored):
  dashboard, port = monitored
  dashboard.systemMonitor.collector = fakeCollector(dashboard.projectDir)
  assert call(port, "POST", "/api/system-monitor", {})[0] == 403
  assert post(port, "/api/system-monitor")[0] == 404
  assert call(port, "GET", "/api/system-monitor/refresh")[0] == 404


def test_monitorAssetsHaveContentType(running, assetDir):
  _, port = running
  for name, contentType in monitorAssets.items():
    assert fetch(port, monitorApi.assetPrefix + name) == (200, contentType, f"/* {name} */".encode("utf-8"))
  assert fetch(port, monitorApi.assetPrefix + "monitor.js?v=2")[0] == 200
  # 白名單裡的檔案還沒放上去時是 404，不是 500
  (assetDir / "monitor.css").unlink()
  assert call(port, "GET", monitorApi.assetPrefix + "monitor.css")[0] == 404


def test_unknownAndTraversalAssetsAre404(running, assetDir):
  _, port = running
  names = ["notes.md", "MONITOR.JS", "", "./monitor.js", "web/monitor.js", "monitor.js/..", "api.py", "metrics.py",
           "__init__.py", "README.md", "tests/test_metrics.py", "../system_monitor/api.py", "..%2Fapi.py",
           "%2e%2e/api.py", "..\\api.py", "../../../server.py"]
  for name in names:
    assert call(port, "GET", monitorApi.assetPrefix + name)[0] == 404, name
  # 模組資料夾不是靜態根目錄，換個寫法也拿不到 Python 和測試
  for path in ["/modules/system-monitor", "/modules/", "/modules/system_monitor/api.py",
               "/modules/system_monitor/web/monitor.js", "/monitor.js", "/js/../modules/system_monitor/metrics.py"]:
    assert call(port, "GET", path)[0] == 404, path


def test_monitorRoutesKeepHostCheck(monitored, assetDir):
  dashboard, port = monitored
  dashboard.systemMonitor.collector = fakeCollector(dashboard.projectDir)
  for path in ["/api/system-monitor", monitorApi.assetPrefix + "monitor.js"]:
    for host in ["evil.example", f"evil.example:{port}", f"127.0.0.1.evil.example:{port}"]:
      status, payload = call(port, "GET", path, headers={"Host": host})
      assert status == 403 and "localhost" in payload["message"]
    assert call(port, "GET", path, headers={"Host": f"localhost:{port}"})[0] == 200


def test_collectorFailuresKeepStateAndPageWorking(monitored):
  dashboard, port = monitored
  def fail(*args):
    raise OSError("拒絕存取")
  dashboard.systemMonitor.collector = fakeCollector(dashboard.projectDir, cpu=fail, memory=fail, disk=fail)
  status, payload = call(port, "GET", "/api/system-monitor")
  # 預期中的採集失敗只標在各項，整個回應仍然成功
  assert status == 200
  assert [payload[item]["status"] for item in ("cpu", "memory", "disk")] == ["error"] * 3
  assert payload["memory"]["totalBytes"] is None and "拒絕存取" in payload["disk"]["message"]

  class BrokenCollector:
    def snapshot(self):
      raise RuntimeError("採集器壞了")
  dashboard.systemMonitor.collector = BrokenCollector()
  status, payload = call(port, "GET", "/api/system-monitor")
  # 沒預料到的錯誤沿用 server 原本的錯誤處理，不讓 server 停掉
  assert status == 500 and "採集器壞了" in payload["message"]
  status, state = call(port, "GET", "/api/state")
  assert status == 200 and state["project"] == str(dashboard.projectDir)
  status, page = call(port, "GET", "/")
  assert status == 200 and b"<html" in page.lower()
