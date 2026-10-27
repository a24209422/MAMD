"""採集主機的 CPU、RAM 與專案所在磁碟用量；只用標準庫，Windows 以 ctypes 呼叫系統 API，不啟動外部程序。

採樣由請求驅動：有人讀取才採樣，同一個 Collector 的結果快取 intervalSeconds 秒，多個頁面共用同一份結果與 CPU 基準。
"""
import copy
import ctypes
import functools
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

intervalSeconds = 3
# CPU 基準太舊時，差分會變成這段期間的平均，不能當成「目前」的使用率
maxCpuBaseAge = 30
maxSystemTimesProcessors = 64
allProcessorGroups = 0xFFFF
isWindows = os.name == "nt"
cpuFields = ("percent",)
memoryFields = ("totalBytes", "usedBytes", "availableBytes", "percent")
diskFields = ("totalBytes", "usedBytes", "freeBytes", "percent")


class Unsupported(Exception):
  """這台主機無法提供某項數據；和暫時讀不到不同，不會自己恢復。"""


class MemoryStatus(ctypes.Structure):
  # MEMORYSTATUSEX
  _fields_ = [("length", ctypes.c_uint32), ("memoryLoad", ctypes.c_uint32),
              ("totalPhys", ctypes.c_uint64), ("availPhys", ctypes.c_uint64),
              ("totalPageFile", ctypes.c_uint64), ("availPageFile", ctypes.c_uint64),
              ("totalVirtual", ctypes.c_uint64), ("availVirtual", ctypes.c_uint64),
              ("availExtendedVirtual", ctypes.c_uint64)]


@functools.cache
def kernel32():
  # 用自己的 WinDLL，設定 argtypes 不會影響其他地方的 ctypes.windll.kernel32
  library = ctypes.WinDLL("kernel32", use_last_error=True)
  library.GetSystemTimes.argtypes = [ctypes.POINTER(ctypes.c_uint64)] * 3
  library.GetSystemTimes.restype = ctypes.c_int
  library.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
  library.GlobalMemoryStatusEx.restype = ctypes.c_int
  library.GetActiveProcessorCount.argtypes = [ctypes.c_uint16]
  library.GetActiveProcessorCount.restype = ctypes.c_uint32
  library.GetActiveProcessorGroupCount.argtypes = []
  library.GetActiveProcessorGroupCount.restype = ctypes.c_uint16
  return library


def requireWindows():
  if not isWindows:
    raise Unsupported("第一版只支援 Windows 主機")


def processorLayout():
  library = kernel32()
  processors = library.GetActiveProcessorCount(allProcessorGroups)
  if not processors:
    raise ctypes.WinError(ctypes.get_last_error())
  return processors, library.GetActiveProcessorGroupCount()


def windowsCpuTimes():
  requireWindows()
  processors, groups = processorLayout()
  # 超過 64 個邏輯處理器時，GetSystemTimes 只涵蓋呼叫端所在的 processor group，不是整機
  if processors > maxSystemTimesProcessors or groups > 1:
    raise Unsupported(f"這台主機有 {processors} 個邏輯處理器（{groups} 個處理器群組），GetSystemTimes 只涵蓋"
                      f" {maxSystemTimesProcessors} 個以內的單一群組，第一版不顯示整機 CPU")
  idle, kernel, user = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
  if not kernel32().GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
    raise ctypes.WinError(ctypes.get_last_error())
  return idle.value, kernel.value, user.value


def windowsMemory():
  requireWindows()
  status = MemoryStatus(length=ctypes.sizeof(MemoryStatus))
  if not kernel32().GlobalMemoryStatusEx(ctypes.byref(status)):
    raise ctypes.WinError(ctypes.get_last_error())
  return status.totalPhys, status.availPhys


def volumeLabel(path):
  drive = os.path.splitdrive(str(path))[0]
  if drive:
    return drive
  current = path
  while not os.path.ismount(current) and current.parent != current:
    current = current.parent
  return str(current)


def percent(part, whole):
  return round(min(max(part / whole * 100, 0), 100), 1)


def blankItem(status, updatedAt, message, fields):
  # 只有 ok 帶數值；其他狀態一律是 null，畫面才不會把它當成 0%
  return {"status": status, "updatedAt": updatedAt, "message": message, **dict.fromkeys(fields)}


class Collector:
  def __init__(self, projectDir, readCpuTimes=None, readMemory=None, readDisk=None,
               clock=time.monotonic, now=None, ttl=intervalSeconds):
    self.projectDir = Path(projectDir).resolve()
    self.volume = volumeLabel(self.projectDir)
    self.readCpuTimes = readCpuTimes or windowsCpuTimes
    self.readMemory = readMemory or windowsMemory
    self.readDisk = readDisk or shutil.disk_usage
    self.clock = clock
    self.now = now or (lambda: datetime.now().astimezone())
    self.ttl = ttl
    self.lock = threading.Lock()
    self.sampleId = 0
    self.cached = None
    self.cpuBase = None

  def snapshot(self):
    # 同時進來的請求排隊等同一次採樣，CPU 基準只被推進一次
    with self.lock:
      clockTime = self.clock()
      if self.cached is None or not 0 <= clockTime - self.cached[0] < self.ttl:
        self.cached = (clockTime, self.sample(clockTime))
      return copy.deepcopy(self.cached[1])

  def sample(self, clockTime):
    updatedAt = self.now().isoformat(timespec="milliseconds")
    self.sampleId += 1
    return {
      "sampleId": self.sampleId, "sampledAt": updatedAt, "intervalSeconds": intervalSeconds,
      "cpu": self.measure("CPU 資料", cpuFields, updatedAt, lambda: self.cpu(clockTime)),
      "memory": self.measure("RAM 資料", memoryFields, updatedAt, self.memory),
      "disk": {**self.measure("磁碟資料", diskFields, updatedAt, self.disk), "volume": self.volume},
    }

  def measure(self, label, fields, updatedAt, read):
    # 單項失敗只影響自己，其他項目照常回傳
    try:
      values = read()
    except Unsupported as error:
      return blankItem("unsupported", updatedAt, str(error), fields)
    except Exception as error:
      return blankItem("error", updatedAt, f"{label}暫時無法取得：{error}", fields)
    if values is None:
      return blankItem("warming-up", updatedAt, "取樣中", fields)
    return {"status": "ok", "updatedAt": updatedAt, "message": None, **values}

  def cpu(self, clockTime):
    try:
      idle, kernel, user = self.readCpuTimes()
    except Unsupported:
      self.cpuBase = None
      raise
    base, self.cpuBase = self.cpuBase, (clockTime, idle, kernel, user)
    # 第一次只有基準值；距離上次太久或計數倒退時，以這次重建基準，不等待下一次取樣
    if base is None or not 0 < clockTime - base[0] <= maxCpuBaseAge:
      return None
    idleDelta, kernelDelta, userDelta = idle - base[1], kernel - base[2], user - base[3]
    # kernel 時間已經包含 idle，總時間不能再加一次 idle
    total = kernelDelta + userDelta
    if min(idleDelta, userDelta) < 0 or idleDelta > kernelDelta or total <= 0:
      return None
    return {"percent": percent(total - idleDelta, total)}

  def memory(self):
    total, available = self.readMemory()
    if total <= 0 or not 0 <= available <= total:
      raise ValueError(f"系統回傳的容量不合理（總量 {total}、可用 {available}）")
    used = total - available
    return {"totalBytes": total, "usedBytes": used, "availableBytes": available, "percent": percent(used, total)}

  def disk(self):
    total, used, free = self.readDisk(self.projectDir)
    if total <= 0 or not 0 <= free <= total or not 0 <= used <= total:
      raise ValueError(f"系統回傳的容量不合理（總量 {total}、已用 {used}、剩餘 {free}）")
    return {"totalBytes": total, "usedBytes": used, "freeBytes": free, "percent": percent(used, total)}
