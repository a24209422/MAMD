"""建立儀表板的展示專案：用假 agent 跑，不花用量。

用法：python tests\\fakes\\make_demo.py <要建立的資料夾>
"""
import json
import subprocess
import sys
from pathlib import Path

fakesDir = Path(__file__).resolve().parent
toolDir = fakesDir.parent.parent
planText = """---
id: P0001
created: 2026-10-26
area: [後端/展示, 前端/展示]
---

# 儀表板展示

| 需求 | AI 理解 | 做法（ADR） | 不採用 | 任務 | 前置 | 驗證 |
|---|---|---|---|---|---|---|
| R1 展示儀表板的各種狀態 | T1、T2 同時改 shared.txt，後合併的會衝突；T3 先卡住，重試後完成 | D1 用假 agent 照情境檔跑 | 真的 agent：會花用量 | T1 寫入 shared.txt（一） | | shared.txt 有 T1 的內容 |
| R1 | | D1 | | T2 寫入 shared.txt（二） | | shared.txt 同時有 T1、T2 的內容 |
| R1 | | D1 | | T3 第一次卡住，重試後完成 | T1 | 在網頁按重試後 T3 完成 |
"""
# T1、T2 都改 shared.txt，誰後合併誰就衝突，所以兩個都準備好第二次呼叫時解衝突。
# T3 先卡住，審查第一次不通過；三個任務都掛在 R1 下，假 Codex 退件時寫的 ref R1 才合規則
scenario = {
  "write": {"T1": ["conflict", "resolve"], "T2": ["conflict", "resolve"], "T3": ["blocked", "done"]},
  "review": {"T3": ["fail", "pass"]},
}


def git(projectDir, *args):
  subprocess.run(["git", *args], cwd=projectDir, check=True, capture_output=True)


def main():
  if not sys.stdout.isatty():
    # 輸出被導到檔案或管線時，Windows 預設編碼不是 UTF-8，中文會變亂碼
    sys.stdout.reconfigure(encoding="utf-8")
  if len(sys.argv) != 2:
    print("用法：python tests\\fakes\\make_demo.py <要建立的資料夾>")
    return 2
  projectDir = Path(sys.argv[1]).resolve()
  if projectDir.exists():
    print(f"{projectDir} 已經存在，換一個資料夾")
    return 1
  (projectDir / "plans").mkdir(parents=True)
  (projectDir / ".board").mkdir()
  (projectDir / "README.md").write_text("儀表板展示專案\n", encoding="utf-8")
  (projectDir / ".gitignore").write_text(".board/\nplans/_review/\n__pycache__/\n", encoding="utf-8")
  git(projectDir, "init", "-q", "-b", "main")
  git(projectDir, "config", "user.name", "demo")
  git(projectDir, "config", "user.email", "demo@example.com")
  git(projectDir, "add", ".")
  git(projectDir, "commit", "-q", "-m", "init")
  (projectDir / "plans" / "0001-demo.md").write_text(planText, encoding="utf-8")
  scenarioPath = projectDir / ".board" / "scenario.json"
  scenarioPath.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
  config = {"maxParallel": 2, "maxRounds": 3, "timeoutMinutes": 5, "testCommand": "",
            "claudeCommand": [sys.executable, str(fakesDir / "fake_claude.py")],
            "codexCommand": [sys.executable, str(fakesDir / "fake_codex.py")]}
  (projectDir / ".board" / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
  submitted = subprocess.run([sys.executable, str(toolDir / "board.py"), "--project", str(projectDir), "submit", "P0001"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
  print(submitted.stdout.strip())
  if submitted.returncode != 0:
    return 1
  print(f"""
展示專案：{projectDir}

1. 開第一個 PowerShell，啟動儀表板，用它印出的網址開瀏覽器：
   python {toolDir / 'server.py'} {projectDir}
2. 開第二個 PowerShell，用假 agent 啟動 conductor（每一步慢 4 秒）：
   $env:FAKE_SCENARIO = "{scenarioPath}"
   $env:FAKE_DELAY = "4"
   python {toolDir / 'conductor.py'} {projectDir}
3. 在網頁的「人工介入」核准 P0001，看卡片的變化。
""")
  return 0


if __name__ == "__main__":
  sys.exit(main())
