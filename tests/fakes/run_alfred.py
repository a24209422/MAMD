"""用目標專案交付的 runtime 跑 Alfred，直到指定任務到達指定狀態、而且沒有 agent 還在跑。

換電腦的端到端測試靠它證明：clone 下來的專案只靠自己帶的 runtime 就能重建 worktree、派工並合併成果。
用法：python run_alfred.py <專案資料夾> <plan> <任務> <狀態>
"""
import json
import sys
import time
from pathlib import Path


def main():
  projectDir, planIdValue, taskId, wantedStatus = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
  sys.stdout.reconfigure(encoding="utf-8")
  # 只放 runtime 進搜尋路徑，import 到的一定是專案帶著的版本，不是開發中的原始碼
  sys.path.insert(0, str(projectDir / ".agent-board" / "runtime"))
  import conductor
  alfred = conductor.Conductor(projectDir, out=lambda message: print(message, flush=True))
  key, task = f"{planIdValue}/{taskId}", None
  deadline = time.time() + 90
  while time.time() < deadline:
    alfred.safeTick()
    task = alfred.currentState()[1]["tasks"].get(key)
    # 等 agent 都結束才停，留下的事件才完整，也不會有孤兒程序
    if task and task["status"] == wantedStatus and not alfred.jobs:
      print(json.dumps({"conductor": conductor.__file__}, ensure_ascii=False), flush=True)
      return 0
    time.sleep(alfred.config["pollSeconds"])
  alfred.stopJobs(list(alfred.jobs), recordInterruption=True)
  print(f"等不到 {key} 變成 {wantedStatus}：{task and (task['status'], task['round'], task['needs'])}", flush=True)
  return 1


if __name__ == "__main__":
  sys.exit(main())
