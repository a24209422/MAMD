"""tests/browser.py 的共用瀏覽器：整段測試只啟動一次、每個測試各自的 context、等 ready 成立再取 DOM、等不到時重來並附上診斷。"""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import browser
from browser import browserPages, sharedBrowser  # noqa: F401  測試模組裡要看得到 fixture

storageReady = "document.body.dataset.seen !== undefined"
pages = {
  # 先記下這個 context 之前有沒有寫過，再寫一次
  "/storage": "<body><script>const seen = localStorage.getItem('owner'); localStorage.setItem('owner', 'written');"
              " document.body.dataset.seen = seen || 'none';</script></body>",
  "/late": "<body><p>先畫好的部分</p><script>setTimeout(() => { const late = document.createElement('p');"
           " late.id = 'late'; late.textContent = '晚 3 秒才畫好的部分'; document.body.append(late); }, 3000);</script></body>",
  "/broken": "<body><script src=\"/missing.js\"></script><script>console.error('畫面程式壞掉了');</script></body>",
}
# 前面測試拿到的共用瀏覽器，最後確認都是同一個
seenBrowsers = []


@pytest.fixture(scope="module")
def site():
  # 整個模組共用同一個 port，兩個測試的 localStorage 才是同一個網站的
  requests = []
  visits = {"/third": 0}
  class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
      if self.path == "/third":
        # 第 3 次載入才畫出 ready 要的內容，模擬前兩次沒畫好
        visits["/third"] += 1
        body = f'<body data-attempt="{visits["/third"]}"><p>第 {visits["/third"]} 次載入</p></body>'
      elif self.path in pages:
        body = pages[self.path]
      else:
        self.send_error(404)
        return
      payload = body.encode("utf-8")
      self.send_response(200)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.send_header("Content-Length", str(len(payload)))
      self.end_headers()
      self.wfile.write(payload)
    def log_request(self, code="-", size="-"):
      requests.append({"method": self.command, "path": self.path, "status": int(code), "error": None})
    def log_message(self, *args):
      pass
  httpServer = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
  url = f"http://127.0.0.1:{httpServer.server_address[1]}"
  browser.serverRequests[url] = requests
  thread = threading.Thread(target=httpServer.serve_forever, daemon=True)
  thread.start()
  yield url
  httpServer.shutdown()
  httpServer.server_close()
  browser.serverRequests.pop(url)


def test_storageStaysInsideTheTestsOwnContext(browserPages, site):
  seenBrowsers.append(browserPages.browser)
  assert 'data-seen="none"' in browserPages.renderPage(site + "/storage", storageReady)
  # 同一個測試裡 localStorage 有留著；下一個測試看不到才是因為換了 context
  assert 'data-seen="written"' in browserPages.renderPage(site + "/storage", storageReady)


def test_nextTestDoesNotSeePreviousTestsStorage(browserPages, site):
  seenBrowsers.append(browserPages.browser)
  assert 'data-seen="none"' in browserPages.renderPage(site + "/storage", storageReady)


def test_lateReadyStillGetsTheWholePage(browserPages, site):
  seenBrowsers.append(browserPages.browser)
  started = time.monotonic()
  dom = browserPages.renderPage(site + "/late", "document.getElementById('late') !== null")
  assert time.monotonic() - started >= 3
  assert "先畫好的部分" in dom and "晚 3 秒才畫好的部分" in dom


def test_neverReadyFailsAfterThreeAttemptsWithDiagnostics(browserPages, site, monkeypatch):
  monkeypatch.setattr(browser, "readySeconds", 0.5)
  with pytest.raises(AssertionError) as failure:
    browserPages.renderUntil(site + "/broken", "window.neverReady === true")
  text = str(failure.value)
  assert text.startswith("畫面重畫 3 次都沒有準備好：")
  for number in (1, 2, 3):
    assert f"第 {number} 次：耗時 " in text
  assert "第 4 次" not in text
  assert text.count("畫面程式壞掉了") == 3
  # 瀏覽器那邊看到的失敗請求，和 server 收到的請求與狀態碼
  assert text.count(f"GET {site}/missing.js 404") == 3
  assert text.count("    GET /broken 200") == 3
  assert text.count("    GET /missing.js 404") == 3


def test_readyOnThirdAttemptPassesWithWarning(browserPages, site, monkeypatch):
  monkeypatch.setattr(browser, "readySeconds", 0.5)
  with pytest.warns(UserWarning, match="重畫 3 次才有資料"):
    dom = browserPages.renderUntil(site + "/third", "Number(document.body.dataset.attempt) >= 3")
  assert "第 3 次載入" in dom
  # 重來的 context 都已經關掉，不會留到測試結束
  assert browserPages.contexts == []


def test_missingPlaywrightSkipsWithReason(monkeypatch):
  monkeypatch.setattr(browser, "sync_playwright", None)
  with pytest.raises(pytest.skip.Exception, match="找不到 Playwright.*requirements-dev.txt"):
    browser.requireBrowser()


def test_missingBrowserSkipsWithReason(monkeypatch):
  monkeypatch.setattr(browser, "sync_playwright", object())
  monkeypatch.setattr(browser, "browserPath", None)
  with pytest.raises(pytest.skip.Exception, match="找不到 Edge 或 Chrome"):
    browser.requireBrowser()


def test_browserLaunchedOnceInSession(sharedBrowser):
  assert browser.launchCount == 1
  assert all(seen is sharedBrowser for seen in seenBrowsers)
