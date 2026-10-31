import os
import re
import subprocess
import time

import pytest

from browser import browserPages, sharedBrowser
from test_server import serving, token
from test_server_projects import initialized
from test_web_projects import idOf, switchTo

# 有持續輸出的程序在跑，先記下另開的子程序 id，再一直印 TICK
tickingShell = ("$p = Start-Process powershell.exe -ArgumentList '-NoProfile','-Command','Start-Sleep 120' "
                "-WindowStyle Hidden -PassThru; Write-Output ('CHILD' + $p.Id); "
                "$i = 0; while ($true) { Write-Output ('TICK' + $i); $i++; Start-Sleep -Milliseconds 200 }")


def waitOutput(item, pattern, after=0):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        found = re.findall(pattern, item.output(after)[1])
        if found:
            return found
        time.sleep(.05)
    raise AssertionError(f'終端輸出等不到 {pattern}')


def processGone(pid):
    # tasklist 用系統字碼頁輸出中文訊息，比對位元組才不會解碼失敗
    listing = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV'], capture_output=True)
    return f'"{pid}"'.encode() not in listing.stdout


def typeCommand(page, text):
    page.locator('.xterm-helper-textarea').focus()
    page.keyboard.insert_text(text)
    page.keyboard.press('Enter')


@pytest.mark.skipif(os.name != 'nt', reason='Windows ConPTY')
def test_terminals_follow_project_switch_and_server_close(tmp_path, browserPages):
    pytest.importorskip('winpty')
    pytest.importorskip('websockets')
    first, second = initialized(tmp_path / 'alpha'), initialized(tmp_path / 'beta')
    openSocket = "terminals.size === 1 && [...terminals.values()][0].socket?.readyState === 1"
    with serving(first) as http:
        registry = http.RequestHandlerClass.registry
        managerOf = lambda projectDir: registry.get(idOf(projectDir)).dashboard.terminal.manager
        page = browserPages.newContext().new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_viewport_size({'width': 1440, 'height': 900})
        page.goto(f'http://127.0.0.1:{http.server_address[1]}/#token={token}')
        page.click('#terminalNew')
        page.wait_for_function(openSocket)
        firstId = page.evaluate('selectedTerminal')
        firstItem = managerOf(first).get(firstId)
        firstPid = firstItem.pty.process.pid
        typeCommand(page, tickingShell)
        firstChild = waitOutput(firstItem, r'CHILD(\d+)')[0]
        waitOutput(firstItem, r'TICK3\b')

        switchTo(page, str(second), second)
        page.click('#terminalNew')
        page.wait_for_function(openSocket)
        # B 的頁面只有自己的分頁，A 的程序在背景繼續跑
        secondId = page.evaluate('selectedTerminal')
        assert page.evaluate('[...terminals.keys()]') == [secondId] and secondId != firstId
        assert page.locator('.terminal-tab').count() == 1
        secondItem = managerOf(second).get(secondId)
        typeCommand(page, f"Write-Output ('CWDSAME' + ((Get-Location).Path -eq '{second.resolve()}'))")
        assert waitOutput(secondItem, r'CWDSAME(True|False)') == ['True']
        typeCommand(page, "$p = Start-Process powershell.exe -ArgumentList '-NoProfile','-Command','Start-Sleep 120' "
                          "-WindowStyle Hidden -PassThru; Write-Output ('CHILD' + $p.Id)")
        secondChild = waitOutput(secondItem, r'CHILD(\d+)')[0]
        for action in ('connect', 'close'):
            refused = page.evaluate(f"apiPost('/api/terminals/{action}', {{ id: '{firstId}' }})")
            assert refused['ok'] is False
        assert firstItem.pty.process.isalive() and firstItem.running

        # 切回 A：接回同一個程序，輸出接著往下印
        switchTo(page, str(first), first)
        page.wait_for_function(f"terminals.get('{firstId}')?.socket?.readyState === 1 && selectedTerminal === '{firstId}'")
        assert page.evaluate('[...terminals.keys()]') == [firstId]
        assert managerOf(first).get(firstId).pty.process.pid == firstPid
        latest = max(int(tick) for tick in waitOutput(firstItem, r'TICK(\d+)'))
        screen = ("Array.from({length: terminals.get(selectedTerminal).term.buffer.active.length}, (_, i) => "
                  "terminals.get(selectedTerminal).term.buffer.active.getLine(i).translateToString()).join('\\n')")
        page.wait_for_function(f"{screen}.includes('TICK{latest + 3}')", timeout=15000)
        assert 'TICK0' in page.evaluate(screen)
        assert not errors
        page.close()
    # 關閉 server 後兩個專案的 shell 和它們開的子程序都結束
    assert not firstItem.pty.process.isalive() and not secondItem.pty.process.isalive()
    assert processGone(firstChild) and processGone(secondChild)


@pytest.mark.skipif(os.name != 'nt', reason='Windows ConPTY')
def test_terminal_tabs_reload_resize_and_close(gitProject, browserPages):
    pytest.importorskip('winpty')
    pytest.importorskip('websockets')
    with serving(gitProject) as http:
        context = browserPages.newContext()
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_viewport_size({'width': 1440, 'height': 900})
        url = f'http://127.0.0.1:{http.server_address[1]}/#token={token}'
        page.goto(url)
        page.click('#terminalNew')
        page.wait_for_function('terminals.size === 1 && [...terminals.values()][0].socket?.readyState === 1')
        identifier = page.evaluate('selectedTerminal')
        item = http.RequestHandlerClass.dashboard.terminal.manager.get(identifier)
        pid = item.pty.process.pid
        page.locator('.xterm-helper-textarea').focus()
        page.keyboard.type("Write-Output ('BROWSER' + (6*7))")
        page.keyboard.press('Enter')
        page.keyboard.insert_text("Write-Output '瀏覽器中文'")
        page.keyboard.press('Enter')
        visible = "[...terminals.values()][0].term.buffer.active.getLine(0) !== undefined"
        page.wait_for_function(visible)
        page.wait_for_function("Array.from({length: terminals.get(selectedTerminal).term.buffer.active.length}, (_,i) => terminals.get(selectedTerminal).term.buffer.active.getLine(i).translateToString()).join('\\n').includes('BROWSER42')")
        page.click('#terminalNew')
        page.wait_for_function('terminals.size === 2')
        page.evaluate("location.hash = 'models'")
        assert item.pty.process.pid == pid
        page.click('#terminalMax')
        page.wait_for_function("document.querySelector('#chatbar').classList.contains('maximized')")
        assert page.locator('#chatbar').bounding_box()['width'] == 1440
        page.click('.chatbar-head [data-chat-toggle]')
        assert not page.locator('#chatbar').evaluate("element => element.classList.contains('maximized')")
        page.click('.chatbar-strip [data-chat-toggle]')
        page.locator('#terminalResize').focus()
        before = page.locator('#chatbar').bounding_box()['width']
        page.keyboard.press('ArrowLeft')
        assert page.locator('#chatbar').bounding_box()['width'] == before + 20
        page.reload()
        page.wait_for_function('terminals.size === 2 && [...terminals.values()].every(item => item.socket?.readyState === 1)')
        assert http.RequestHandlerClass.dashboard.terminal.manager.get(identifier).pty.process.pid == pid
        page.set_viewport_size({'width': 390, 'height': 844})
        page.evaluate('setTerminalOpen(true)')
        assert page.locator('#chatbar').bounding_box()['width'] == 390
        page.on('dialog', lambda dialog: dialog.accept())
        page.locator('.terminal-tab').first.locator('button').last.click()
        page.wait_for_function('terminals.size === 1')
        assert not item.pty.process.isalive()
        assert not errors
        page.close()
