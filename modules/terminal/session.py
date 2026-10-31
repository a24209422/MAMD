"""Persistent terminal sessions and bounded output, independent of browser lifetime."""
from collections import deque
import secrets
import threading


class Session:
    def __init__(self, identifier, title, pty, historyBytes):
        self.id, self.title, self.pty = identifier, title, pty
        self.historyBytes = historyBytes
        self.history = deque()
        self.bytes = self.sequence = 0
        self.running = True
        self.lock = threading.RLock()
        self.client = None
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def append(self, text):
        with self.lock:
            # Split large reads at Unicode boundaries; retain at least one bounded chunk.
            chunkSize = len(text) if len(text.encode('utf-8')) <= self.historyBytes else max(1, self.historyBytes // 4)
            for offset in range(0, len(text), chunkSize):
                part = text[offset:offset + chunkSize]
                self.sequence += 1
                self.history.append((self.sequence, part))
                self.bytes += len(part.encode('utf-8'))
            while self.bytes > self.historyBytes and self.history:
                self.bytes -= len(self.history.popleft()[1].encode('utf-8'))

    def output(self, after):
        with self.lock:
            truncated = bool(self.history and after < self.history[0][0] - 1)
            return self.sequence, ''.join(text for seq, text in self.history if seq > after), truncated

    def read(self):
        try:
            while self.running:
                text = self.pty.read()
                if text:
                    self.append(text)
        except (EOFError, OSError):
            pass
        finally:
            self.running = False
            self.pty.close()

    def view(self):
        return dict(id=self.id, title=self.title, running=self.running)

    def close(self):
        self.running = False
        if self.client:
            self.client.close()
        self.pty.close()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=3)


class Sessions:
    def __init__(self, project, factory, limit=8, historyBytes=1024 * 1024):
        self.project, self.factory = project, factory
        self.limit, self.historyBytes = limit, historyBytes
        self.items = {}
        self.lock = threading.RLock()
        self.counter = 0
        self.closed = False

    def create(self):
        with self.lock:
            if self.closed:
                raise ValueError('終端機服務已結束')
            if len(self.items) >= self.limit:
                raise ValueError('終端分頁上限為 8 個，請先關閉不需要的分頁')
            self.counter += 1
            item = Session(secrets.token_hex(16), f'PowerShell {self.counter}',
                           self.factory(self.project), self.historyBytes)
            self.items[item.id] = item
            return item

    def get(self, identifier):
        with self.lock:
            if identifier not in self.items:
                raise ValueError('找不到終端工作階段')
            return self.items[identifier]

    def list(self):
        with self.lock:
            return [item.view() for item in self.items.values()]

    def close(self, identifier):
        with self.lock:
            item = self.get(identifier)
            del self.items[identifier]
        item.close()

    def shutdown(self):
        with self.lock:
            self.closed = True
            items = list(self.items.values())
            self.items.clear()
        for item in items:
            item.close()
