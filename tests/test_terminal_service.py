import json
import os
import threading
import time
import re
import subprocess

import pytest

from modules.terminal.service import TerminalService, dimensions
from test_terminal import FakePty
from test_server import running


@pytest.fixture
def service(tmp_path):
    pytest.importorskip('websockets')
    instance = TerminalService(tmp_path, FakePty)
    instance.start(8765)
    yield instance
    instance.close()


def attach(service, identifier):
    from websockets.sync.client import connect
    _, result = service.request('connect', {'id': identifier}, 8765)
    socket = connect(result['url'], origin='http://127.0.0.1:8765', proxy=None)
    socket.send(json.dumps({'ticket': result['ticket']}))
    return socket, result


def test_websocket_replay_input_resize_and_takeover(service):
    item = service.manager.create()
    item.append('hello 中文')
    first, ticket = attach(service, item.id)
    assert json.loads(first.recv(timeout=3))['text'] == 'hello 中文'
    first.send(json.dumps({'type': 'input', 'text': 'abc'}))
    first.send(json.dumps({'type': 'resize', 'cols': 100, 'rows': 30}))
    deadline = time.monotonic() + 3
    while not item.pty.sizes and time.monotonic() < deadline:
        time.sleep(.01)
    assert item.pty.input == ['abc'] and item.pty.sizes == [(100, 30)]
    second, _ = attach(service, item.id)
    assert json.loads(second.recv(timeout=3))['text'] == 'hello 中文'
    from websockets.exceptions import ConnectionClosed
    with pytest.raises(ConnectionClosed):
        first.recv(timeout=3)
    second.close()
    assert item.running
    third, _ = attach(service, item.id)
    assert json.loads(third.recv(timeout=3))['text'] == 'hello 中文'
    third.close()


def test_bad_tickets_and_origins(service):
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    url = f'ws://127.0.0.1:{service.port}/'
    with pytest.raises(InvalidStatus):
        connect(url, origin='http://evil.example', proxy=None)
    with connect(url, origin='http://localhost:8765', proxy=None) as socket:
        socket.send(json.dumps({'ticket': 'bad'}))
        with pytest.raises(ConnectionClosed):
            socket.recv(timeout=3)


def test_actions_and_validation(service):
    assert service.request('list', {}, 8765)[1]['sessions'] == []
    item = service.request('create', {}, 8765)[1]['session']
    assert service.request('close', {'id': item['id']}, 8765)[1]['ok']
    assert service.request('connect', {}, 8765)[0] == 400
    assert service.request('close', {'id': 'bad'}, 8765)[0] == 400
    for value in ({}, {'cols': True, 'rows': 20}, {'cols': 1001, 'rows': 20}):
        with pytest.raises(ValueError):
            dimensions(value)


@pytest.mark.skipif(os.name != 'nt', reason='Windows ConPTY')
def test_real_powershell_unicode_resize_and_job_cleanup(tmp_path):
    pytest.importorskip('winpty')
    from modules.terminal.pty import WindowsPty
    from modules.terminal.session import Sessions
    manager = Sessions(tmp_path, WindowsPty)
    item = manager.create()
    try:
        item.pty.resize(110, 30)
        item.pty.write("Write-Output ('MARK' + [char]0x4e2d + [char]0x6587)\r")
        deadline = time.monotonic() + 15
        while 'MARK中文' not in item.output(0)[1] and time.monotonic() < deadline:
            time.sleep(.1)
        assert 'MARK中文' in item.output(0)[1]
        assert item.pty.process.isalive()
        item.pty.write("Write-Output ('SLEEP' + 'START'); Start-Sleep -Seconds 60\r")
        deadline = time.monotonic() + 10
        while 'SLEEPSTART' not in item.output(0)[1] and time.monotonic() < deadline:
            time.sleep(.05)
        assert 'SLEEPSTART' in item.output(0)[1]
        interruptedAt = item.output(0)[0]
        item.pty.write('\x03')
        deadline = time.monotonic() + 10
        while 'PS ' not in item.output(interruptedAt)[1] and time.monotonic() < deadline:
            time.sleep(.05)
        assert 'PS ' in item.output(interruptedAt)[1]
        item.pty.write("Write-Output ('AFTER' + (2+3))\r")
        deadline = time.monotonic() + 10
        while 'AFTER5' not in item.output(0)[1] and time.monotonic() < deadline:
            time.sleep(.05)
        assert 'AFTER5' in item.output(0)[1]
        item.pty.write("$p = Start-Process powershell.exe -ArgumentList '-NoProfile','-Command','Start-Sleep 60' -WindowStyle Hidden -PassThru; Write-Output ('CHILD' + $p.Id)\r")
        deadline = time.monotonic() + 10
        child = None
        while child is None and time.monotonic() < deadline:
            child = re.search(r'CHILD(\d+)', item.output(0)[1])
            time.sleep(.05)
        assert child is not None
        childPid = child.group(1)
    finally:
        manager.shutdown()
    assert not item.pty.process.isalive()
    # tasklist 用系統語系編碼輸出（中文 Windows 是 cp950），用 UTF-8 解碼會失敗；PID 是 ASCII，直接比對位元組
    listing = subprocess.run(['tasklist', '/FI', f'PID eq {childPid}', '/FO', 'CSV'], capture_output=True)
    assert f'"{childPid}"'.encode('ascii') not in listing.stdout


def test_http_terminal_routes_require_token(running):
    # Reuse the real HTTP test fixture; all terminal data needs write authorization.
    from test_server import post
    _, port = running
    assert post(port, '/api/terminals/list', withToken='bad')[0] == 403
    status, payload = post(port, '/api/terminals/list')
    from modules.terminal.service import unavailable
    if not unavailable():
        assert status == 200 and payload['sessions'] == []
    else:
        assert status == 400


def test_ticket_is_single_use_and_output_waits_for_ack(service):
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed
    item = service.manager.create()
    item.append('first')
    first, ticket = attach(service, item.id)
    assert json.loads(first.recv(timeout=3))['text'] == 'first'
    item.append('second')
    with pytest.raises(TimeoutError):
        first.recv(timeout=.1)
    first.send(json.dumps({'type': 'ack'}))
    assert json.loads(first.recv(timeout=3))['text'] == 'second'
    with connect(ticket['url'], origin='http://localhost:8765', proxy=None) as second:
        second.send(json.dumps({'ticket': ticket['ticket']}))
        with pytest.raises(ConnectionClosed):
            second.recv(timeout=3)
    first.close()


def test_optional_dependency_and_platform_errors(monkeypatch, tmp_path):
    from modules.terminal import service as module
    monkeypatch.setattr(module.os, 'name', 'posix')
    assert 'Windows' in module.unavailable()
    monkeypatch.setattr(module.os, 'name', 'nt')
    monkeypatch.setattr(module.importlib.util, 'find_spec', lambda name: None)
    assert 'pip install' in module.unavailable()
    instance = TerminalService(tmp_path)
    assert instance.request('list', {}, 8765)[0] == 400
    monkeypatch.setattr(module.importlib.util, 'find_spec', lambda name: True)
    assert module.unavailable() == ''


def test_invalid_input_and_expired_tickets(service):
    item = service.manager.create()
    socket = object()
    with pytest.raises(ValueError):
        service.input(item, socket, {'type': 'input', 'text': 'x'}, False)
    item.client = socket
    for message in ([], {'type': 'bad'}, {'type': 'input', 'text': 12}):
        with pytest.raises(ValueError):
            service.input(item, socket, message, False)
    item.running = False
    assert service.input(item, socket, {'type': 'input', 'text': 'x'}, True)
    assert service.input(item, socket, {'type': 'resize', 'cols': 80, 'rows': 24}, True)
    assert not service.input(item, socket, {'type': 'ack'}, True)
    item.client = None
    class FakeSocket:
        def recv(self, timeout):
            return json.dumps({'ticket': 'expired'})
    service.tickets['expired'] = (item.id, 0)
    with pytest.raises(ValueError):
        service.authenticate(FakeSocket())
    assert service.request('bad', {'id': item.id}, 8765)[0] == 400


@pytest.mark.skipif(os.name != 'nt', reason='Windows APIs')
def test_job_failure_and_shell_start_failure(monkeypatch, tmp_path):
    pytest.importorskip('winpty')
    from modules.terminal import pty
    with pytest.raises(OSError):
        pty.Job(0)
    monkeypatch.setattr(pty.shutil, 'which', lambda name: None)
    with pytest.raises(ValueError, match='PowerShell'):
        pty.WindowsPty(tmp_path)
    monkeypatch.setattr(pty.shutil, 'which', lambda name: 'powershell.exe')
    def fail(pid):
        raise OSError('cannot contain process')
    monkeypatch.setattr(pty, 'Job', fail)
    with pytest.raises(OSError, match='contain'):
        pty.WindowsPty(tmp_path)
