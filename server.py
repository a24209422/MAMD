"""agent-board 儀表板的本機 server：提供 web/ 的網頁和 /api/ 的資料，寫入交給 board.py 的指令函式。

用法：python server.py <專案資料夾> [--port 8765]
"""
import argparse
import contextlib
import hmac
import io
import json
import re
import secrets
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import board
import board_api
from board_core import BoardError, PlanStatus, roleChoiceText, saveRoleChoice

webDir = Path(__file__).resolve().parent / "web"
scriptNames = ["core", "overview", "plan", "ops", "console", "assistant", "main"]
staticFiles = {"/": ("index.html", "text/html; charset=utf-8")}
staticFiles.update({f"/js/{name}.js": (f"js/{name}.js", "text/javascript; charset=utf-8") for name in scriptNames})
maxBodyBytes = 64 * 1024
maxDiscardBytes = 1024 * 1024
decideActions = ("retry", "done", "abandon")
idText = r"[A-Za-z]+\d+"
getRoutes = [
  (re.compile(r"^/api/state$"), board_api.stateView),
  (re.compile(rf"^/api/plans/({idText})$"), board_api.planView),
  (re.compile(r"^/api/reviews/([^/]+)$"), board_api.reviewText),
  (re.compile(r"^/api/agents/([A-Za-z0-9-]+)/log$"), board_api.agentLogView),
  (re.compile(rf"^/api/tasks/({idText})/({idText})/diff$"), board_api.taskDiffView),
]
postRoutes = [
  (re.compile(rf"^/api/plans/({idText})/approve$"), "approve"),
  (re.compile(rf"^/api/plans/({idText})/reject$"), "reject"),
  (re.compile(rf"^/api/plans/({idText})/abandon$"), "abandon"),
  (re.compile(rf"^/api/tasks/({idText})/({idText})/decide$"), "decide"),
  (re.compile(r"^/api/stop$"), "stop"),
  (re.compile(r"^/api/resume$"), "resume"),
  (re.compile(r"^/api/roles/(writer|reviewer)$"), "saveRole"),
]


class Dashboard:
  def __init__(self, projectDir, token):
    self.projectDir = Path(projectDir).resolve()
    self.token = token
    # 指令函式把結果印到 stdout；接住時 stdout 是整個程式共用的，同一時間只能有一個寫入在跑
    self.writeLock = threading.RLock()
    self.errorLock = threading.Lock()
    self.reportedErrors = set()

  def tokenMatches(self, given):
    return hmac.compare_digest(given.encode("utf-8"), self.token.encode("utf-8"))

  def runCommand(self, command, *args, **kwargs):
    with self.writeLock:
      buffer = io.StringIO()
      try:
        with contextlib.redirect_stdout(buffer):
          code = command(self.projectDir, *args, **kwargs)
      except BoardError as error:
        return {"ok": False, "message": str(error)}
    return {"ok": code == 0, "message": buffer.getvalue().strip()}

  def approve(self, planIdValue, body):
    with self.writeLock:
      plans, state = board.currentState(self.projectDir)
      plan, planState = plans.get(planIdValue), state["plans"].get(planIdValue)
      warnings = []
      # 不是待核准、或還有紅色問題時，直接交給 cmdApprove 回報原因，不用先問確認
      if (plan and plan["graph"] and planState and planState["status"] == PlanStatus.pending
          and not board.planBlockers(plan)):
        warnings = board.approveWarnings(self.projectDir, plan)
      # 你看到的警告和現在的不同（例如這段時間主資料夾多了未 commit 的改動）就要重新確認
      if warnings and not (body.get("confirm") is True and body.get("warnings") == warnings):
        return 200, {"ok": False, "needsConfirm": True, "warnings": warnings, "message": "核准前請確認以下事項"}
      return 200, self.runCommand(board.cmdApprove, planIdValue, confirm=lambda prompt: "y")

  def reject(self, planIdValue, body):
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
      return 400, {"ok": False, "message": "退回要寫原因"}
    return 200, self.runCommand(board.cmdReject, planIdValue, reason.strip())

  def abandon(self, planIdValue, body):
    return 200, self.runCommand(board.cmdAbandon, planIdValue)

  def decide(self, planIdValue, taskId, body):
    action, note = body.get("action"), body.get("note", "")
    if action not in decideActions:
      return 400, {"ok": False, "message": "action 只能是 retry、done、abandon"}
    if not isinstance(note, str):
      return 400, {"ok": False, "message": "note 要是文字"}
    return 200, self.runCommand(board.cmdDecide, planIdValue, taskId, action, note.strip())

  def stop(self, body):
    return 200, self.runCommand(board.cmdStop)

  def resume(self, body):
    return 200, self.runCommand(board.cmdResume)

  def saveRole(self, role, body):
    cli, model = body.get("cli"), body.get("model") or ""
    if not isinstance(cli, str) or not isinstance(model, str):
      return 400, {"ok": False, "message": "cli、model 要是文字"}
    model = model.strip()
    # 和其他寫入共用鎖：兩個分頁同時存，讀改寫才不會互相蓋掉
    with self.writeLock:
      try:
        saveRoleChoice(self.projectDir, role, cli, model)
      except BoardError as error:
        return 400, {"ok": False, "message": str(error)}
    return 200, {"ok": True, "message": f"已儲存：{role} 改用 {roleChoiceText(cli, model)}，從下一個新啟動的 agent 開始用"}

  def reportError(self, error):
    message = f"{type(error).__name__}: {error}"
    with self.errorLock:
      # 網頁每 2 秒就會再打一次，同一個錯誤只印一次，終端機才不會被洗掉
      if message in self.reportedErrors:
        return
      self.reportedErrors.add(message)
    print(f"server 錯誤：{message}", file=sys.stderr)
    traceback.print_exception(error)


class DashboardHandler(BaseHTTPRequestHandler):
  dashboard = None

  def log_message(self, format, *args):
    # 網頁每 2 秒輪詢一次，成功的請求不印；錯誤由 reportError 印
    return

  def do_GET(self):
    self.dispatch(self.routeGet)

  def do_POST(self):
    self.dispatch(self.routePost)

  def dispatch(self, route):
    try:
      if not self.hostAllowed():
        self.sendJson(403, {"ok": False, "message": "只接受 127.0.0.1 或 localhost 的網址"})
        return
      route(urlsplit(self.path).path)
    except Exception as error:
      self.dashboard.reportError(error)
      self.sendJson(500, {"ok": False, "message": f"server 錯誤：{error}"})

  def hostAllowed(self):
    # 擋 DNS rebinding：別的網站把自己的網域指到 127.0.0.1 時，Host 會是那個網域
    port = self.server.server_address[1]
    return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

  def sendBody(self, status, body, contentType):
    self.send_response(status)
    self.send_header("Content-Type", contentType)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    self.wfile.write(body)

  def sendJson(self, status, payload):
    self.sendBody(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

  def routeGet(self, path):
    if path in staticFiles:
      fileName, contentType = staticFiles[path]
      filePath = webDir / fileName
      if filePath.is_file():
        self.sendBody(200, filePath.read_bytes(), contentType)
        return
    for pattern, handler in getRoutes:
      match = pattern.match(path)
      if match:
        payload = handler(self.dashboard.projectDir, *match.groups())
        if payload is not None:
          self.sendJson(200, payload)
          return
        break
    self.sendJson(404, {"ok": False, "message": "找不到"})

  def routePost(self, path):
    try:
      length = int(self.headers.get("Content-Length") or 0)
    except ValueError:
      self.sendJson(400, {"ok": False, "message": "Content-Length 不正確"})
      return
    if length > maxBodyBytes:
      # 先讀掉送來的內容再回應；用戶端還在送時就關掉連線，它會收不到 413
      self.rfile.read(min(length, maxDiscardBytes))
      self.sendJson(413, {"ok": False, "message": "內容太大"})
      return
    # token 錯也要先讀完內容：沒讀完就關連線，Windows 會送 RST，用戶端還沒讀到的 403 會被丟掉
    data = self.rfile.read(length) if length else b""
    if not self.dashboard.tokenMatches(self.headers.get("X-Board-Token", "")):
      self.sendJson(403, {"ok": False, "message": "token 不對：請用 server.py 印出的網址重新開啟"})
      return
    try:
      body = json.loads(data.decode("utf-8")) if data else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
      body = None
    if not isinstance(body, dict):
      self.sendJson(400, {"ok": False, "message": "內容要是 JSON 物件"})
      return
    for pattern, methodName in postRoutes:
      match = pattern.match(path)
      if match:
        status, payload = getattr(self.dashboard, methodName)(*match.groups(), body)
        self.sendJson(status, payload)
        return
    self.sendJson(404, {"ok": False, "message": "找不到"})


class DashboardServer(ThreadingHTTPServer):
  # Windows 的 SO_REUSEADDR 會讓第二個 server 也綁上同一個埠、卻收不到請求；關掉才會得到「埠被佔用」
  allow_reuse_address = False
  daemon_threads = True


def makeServer(projectDir, port, token):
  handler = type("BoundDashboardHandler", (DashboardHandler,), {"dashboard": Dashboard(projectDir, token)})
  return DashboardServer(("127.0.0.1", port), handler)


def main(argv=None):
  if not sys.stdout.isatty():
    # 輸出被導到檔案或管線時，Windows 預設編碼不是 UTF-8，中文會變亂碼
    sys.stdout.reconfigure(encoding="utf-8")
  parser = argparse.ArgumentParser(description="agent-board 儀表板")
  parser.add_argument("project", help="專案資料夾")
  parser.add_argument("--port", type=int, default=8765)
  args = parser.parse_args(argv)
  if not board.isHuman():
    print("server.py 只能由使用者在終端機啟動：網址裡的 token 等於核准權限，不能讓 agent 拿到。")
    return 2
  projectDir = Path(args.project).resolve()
  if not projectDir.is_dir():
    print(f"找不到專案資料夾：{projectDir}")
    return 1
  token = secrets.token_urlsafe(32)
  try:
    httpServer = makeServer(projectDir, args.port, token)
  except OSError:
    print(f"埠 {args.port} 被佔用，用 --port 換一個")
    return 1
  print(f"儀表板：http://127.0.0.1:{httpServer.server_address[1]}/#token={token}")
  print("這個網址等於核准權限，不要貼給別人或 agent。按 Ctrl+C 結束。")
  try:
    httpServer.serve_forever()
  except KeyboardInterrupt:
    print("已結束")
  finally:
    httpServer.server_close()
  return 0


if __name__ == "__main__":
  sys.exit(main())
