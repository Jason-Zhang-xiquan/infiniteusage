"""免费 agent 能不能独立完成「写代码」子任务：OpenCode build agent + 免费模型，跑阶段 B 的 12 个变异修复任务，
用单元测试判对错（不靠模型自评）。零付费额度。Mac 上串行跑，带内存保护。

用法（Mac）: python3 bench/free_coder.py [--models a,b] [--tasks t01,t02] [--timeout 600]
结果追加到 bench/results/free_coder.jsonl
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_run import ENV, PROMPT, prepare, sh  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from iu.explore import opencode_bin  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "free_coder.jsonl")
TASKS = "/tmp/iu_tasks.json"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def mem_free() -> int:
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    m = re.search(r"free percentage:\s*(\d+)", out)
    return int(m.group(1)) if m else 100


def kill_strays():
    """以前用 pkill -f vitest 清残留，会误杀整台机器上别的 vitest（包括并行跑的 iu delegate）。
    现在只杀自己启动的进程组（见 kill_group），这里不再做全局清理。"""


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_task(task, model, timeout):
    d = prepare(task, "A", 0)
    env = {**ENV, "NODE_OPTIONS": "--max-old-space-size=2048"}
    t0 = time.time()
    p = subprocess.Popen([opencode_bin(), "run", "--agent", "build", "-m", f"opencode/{model}", "--dir", d, PROMPT],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    status = "ok"
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, status = b"", "超时"
    kill_group(p.pid)  # OpenCode 留下的 vitest 等子进程
    secs = time.time() - t0
    kill_strays()
    text = ANSI.sub("", out.decode("utf-8", "replace")).strip()
    code, test_out = sh("cd server && npx vitest run src/__tests__/lib --maxWorkers=2", d, timeout=600)
    kill_strays()
    _, touched = sh("git diff --name-only", d)
    touched = [x for x in touched.split() if x]
    tests_touched = [x for x in touched if "__tests__" in x or ".test." in x]
    fixed_line = open(os.path.join(d, task["file"]), encoding="utf-8").read().split("\n")[task["line"] - 1] == task["old"]
    return {"ts": time.time(), "task": task["id"], "model": model, "status": status, "seconds": round(secs, 1),
            "tests_pass": code == 0 and not tests_touched, "exact_revert": fixed_line, "files_touched": touched,
            "tests_touched": tests_touched, "reply": text[-300:]}


def main():
    tasks = json.load(open(TASKS))
    only = arg("--tasks", "")
    if only:
        tasks = [t for t in tasks if any(t["id"].startswith(x) for x in only.split(","))]
    models = arg("--models", "deepseek-v4.1-flash-free").split(",")
    timeout = int(arg("--timeout", "600"))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    for model in models:
        for t in tasks:
            while mem_free() < 50:
                print("内存不足，等 60 秒", flush=True)
                time.sleep(60)
            r = run_task(t, model, timeout)
            open(OUT, "a").write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"{model} {t['id']}: {'通过' if r['tests_pass'] else '失败'} "
                  f"{r['seconds']}s {r['status']} 改了 {r['files_touched']}", flush=True)


if __name__ == "__main__":
    main()
