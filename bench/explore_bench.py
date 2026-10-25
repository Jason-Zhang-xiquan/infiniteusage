"""大杠杆 A：把「查代码」这类探索任务整块交给免费 agent（OpenCode + Zen 免费模型，只读的 plan agent）。
不花 Claude 额度。问题都在 FreeLLMAPI（公开 MIT 项目）上，标准答案是关键值 + 文件:行；agent 要自己找到文件。

每题一次一个跑（保护 Mac 内存）：开跑前可用内存 ≥ 40%，每次最多 5 分钟。
评分沿用 run_bench.score（必需内容 + 位置）。
用法（Mac）: python3 bench/explore_bench.py /tmp/iu_explore.jsonl [--models a,b] [--reps 1]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from run_bench import score  # noqa: E402

OC = os.path.expanduser("~/iu_tools/opencode/node_modules/.bin/opencode")
REPO = "/tmp/oc_explore"
ENV = {**os.environ, "PATH": "/opt/homebrew/bin:" + os.environ["PATH"], "NO_COLOR": "1",
       "NODE_OPTIONS": "--max-old-space-size=2048"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

QUESTIONS = [
    ("default_port", "What TCP port and bind host does the server use by default when PORT and HOST are not set?",
     [["3001"], ["::"]], ["config.ts:72"]),
    ("catalog_interval", "How often does the server sync its model catalog, and where is that interval defined?",
     [["12"]], ["catalog-sync.ts:56"]),
    ("kilo_limit", "What anonymous rate limit does the code document for the Kilo gateway's free routes?",
     [["200"]], []),
    ("provider_timeout", "Which environment variable overrides the chat timeout for one provider, and what does 0 do?",
     [["PROVIDER_TIMEOUT_"], ["disable"]], []),
    ("setup_code", "Which function mints the first-run setup code, and in which file and line is it defined?",
     [["generateSetupCode"]], ["setup-code.ts:18"]),
    ("keyless", "Which provider platform ids are registered as keyless (usable without an API key)?",
     [["kilo"], ["ovh"]], []),
    ("parse_budget", "Where is the function parseBudget defined (file and line)?", [["parseBudget"]], ["budget.ts:22"]),
    ("task_header", "What is the name of the HTTP header used to pass the task type, and where is it defined?",
     [["x-freellm-task-type"]], ["task-type.ts:17"]),
    ("min_output_cap", "What is the value of MIN_LEARNED_OUTPUT_CAP and where is it defined?", [["1024"]],
     ["output-cap.ts:33"]),
    ("db_path_env", "Which environment variable changes where the SQLite database file is stored?",
     [["FREEAPI_DB_PATH"]], []),
]
MODELS = ["deepseek-v4.1-flash-free", "longcat-2.5-preview-free", "space-bunny-free"]


def mem_free() -> int:
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=30).stdout
        return int(re.search(r"free percentage:\s*(\d+)", out).group(1))
    except Exception:  # noqa: BLE001
        return 100


def ask(model, question, timeout=300):
    prompt = (f"{question} Look it up in this repository (read-only, do not modify anything). Answer concisely and "
              f"cite every claim as path:line.")
    p = subprocess.Popen([OC, "run", "--agent", "plan", "-m", f"opencode/{model}", "--dir", REPO, prompt],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return ANSI.sub("", out.decode("utf-8", "replace")), p.returncode, False
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        return ANSI.sub("", (out or b"").decode("utf-8", "replace")), -9, True


def main():
    out_path = sys.argv[1]
    models = sys.argv[sys.argv.index("--models") + 1].split(",") if "--models" in sys.argv else MODELS
    reps = int(sys.argv[sys.argv.index("--reps") + 1]) if "--reps" in sys.argv else 1
    if not os.path.exists(REPO):
        subprocess.run(["cp", "-Rc", "/tmp/flbench_clean", REPO], check=True)
    for rep in range(1, reps + 1):
        for qid, q, req, locs in QUESTIONS:
            for m in models:
                while mem_free() < 40:
                    time.sleep(30)
                t0 = time.time()
                text, code, timed_out = ask(m, q)
                gold = {"kind": "sum", "required": req, "locations": locs}
                r, h, miss = score(gold, text, text)  # 位置是否真实另由 file:line 检查（见下）
                fake = []
                for name, line in re.findall(r"([\w\-.]+\.ts)[:(](\d+)", text):
                    hits = subprocess.run(["find", REPO + "/server/src", "-name", name], capture_output=True,
                                          text=True).stdout.split()
                    if not hits or not any(int(line) <= sum(1 for _ in open(h, errors="replace")) for h in hits):
                        fake.append(f"{name}:{line}")
                rec = {"q": qid, "model": m, "rep": rep, "score": r, "missing": miss, "fake_refs": fake,
                       "seconds": round(time.time() - t0, 1), "exit": code, "timed_out": timed_out,
                       "error": ("Error:" in text and r == 0), "chars": len(text), "answer": text[-1500:]}
                with open(out_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"r{rep} {qid:<18} {m:<28} score {r:.2f} fake {len(fake)} {rec['seconds']}s "
                      f"{'TIMEOUT' if timed_out else ''}{'ERR' if rec['error'] else ''}", flush=True)
    shutil.rmtree(REPO, ignore_errors=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
