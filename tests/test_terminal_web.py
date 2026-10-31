import os

import pytest

from browser import browserPages, sharedBrowser
from test_server import serving, token


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
