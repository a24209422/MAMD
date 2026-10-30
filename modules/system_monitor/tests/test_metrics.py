import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import pytest

from modules.system_monitor import metrics

gib = 1024 ** 3
stamp = "2026-10-27T10:00:00.000+08:00"


class FakeClock:
  def __init__(self):
    self.value = 1000.0

  def __call__(self):
    return self.value

  def advance(self, seconds=metrics.intervalSeconds):
    self.value += seconds


def sequence(*values):
  # 依序回傳，用完後重複最後一個；值是例外就丟出
  calls = []
  def read(*args):
    calls.append(args)
    value = values[min(len(calls), len(values)) - 1]
    if isinstance(value, Exception):
      raise value
    return value
  read.calls = calls
  return read


def makeCollector(projectDir, cpu=(0, 0, 0), memory=(16 * gib, 4 * gib), disk=(1000, 250, 750), clock=None):
  readers = [reader if callable(reader) else sequence(reader) for reader in (cpu, memory, disk)]
  return metrics.Collector(projectDir, *readers, clock=clock or FakeClock(),
                           now=lambda: datetime(2026, 10, 27, 10, 0, tzinfo=timezone(timedelta(hours=8))))


def test_cpuUsesAdjacentDeltaWithIdleInsideKernelTime(tmp_path):
  clock = FakeClock()
  collector = makeCollector(tmp_path, cpu=sequence((0, 0, 0), (600, 800, 200), (600, 900, 300)), clock=clock)
  assert collector.snapshot()["cpu"] == {"status": "warming-up", "updatedAt": stamp, "message": "取樣中", "percent": None}
  clock.advance()
  # (800 + 200 - 600) / (800 + 200)；把 idle 再加進總時間會變成 25%
  assert collector.snapshot()["cpu"] == {"status": "ok", "updatedAt": stamp, "message": None, "percent": 40.0}
  clock.advance()
  # 和上一次比，不是和第一次比（和第一次比會是 50%）
  assert collector.snapshot()["cpu"]["percent"] == 100.0


def test_firstSampleDoesNotWaitAndInvalidDeltaRebuildsBase(tmp_path):
  clock = FakeClock()
  cpu = sequence((1000, 2000, 1000), (0, 0, 0), (300, 500, 500), (300, 500, 500), (400, 700, 700), (500, 800, 900))
  collector = makeCollector(tmp_path, cpu=cpu, clock=clock)
  results = [collector.snapshot()["cpu"]]
  # 第一次只讀一次就回傳，不等待下一次取樣
  assert len(cpu.calls) == 1 and results[0]["status"] == "warming-up"
  for seconds in (3, 3, 3, metrics.maxCpuBaseAge + 1, 3):
    clock.advance(seconds)
    results.append(collector.snapshot()["cpu"])
  # 計數倒退：以這次重建基準
  assert results[1]["status"] == "warming-up"
  assert results[2]["percent"] == 70.0
  # 沒有經過任何 CPU 時間
  assert results[3]["status"] == "warming-up"
  # 基準太舊：不把長時間平均當成目前使用率
  assert results[4]["status"] == "warming-up" and results[4]["percent"] is None
  # (100 + 200 - 100) / (100 + 200)
  assert results[5] == {"status": "ok", "updatedAt": stamp, "message": None, "percent": 66.7}


def test_memoryAndDiskReportBytesForProjectVolume(tmp_path):
  projectDir = tmp_path / "proj"
  (projectDir / "sub").mkdir(parents=True)
  disk = sequence((500 * gib, 125 * gib, 375 * gib))
  snapshot = makeCollector(projectDir / "sub" / "..", disk=disk).snapshot()
  assert {key: snapshot[key] for key in ("sampleId", "sampledAt", "intervalSeconds")} == {
    "sampleId": 1, "sampledAt": stamp, "intervalSeconds": 3}
  assert snapshot["memory"] == {"status": "ok", "updatedAt": stamp, "message": None, "totalBytes": 16 * gib,
                                "usedBytes": 12 * gib, "availableBytes": 4 * gib, "percent": 75.0}
  assert snapshot["disk"] == {"status": "ok", "updatedAt": stamp, "message": None, "totalBytes": 500 * gib,
                              "usedBytes": 125 * gib, "freeBytes": 375 * gib, "percent": 25.0,
                              "volume": metrics.volumeLabel(projectDir.resolve())}
  # 用專案的實際位置查詢
  assert disk.calls == [(projectDir.resolve(),)]


def test_volumeLabelWithoutDriveClimbsToMountPoint(monkeypatch):
  mounts = {"/mnt/data"}
  # 不依賴執行測試的主機實際有哪些掛載點
  monkeypatch.setattr(metrics.os.path, "ismount", lambda path: str(path) in mounts)
  assert metrics.volumeLabel(PurePosixPath("/mnt/data/proj/sub")) == "/mnt/data"
  assert metrics.volumeLabel(PurePosixPath("/mnt/data")) == "/mnt/data"
  # 一路都找不到掛載點時停在根目錄
  assert metrics.volumeLabel(PurePosixPath("/home/user/proj")) == "/"


def test_singleItemFailureKeepsOtherItemsUpdating(tmp_path):
  clock = FakeClock()
  cpu = sequence((0, 0, 0), OSError("拒絕存取"), (600, 800, 200))
  memory = sequence(OSError("系統忙碌"), (4 * gib, 8 * gib), (16 * gib, 4 * gib))
  disk = sequence((0, 0, 0), (1000, 250, 750))
  collector = makeCollector(tmp_path, cpu=cpu, memory=memory, disk=disk, clock=clock)
  first = collector.snapshot()
  assert first["cpu"]["status"] == "warming-up"
  assert first["memory"] == {"status": "error", "updatedAt": stamp, "message": "RAM 資料暫時無法取得：系統忙碌",
                             "totalBytes": None, "usedBytes": None, "availableBytes": None, "percent": None}
  assert first["disk"]["status"] == "error" and "不合理" in first["disk"]["message"]
  assert first["disk"]["percent"] is None and first["disk"]["volume"]
  clock.advance()
  second = collector.snapshot()
  assert second["cpu"]["status"] == "error" and "拒絕存取" in second["cpu"]["message"] and second["cpu"]["percent"] is None
  # 可用量大於總量也是錯誤，不能算出負的已用量
  assert second["memory"]["status"] == "error" and second["memory"]["usedBytes"] is None
  assert second["disk"]["percent"] == 25.0
  clock.advance()
  third = collector.snapshot()
  # 中間讀取失敗不清掉基準，下一次成功直接算出使用率
  assert third["cpu"]["percent"] == 40.0
  assert third["memory"]["percent"] == 75.0


def test_unsupportedPlatformStillReportsDisk(tmp_path, monkeypatch):
  monkeypatch.setattr(metrics, "isWindows", False)
  snapshot = metrics.Collector(tmp_path, clock=FakeClock()).snapshot()
  assert snapshot["cpu"]["status"] == "unsupported" and snapshot["cpu"]["percent"] is None
  assert snapshot["cpu"]["message"] == "第一版只支援 Windows 主機"
  assert snapshot["memory"]["status"] == "unsupported"
  assert [snapshot["memory"][key] for key in metrics.memoryFields] == [None] * 4
  assert snapshot["disk"]["status"] == "ok"
  assert snapshot["disk"]["totalBytes"] == shutil.disk_usage(tmp_path).total


@pytest.mark.parametrize("layout", [(128, 2), (60, 2)])
def test_processorGroupsBeyondSystemTimesMarkOnlyCpuUnsupported(tmp_path, monkeypatch, layout):
  monkeypatch.setattr(metrics, "isWindows", True)
  monkeypatch.setattr(metrics, "processorLayout", lambda: layout)
  snapshot = makeCollector(tmp_path, cpu=metrics.windowsCpuTimes).snapshot()
  assert snapshot["cpu"]["status"] == "unsupported" and "64" in snapshot["cpu"]["message"]
  assert snapshot["cpu"]["percent"] is None
  assert snapshot["memory"]["status"] == "ok" and snapshot["disk"]["status"] == "ok"


def test_resultsIncludingFailuresAreCachedForInterval(tmp_path):
  clock = FakeClock()
  cpu = sequence((0, 0, 0), (600, 800, 200))
  memory = sequence(OSError("系統忙碌"), (16 * gib, 4 * gib))
  collector = makeCollector(tmp_path, cpu=cpu, memory=memory, clock=clock)
  first = collector.snapshot()
  clock.advance(2.5)
  again = collector.snapshot()
  assert again == first and again["sampleId"] == 1
  assert len(cpu.calls) == 1 and len(memory.calls) == 1
  # 呼叫端拿到的是複本，改了不會影響快取
  again["memory"]["status"] = "ok"
  assert collector.snapshot()["memory"]["status"] == "error"
  clock.advance(0.5)
  fresh = collector.snapshot()
  assert fresh["sampleId"] == 2 and fresh["memory"]["status"] == "ok" and fresh["cpu"]["percent"] == 40.0
  assert len(cpu.calls) == 2 and len(memory.calls) == 2


def test_concurrentReadersShareOneSampleAndCpuBase(tmp_path):
  clock = FakeClock()
  readings = sequence((0, 0, 0), (600, 800, 200))
  def slowCpu():
    # 拉長採樣時間，讓其他執行緒一定在採樣途中進來
    time.sleep(0.05)
    return readings()
  collector = makeCollector(tmp_path, cpu=slowCpu, clock=clock)
  barrier, results = threading.Barrier(8), []
  def read():
    barrier.wait()
    results.append(collector.snapshot())
  threads = [threading.Thread(target=read) for _ in range(8)]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join(timeout=10)
  assert len(results) == 8 and {result["sampleId"] for result in results} == {1}
  assert len(readings.calls) == 1
  clock.advance()
  # 多個讀取者沒有各自重置基準，下一次仍和第一次的基準比
  assert collector.snapshot()["cpu"]["percent"] == 40.0


@pytest.mark.skipif(os.name != "nt", reason="真實採集只支援 Windows")
@pytest.mark.parametrize("which", ["repo", "tmp"])
def test_realWindowsSampleIsInRangeForGivenProject(tmp_path, which):
  projectDir = Path(__file__).resolve().parents[3] if which == "repo" else tmp_path
  collector = metrics.Collector(projectDir, ttl=0)
  started = time.monotonic()
  snapshot = collector.snapshot()
  # 第一次只建立 CPU 基準，不等待取樣間隔
  assert time.monotonic() - started < 1
  assert datetime.fromisoformat(snapshot["sampledAt"]).utcoffset() is not None
  cpuStatus = snapshot["cpu"]["status"]
  assert cpuStatus in ("warming-up", "unsupported")
  deadline = time.monotonic() + 5
  while snapshot["cpu"]["status"] == "warming-up" and time.monotonic() < deadline:
    time.sleep(0.25)
    snapshot = collector.snapshot()
  if cpuStatus == "warming-up":
    assert snapshot["cpu"]["status"] == "ok" and 0 <= snapshot["cpu"]["percent"] <= 100

  memory = snapshot["memory"]
  assert memory["status"] == "ok"
  assert 0 < memory["availableBytes"] <= memory["totalBytes"]
  assert memory["usedBytes"] == memory["totalBytes"] - memory["availableBytes"]
  assert 0 <= memory["percent"] <= 100

  disk = snapshot["disk"]
  assert disk["status"] == "ok"
  assert disk["volume"] == os.path.splitdrive(str(projectDir.resolve()))[0]
  assert disk["totalBytes"] == shutil.disk_usage(projectDir).total
  assert disk["usedBytes"] + disk["freeBytes"] == disk["totalBytes"]
  assert 0 <= disk["percent"] <= 100
