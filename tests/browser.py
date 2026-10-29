"""瀏覽器測試共用的 Playwright 工具：整段測試只開一次 Edge（或 Chrome），每個測試用自己的 context，等畫面真的畫好再取 DOM。"""
import os
import shutil
import time
import warnings
from pathlib import Path

import pytest

try:
  from playwright.sync_api import Error as PlaywrightError
  from playwright.sync_api import sync_playwright
except ImportError:
  PlaywrightError = sync_playwright = None

browserCandidates = [
  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]
renderAttempts = 3
# 每次載入等 ready 成立的秒數；要等 script 載完、API 回應，負載高時也要夠用
readySeconds = 15
# 整段測試啟動瀏覽器的次數，測試拿來確認只啟動一次
launchCount = 0
# 測試 server 的網址 → 它收到的請求（method、path、status、error），畫面等不到時列出來
serverRequests = {}
# 測試模組各自 import fixture 時 pytest 會各建一份 session 快取；瀏覽器記在這裡，整段測試仍只啟動一次
shared = {}


def findBrowser():
  for name in ("msedge", "chrome", "chromium"):
    found = shutil.which(name)
    if found:
      return found
  return next((path for path in browserCandidates if Path(path).exists()), None)


browserPath = findBrowser()


def unavailableReason(playwrightLoaded, foundBrowser):
  if not playwrightLoaded:
    return "找不到 Playwright，跳過網頁畫面測試（先執行 python -m pip install -r requirements-dev.txt）"
  if foundBrowser is None:
    return "找不到 Edge 或 Chrome，跳過網頁畫面測試"
  return None


def requireBrowser():
  reason = unavailableReason(sync_playwright is not None, browserPath)
  if reason:
    pytest.skip(reason)


@pytest.fixture(scope="session")
def sharedBrowser():
  requireBrowser()
  if "browser" not in shared:
    launchShared()
  yield shared["browser"]
  closeShared()


def launchShared():
  global launchCount
  playwright = sync_playwright().start()
  try:
    # 用電腦上的 Edge 或 Chrome，不另外下載 Playwright 的 Chromium
    shared["browser"] = playwright.chromium.launch(executable_path=browserPath, headless=True)
  except BaseException:
    playwright.stop()
    raise
  shared["playwright"] = playwright
  launchCount += 1


def closeShared():
  browser = shared.pop("browser", None)
  if browser is not None:
    browser.close()
    shared.pop("playwright").stop()


@pytest.fixture
def browserPages(sharedBrowser):
  pages = BrowserPages(sharedBrowser)
  yield pages
  pages.close()


class BrowserPages:
  # 每個測試一份：開的 context 在測試結束時關掉，cookie、localStorage 不會留給下一個測試
  def __init__(self, browser):
    self.browser = browser
    self.context = None
    self.contexts = []

  def newContext(self):
    context = self.browser.new_context()
    self.contexts.append(context)
    return context

  def closeContext(self, context):
    self.contexts.remove(context)
    context.close()

  def renderPage(self, url, ready, viewport=None):
    # 同一個測試裡的 renderPage 共用這個測試自己的 context；ready 是 JavaScript 條件，成立才取 DOM
    if self.context is None:
      self.context = self.newContext()
    attempt = loadPage(self.context, url, ready, viewport)
    if not attempt.ready:
      attempt.requests = requestsFor(url)
      raise AssertionError(renderFailureText("畫面沒有準備好", [attempt]))
    return attempt.dom

  def renderUntil(self, url, ready, viewport=None):
    # 負載高時偶爾等不到，單獨重跑就過；換新的 context 重來，真的壞掉時每次都不成立仍會失敗
    requests = requestsFor(url)
    tries = []
    for number in range(1, renderAttempts + 1):
      firstRequest = len(requests)
      context = self.newContext()
      attempt = loadPage(context, url, ready, viewport)
      self.closeContext(context)
      if attempt.ready:
        if number > 1:
          warnings.warn(f"{currentTestName()} 的畫面重畫 {number} 次才有資料")
        return attempt.dom
      attempt.requests = requests[firstRequest:]
      tries.append(attempt)
    raise AssertionError(renderFailureText(f"畫面重畫 {len(tries)} 次都沒有準備好", tries))

  def close(self):
    for context in list(self.contexts):
      self.closeContext(context)


class Attempt:
  # 一次載入的結果與診斷：耗時、等不到的原因、console 的錯誤、失敗的請求、server 收到的請求
  def __init__(self):
    self.dom, self.ready, self.seconds, self.error = "", False, 0.0, None
    self.consoleErrors, self.failedRequests, self.requests = [], [], []


def loadPage(context, url, ready, viewport):
  attempt = Attempt()
  page = context.new_page()
  page.on("console", lambda message: message.type == "error" and attempt.consoleErrors.append(message.text))
  page.on("pageerror", lambda error: attempt.consoleErrors.append(f"未處理的例外：{error}"))
  page.on("requestfailed", lambda request: attempt.failedRequests.append(f"{request.method} {request.url} {request.failure}"))
  page.on("response", lambda response: response.status >= 400 and attempt.failedRequests.append(
    f"{response.request.method} {response.url} {response.status}"))
  if viewport:
    page.set_viewport_size(viewport)
  started = time.monotonic()
  try:
    # 不等 load 事件，畫好沒有交給 ready 判斷
    page.goto(url, wait_until="commit", timeout=readySeconds * 1000)
    page.wait_for_function(ready, timeout=readySeconds * 1000)
    attempt.ready = True
  except PlaywrightError as error:
    attempt.error = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
  attempt.seconds = time.monotonic() - started
  try:
    attempt.dom = page.content()
  except PlaywrightError:
    attempt.dom = ""
  page.close()
  return attempt


def requestsFor(url):
  return next((recorded for base, recorded in serverRequests.items() if url.startswith(base)), [])


def currentTestName():
  return os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" ", 1)[0]


def renderFailureText(header, tries):
  lines = [header + "："]
  for number, attempt in enumerate(tries, 1):
    lines.append(f"第 {number} 次：耗時 {attempt.seconds:.1f} 秒" + (f"（{attempt.error}）" if attempt.error else ""))
    lines += listing("瀏覽器 console 的錯誤", attempt.consoleErrors, "（沒有）")
    lines += listing("失敗的請求", attempt.failedRequests, "（沒有）")
    lines += listing("server 收到的請求", [requestText(request) for request in attempt.requests], "（沒有收到）")
  return "\n".join(lines)


def listing(title, items, empty):
  return [f"  {title}：" + ("" if items else empty)] + [f"    {item}" for item in items]


def requestText(request):
  text = f"{request['method']} {request['path']} {request['status']}"
  return f"{text} {request['error']}" if request.get("error") else text
