"""Windows ConPTY adapter with a kill-on-close Job Object."""
import ctypes
from ctypes import wintypes as w
import os
import shutil
import threading


class Limits(ctypes.Structure):
    _fields_ = [('times', ctypes.c_longlong * 2), ('flags', w.DWORD),
                ('minWorking', ctypes.c_size_t), ('maxWorking', ctypes.c_size_t),
                ('processLimit', w.DWORD), ('affinity', ctypes.c_size_t),
                ('priority', w.DWORD), ('scheduling', w.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', Limits), ('io', ctypes.c_ulonglong * 6),
                ('memory', ctypes.c_size_t * 4)]


class Job:
    def __init__(self, pid):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([w.LPVOID, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
            'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
            'CloseHandle': ([w.HANDLE], w.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            method = getattr(self.api, name)
            method.argtypes, method.restype = arguments, result
        self.handle = self.api.CreateJobObjectW(None, None)
        process = self.api.OpenProcess(0x0101, False, pid)
        info = ExtendedLimits()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not (self.handle and process and self.api.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))
                    and self.api.AssignProcessToJobObject(self.handle, process)):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            self.close()
            raise
        finally:
            if process:
                self.api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class WindowsPty:
    def __init__(self, project):
        from winpty import PtyProcess
        shell = shutil.which('pwsh.exe') or shutil.which('powershell.exe')
        if not shell:
            raise ValueError('找不到 PowerShell')
        # Set up containment before allowing commands or user profile code to execute.
        self.process = PtyProcess.spawn([shell, '-NoLogo', '-NoProfile'], cwd=str(project),
                                       env=dict(os.environ), dimensions=(24, 80), backend=0)
        self.lock = threading.Lock()
        self.closed = False
        try:
            self.job = Job(self.process.pid)
        except OSError:
            self.process.close(force=True)
            raise

    def read(self):
        return self.process.read(4096)

    def write(self, text):
        if not self.closed:
            self.process.write(text)

    def resize(self, cols, rows):
        if not self.closed:
            self.process.setwinsize(rows, cols)

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.job.close()
                self.process.close(force=True)
