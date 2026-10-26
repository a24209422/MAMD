"""把 repo 附的 tdd-crap skill 安裝到 ~/.claude/skills，board.py init --tdd 和 Claude 才找得到它。

用法：python install_skill.py
"""
import shutil
import sys
from pathlib import Path

skillName = "tdd-crap"
sourceDir = Path(__file__).resolve().parent / "skills" / skillName
markerName = ".agent-board-skill"


def install(source, target):
  if target.exists() and not (target / markerName).exists():
    # 沒有標記檔代表是使用者自己放的同名 skill，蓋掉就找不回來
    print(f"{target} 已經有同名的 skill，不是這個 repo 裝的，不覆蓋。")
    print("要改用這個 repo 的版本，先把那個資料夾移到別的地方，再執行一次。")
    return 1
  action = "已更新" if target.exists() else "已安裝"
  if target.exists():
    # 整個換掉，舊版有、新版刪掉的檔案才不會留著
    shutil.rmtree(target)
  shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".quality"))
  (target / markerName).write_text(
    "這個資料夾由 agent-board 的 install_skill.py 產生，請不要直接修改。\n"
    f"更新：在 agent-board 資料夾 git pull 之後，重新執行 python install_skill.py\n來源：{source}\n",
    encoding="utf-8")
  print(f"{action} {skillName}：{target}")
  print(f"品質檢查需要 pytest、coverage、radon：python -m pip install -r {target / 'requirements.txt'}")
  return 0


def main():
  if not sys.stdout.isatty():
    # 輸出被導到檔案或管線時，Windows 預設編碼不是 UTF-8，中文會變亂碼
    sys.stdout.reconfigure(encoding="utf-8")
  return install(sourceDir, Path.home() / ".claude" / "skills" / skillName)


if __name__ == "__main__":
  sys.exit(main())
