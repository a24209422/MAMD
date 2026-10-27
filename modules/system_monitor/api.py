"""系統監控掛到儀表板 server 的介面：唯讀的 GET /api/system-monitor 與畫面資產白名單。

不自己開 server：由 server.py 在每個實例建一個 SystemMonitor，請求經過 server 原有的 Host 檢查與錯誤處理後才轉進來。
"""
import json
from pathlib import Path

from .metrics import Collector

apiPath = "/api/system-monitor"
assetPrefix = "/modules/system-monitor/"
webDir = Path(__file__).resolve().parent / "web"
# 只開放畫面需要的三個檔案，路徑要完全相同；模組的 Python、測試與 README 不能被下載
assets = {
  f"{assetPrefix}monitor.html": ("monitor.html", "text/html; charset=utf-8"),
  f"{assetPrefix}monitor.css": ("monitor.css", "text/css; charset=utf-8"),
  f"{assetPrefix}monitor.js": ("monitor.js", "text/javascript; charset=utf-8"),
}
jsonType = "application/json; charset=utf-8"


class SystemMonitor:
  def __init__(self, projectDir, collector=None):
    # 同一個 server 的所有頁面共用這個採集器，CPU 基準不會被各自重置
    self.collector = collector or Collector(projectDir)

  def handleGet(self, path):
    """這個模組的路徑回傳 (內容 bytes, Content-Type)；其他路徑回傳 None，交還 server 處理。"""
    if path == apiPath:
      return json.dumps(self.collector.snapshot(), ensure_ascii=False).encode("utf-8"), jsonType
    if path in assets:
      fileName, contentType = assets[path]
      filePath = webDir / fileName
      if filePath.is_file():
        return filePath.read_bytes(), contentType
    return None
