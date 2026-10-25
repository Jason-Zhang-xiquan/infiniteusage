"""别的 coding agent 当宿主时，会不会自己读 infiniteusage skill、照着用 iu delegate / iu sum？
宿主：OpenCode + 免费模型（零付费额度）。任务：阶段 B 的 4-bug 冲刺，提示词里**不提** skill，看它自己会不会用。
记录 OpenCode 的 JSON 事件流，统计：加载 skill、调 iu agents / delegate / sum 的次数；再独立复核测试。

用法（Mac）: python3 bench/host_skill.py [--hosts deepseek-v4.1-flash-free,glm-5.3-flash-free] [--sprints S1,S2,S3]
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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ab_run import ENV, PROMPT, sh  # noqa: E402
from ab_sprint import prepare  # noqa: E402
from free_coder import kill_group, mem_free  # noqa: E402
from iu.explore import opencode_bin  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "host_skill.jsonl")
RAW = "/tmp/iu_host"
VERIFY = "cd server && npx vitest run src/__tests__/lib"
AGENTS_MD = os.path.expanduser("~/.config/opencode/AGENTS.md")
RULE = ("Before you start any coding task, load the `infiniteusage` skill and follow it: for a code change that a "
        "command can verify, first run `iu delegate` exactly as the skill describes; run commands with long output "
        "through `iu sum`.\n")
# plain：提示词不提 skill；agents_md：OpenCode 全局说明文件里写一条规则（测完删掉）；mention：提示词里直接点名 skill
VARIANTS = {"plain": PROMPT, "agents_md": PROMPT, "mention": PROMPT + " Use the infiniteusage skill."}


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def calls(raw):
    """只数跑完的 bash 调用（事件流里同一个调用会出现两次；skill 正文里也有「委托成功」字样，不能直接数文本）"""
    out = {"iu_agents": 0, "iu_delegate": 0, "iu_sum": 0, "delegate_ok": 0, "sum_model": 0}
    for line in open(raw, encoding="utf-8", errors="replace"):
        try:
            st = (json.loads(line).get("part") or {}).get("state") or {}
        except ValueError:
            continue
        cmd, res = str((st.get("input") or {}).get("command", "")), str(st.get("output", ""))
        if st.get("status") != "completed" or "run_iu.py" not in cmd:
            continue
        for k in ("agents", "delegate", "sum"):
            if re.search(rf"run_iu\.py\"?\s+{k}\b", cmd):
                out[f"iu_{k}"] += 1
        out["delegate_ok"] += "[iu delegate 委托成功" in res
        out["sum_model"] += res.startswith("[iu 免费模型")
    return out


def run(sprint, host, tasks, rep, variant="plain"):
    d = prepare(sprint, "C0", rep, tasks)
    os.makedirs(RAW, exist_ok=True)
    raw = os.path.join(RAW, f"{sprint}_{host}_{rep}.jsonl")
    env = {**ENV, "NODE_OPTIONS": "--max-old-space-size=2048"}
    t0 = time.time()
    with open(raw, "wb") as f:
        p = subprocess.Popen([opencode_bin(), "run", "--agent", "build", "-m", f"opencode/{host}", "--format", "json",
                              "--dir", d, VARIANTS[variant]], stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, env=env,
                             start_new_session=True)
        try:
            p.wait(timeout=1200)
            status = "ok"
        except subprocess.TimeoutExpired:
            status = "超时"
        kill_group(p.pid)
    wall = round(time.time() - t0, 1)
    text = open(raw, encoding="utf-8", errors="replace").read()
    code, _ = sh(VERIFY, d, timeout=600)
    _, touched = sh("git status --porcelain -- server/src/__tests__", d)
    r = {"ts": time.time(), "sprint": sprint, "host": host, "variant": variant, "rep": rep, "status": status, "wall_s": wall,
         "tests_pass": code == 0, "tests_touched": bool(touched.strip()),
         "skill_loaded": bool(re.search(r'"tool"\s*:\s*"skill"[^\n]{0,400}infiniteusage|infiniteusage[^\n]{0,200}"tool"\s*:\s*"skill"', text)),
         "mentions_skill": text.count("infiniteusage"),
         "tools": {t: text.count(f'"tool":"{t}"') for t in ("skill", "bash", "read", "edit")},
         **calls(raw), "raw": raw}
    open(OUT, "a").write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{sprint} [{variant}] 宿主 {host}: 测试{'全过' if r['tests_pass'] else '没过'} {wall}s 读 skill={r['skill_loaded']}"
          f"（提到 {r['mentions_skill']} 次） agents={r['iu_agents']} delegate={r['iu_delegate']}"
          f"（成功 {r['delegate_ok']}） sum={r['iu_sum']}", flush=True)


def main():
    tasks = {t["id"]: t for t in json.load(open("/tmp/iu_tasks.json"))}
    rep = int(arg("--rep0", "200"))
    variant = arg("--variant", "plain")
    if variant == "agents_md":
        assert not os.path.exists(AGENTS_MD), "已经有 AGENTS.md，不覆盖"
        open(AGENTS_MD, "w").write(RULE)
    try:
        for host in arg("--hosts", "deepseek-v4.1-flash-free").split(","):
            for s in arg("--sprints", "S1,S2,S3").split(","):
                while mem_free() < 50:
                    time.sleep(60)
                rep += 1
                run(s, host, tasks, rep, variant)
    finally:
        if variant == "agents_md" and os.path.exists(AGENTS_MD):
            os.remove(AGENTS_MD)


if __name__ == "__main__":
    main()
