import threading
import time

import pytest

from modules.terminal.session import Sessions


class FakePty:
    def __init__(self, project):
        self.closed = False
        self.input = []
        self.sizes = []

    def read(self):
        time.sleep(0.005)
        if self.closed:
            raise EOFError()
        return ''

    def write(self, text):
        self.input.append(text)

    def resize(self, cols, rows):
        self.sizes.append((cols, rows))

    def close(self):
        self.closed = True


def test_sessions_survive_detach_and_are_independent(tmp_path):
    manager = Sessions(tmp_path, FakePty)
    first = manager.create()
    assert first is not None
    second = manager.create()
    assert first.id != second.id
    first.pty.write('cd foo\r')
    assert not second.pty.input
    manager.close(first.id)
    assert first.pty.closed and not second.pty.closed
    manager.shutdown()
    assert second.pty.closed


def test_history_is_bounded_and_sequence_continues(tmp_path):
    manager = Sessions(tmp_path, FakePty, historyBytes=8)
    item = manager.create()
    assert item is not None
    item.append('abcd')
    item.append('中文')
    sequence, text, truncated = item.output(0)
    assert sequence == 2 and text == '中文' and truncated
    assert item.output(sequence)[1] == ''
    manager.shutdown()


def test_session_limit_and_unknown_close(tmp_path):
    manager = Sessions(tmp_path, FakePty, limit=1)
    assert manager.create() is not None
    with pytest.raises(ValueError, match='上限'):
        manager.create()
    with pytest.raises(ValueError, match='找不到'):
        manager.close('unknown')
    manager.shutdown()
