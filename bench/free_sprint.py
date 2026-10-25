"""端到端测 iu delegate：同一个代码库里同时有 4 个 bug（阶段 B 的冲刺 S1~S3），比单个 bug 更接近真实的子任务。
走真正的 `iu delegate` 命令（快照 → 免费 agent → 验证 → 失败就还原），再独立复核：测试是否全过、测试文件有没有被改。
另外测一次「验证必失败」，确认会把工作区还原干净。零付费额度，Mac 上串行。

用法（Mac）: python3 bench/free_sprint.py [--sprints S1,S2,S3]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_run import ENV, PROMPT, sh  # noqa: E402
from ab_sprint import prepare  # noqa: E402
from free_coder import kill_strays, mem_free  # noqa: E402

IU = os.path.expanduser("~/InfiniteUsage/run_iu.py")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "free_sprint.jsonl")
VERIFY = "cd server && npx vitest run src/__tests__/lib"


def delegate(d, verify):
    t0 = time.time()
    p = subprocess.run(["/usr/bin/python3", IU, "delegate", "--verify", verify, "--budget", "1200", PROMPT], cwd=d,
                       capture_output=True, env=ENV, timeout=2400)
    kill_strays()
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace"), round(time.time() - t0, 1)


def main():
    tasks = {t["id"]: t for t in json.load(open("/tmp/iu_tasks.json"))}
    sprints = (sys.argv[sys.argv.index("--sprints") + 1] if "--sprints" in sys.argv else "S1,S2,S3").split(",")
    for s in sprints:
        while mem_free() < 50:
            time.sleep(60)
        d = prepare(s, "C0", 90, tasks)
        code, out, secs = delegate(d, VERIFY)
        tcode, _ = sh(VERIFY, d, timeout=600)
        kill_strays()
        _, touched = sh("git status --porcelain", d)
        tests_touched = [x for x in touched.split("\n") if "__tests__" in x or ".test." in x]
        r = {"ts": time.time(), "sprint": s, "bugs": 4, "delegate_ok": code == 0, "seconds": secs,
             "tests_pass_after": tcode == 0, "tests_touched": tests_touched, "status": touched.strip()[:400],
             "head": out[:300], "tail": out[-500:]}
        open(OUT, "a").write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{s}: delegate {'成功' if code == 0 else '失败'} {secs}s，复核测试 {'全过' if tcode == 0 else '没过'}，"
              f"测试文件改动 {tests_touched or '无'}", flush=True)
    # 还原测试：验证命令必失败 → 工作区必须和调用前一模一样
    d = prepare("S1", "C0", 91, tasks)
    _, before = sh("git status --porcelain; git diff | shasum", d)
    code, out, secs = delegate(d, "false")
    _, after = sh("git status --porcelain; git diff | shasum", d)
    r = {"ts": time.time(), "sprint": "rollback-test", "delegate_ok": code == 0, "seconds": secs,
         "restored": before == after, "before": before[-200:], "after": after[-200:], "tail": out[-400:]}
    open(OUT, "a").write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"还原测试：delegate {'成功（不应该）' if code == 0 else '失败（应该）'}，工作区"
          f"{'已还原干净' if before == after else '没有还原干净！'}", flush=True)


if __name__ == "__main__":
    main()
