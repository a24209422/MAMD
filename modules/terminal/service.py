"""Authenticated local WebSocket transport, loaded only when terminal support is used."""
import importlib.util
import json
import os
import secrets
import threading
import time
import sys
from pathlib import Path

from .session import Sessions


def unavailable():
    if os.name != 'nt':
        return '終端機目前支援 Windows'
    if any(importlib.util.find_spec(name) is None for name in ('winpty', 'websockets')):
        requirements = Path(__file__).resolve().parents[2] / 'requirements-terminal.txt'
        return f'尚未安裝終端套件；請執行：& "{sys.executable}" -m pip install -r "{requirements}"'
    return ''


def dimensions(message):
    cols, rows = message.get('cols'), message.get('rows')
    if type(cols) is not int or type(rows) is not int or not (2 <= cols <= 1000 and 1 <= rows <= 500):
        raise ValueError('終端尺寸不正確')
    return cols, rows


class TerminalService:
    def __init__(self, project, factory=None):
        self.project, self.factory = project, factory
        self.manager = None
        self.server = None
        self.thread = None
        self.tickets = {}
        self.lock = threading.RLock()
        self.port = None
        self.closed = False

    def start(self, httpPort):
        with self.lock:
            if self.closed:
                raise ValueError('終端機服務已結束')
            if self.manager:
                return
            if not self.factory and unavailable():
                raise ValueError(unavailable())
            from websockets.sync.server import serve
            from .pty import WindowsPty
            origins = [f'http://{host}:{httpPort}' for host in ('127.0.0.1', 'localhost')]
            self.server = serve(self.handle, '127.0.0.1', 0, origins=origins, max_size=65536,
                                compression=None, open_timeout=5, close_timeout=1)
            self.port = self.server.socket.getsockname()[1]
            self.manager = Sessions(self.project, self.factory or WindowsPty)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()

    def request(self, action, body, httpPort):
        try:
            self.start(httpPort)
            return 200, self.action(action, body)
        except (ValueError, OSError) as error:
            return 400, dict(ok=False, message=str(error))

    def action(self, action, body):
        if action == 'list':
            return dict(ok=True, sessions=self.manager.list())
        if action == 'create':
            return dict(ok=True, session=self.manager.create().view())
        identifier = body.get('id')
        if not isinstance(identifier, str):
            raise ValueError('缺少終端工作階段 id')
        item = self.manager.get(identifier)
        if action == 'close':
            self.manager.close(identifier)
            return dict(ok=True)
        if action != 'connect':
            raise ValueError('未知終端操作')
        ticket = secrets.token_urlsafe(32)
        with self.lock:
            self.tickets = {key: value for key, value in self.tickets.items() if value[1] > time.monotonic()}
            # Bound abandoned connect attempts too.
            if len(self.tickets) >= 64:
                raise ValueError('連接請求過多，請稍後重試')
            self.tickets[ticket] = (item.id, time.monotonic() + 30)
        return dict(ok=True, url=f'ws://127.0.0.1:{self.port}/', ticket=ticket)

    def authenticate(self, socket):
        message = json.loads(socket.recv(timeout=5))
        if not isinstance(message, dict) or not isinstance(message.get('ticket'), str):
            raise ValueError('票證不正確')
        with self.lock:
            entry = self.tickets.pop(message['ticket'], None)
        if not entry or entry[1] < time.monotonic():
            raise ValueError('票證已失效')
        return self.manager.get(entry[0])

    def handle(self, socket):
        from websockets.exceptions import ConnectionClosed
        item = None
        try:
            item = self.authenticate(socket)
            with item.lock:
                previous, item.client = item.client, socket
            if previous:
                previous.close(4001, '已由另一視窗接管')
            self.stream(socket, item)
        except (ValueError, TypeError, OSError, TimeoutError, ConnectionClosed):
            socket.close(1008, '終端連線已結束或資料不正確')
        finally:
            if item:
                with item.lock:
                    if item.client is socket:
                        item.client = None

    def stream(self, socket, item):
        sequence, waiting, finished = 0, False, False
        while item.client is socket and not self.closed:
            if not waiting:
                end, text, truncated = item.output(sequence)
                if end != sequence or (not item.running and not finished):
                    socket.send(json.dumps(dict(type='output', text=text, sequence=end,
                                                truncated=truncated, running=item.running)))
                    sequence, waiting = end, True
                    finished = not item.running
            try:
                message = json.loads(socket.recv(timeout=0.05))
                waiting = self.input(item, socket, message, waiting)
            except TimeoutError:
                continue

    def input(self, item, socket, message, waiting):
        if not isinstance(message, dict):
            raise ValueError('訊息需為物件')
        # Check ownership under the same lock used by connection takeover.
        with item.lock:
            if item.client is not socket:
                raise ValueError('連線已被接管')
            kind = message.get('type')
            if kind == 'ack':
                return False
            if kind == 'input' and isinstance(message.get('text'), str):
                if item.running:
                    item.pty.write(message['text'])
            elif kind == 'resize':
                cols, rows = dimensions(message)
                if item.running:
                    item.pty.resize(cols, rows)
            else:
                raise ValueError('未知終端訊息')
        return waiting

    def close(self):
        with self.lock:
            self.closed = True
            self.tickets.clear()
        if self.manager:
            self.manager.shutdown()
        if self.server:
            self.server.shutdown()
        if self.thread:
            self.thread.join(timeout=3)
