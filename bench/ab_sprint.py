"""阶段 B v2：长会话「冲刺」A/B（设计见 docs/PLAN_phase_B_v2.md）。

每个冲刺：FreeLLMAPI（公开 MIT 项目）干净副本里注入 4 个 bug（提交进 git，看不出改了哪），加 TASK.md 里 2 个查代码的问题。
组：
  C0 基线           一个会话做完
  C1 交接+新会话    每个会话只做一项、写 HANDOFF.md 就结束，下一个全新会话接着做；完成（评分通过）或 7 个会话为止
  C2 外包 hook v2   基线 + iu PreToolUse hook（带 || true）
评分：lib 单元测试全过 + 没改测试文件 + ANSWERS.md 里有两个问题的答案关键字

用法（Mac）:
  python3 ab_sprint.py selftest                       用「直接打补丁的假 agent」验证评分器
  python3 ab_sprint.py run OUT.jsonl --sprints S1 --arms C0 --reps 1      试跑一次
  python3 ab_sprint.py run OUT.jsonl --sprints S1,S2,S3 --arms C0,C1,C2 --reps 2
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

CLEAN = "/tmp/flbench_clean"
TASKS = "/tmp/iu_tasks.json"
ROOT = {"C0": "/tmp/iu_sprC0", "C1": "/tmp/iu_sprC1", "C2": "/tmp/iu_sprC2",
        "P": "/tmp/iu_sprP", "E": "/tmp/iu_sprE", "O": "/tmp/iu_sprO",
        "OPUS": "/tmp/iu_sprOPUS", "HAI": "/tmp/iu_sprHAI", "OPUS_E": "/tmp/iu_sprOPUSE"}
# 主线程换模型（大杠杆 B）：C0 = Sonnet；OPUS / HAI = 同样的任务换成 Opus / Haiku；OPUS_E = Opus + --effort medium
ARM_MODEL = {"OPUS": "opus", "HAI": "haiku", "OPUS_E": "opus", "OPUS_W": "opus"}
# 提前压缩（大杠杆 E，Claude Code 自带：先清旧工具输出，再总结）。默认窗口约 967K，几乎不触发；
# 冲刺任务上下文只有 7~16 万，所以用允许的最小值 100K 才测得到
ROOT.update({"W": "/tmp/iu_sprW", "OPUS_W": "/tmp/iu_sprOPUSW", "X": "/tmp/iu_sprX", "OPUS_X": "/tmp/iu_sprOPUSX"})
# 探索外包（大杠杆 A）：告诉 agent 查代码先问 iu explore（OpenCode 免费模型），再按行号核对
EXPLORE_RULE = (
    "IMPORTANT cost rule (mandatory): reading and searching files yourself is expensive; a free helper does code "
    "exploration for you. You MUST use it (1) for every question you are asked about the codebase, and (2) before "
    "you search for or read any code you have not seen yet in this session (where something is defined, how it works, "
    "which file handles X). Run: "
    f"/usr/bin/python3 {os.path.expanduser('~/InfiniteUsage/run_iu.py')} explore \"<one specific question>\" "
    "It takes 10-60 seconds and returns an answer with path:line citations from a free model. Then read ONLY the "
    "cited lines (Read with offset/limit) to verify before you edit or answer; never read whole files when it gave "
    "you lines. Ask one focused question per call. Only if it says it could not answer, search yourself.")
ARM_MODEL.update({"OPUS_X": "opus"})
LIMIT_RETRIES, LIMIT_WAIT = 8, 1800  # 撞上用量上限时（花费 $0、只有 1 轮）等 30 分钟重试，最多 8 次
# 内存保护（2026-10-18 四个会话并行把 Mac 内存吃满之后加的）
MEM_START_MIN = 50   # 可用内存低于这个百分比就先不开跑
MEM_KILL_BELOW = 25  # 运行中低于这个百分比，立刻杀掉这次运行
VITEST_WORKERS = 2
# 新方法组（research/10_profiles_and_unused_tools.md）：
#   P  ponytail：把它的 SKILL.md 追加到系统提示（插件本身也是会话开始时强制注入，效果相同）
#   E  降推理强度：--effort medium
#   O  调小内置输出上限：Bash 8000 字符（默认 30000）、读文件 6000 token（默认 25000）
PONYTAIL = "/tmp/ponytail/skills/ponytail/SKILL.md"
ARM_ARGS = {"P": ["--append-system-prompt-file", PONYTAIL], "E": ["--effort", "medium"],
            "OPUS_E": ["--effort", "medium"]}
ARM_ARGS_LATE = {"X": lambda: ["--append-system-prompt", EXPLORE_RULE],
                 "OPUS_X": lambda: ["--append-system-prompt", EXPLORE_RULE]}
ARM_ENV = {"O": {"BASH_MAX_OUTPUT_LENGTH": "8000", "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS": "6000"},
           "W": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000"},
           "OPUS_W": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000"}}
TRANSCRIPTS = "/tmp/iu_spr_transcripts"
IU = os.path.expanduser("~/InfiniteUsage/run_iu.py")
PILOT = os.path.expanduser("~/.infiniteusage/pilot_projects.txt")
HOOK_LOG = os.path.expanduser("~/.infiniteusage/hook.jsonl")
OFFLOAD_LOG = os.path.expanduser("~/.infiniteusage/offload.jsonl")
ENV = {**os.environ, "PATH": os.path.expanduser("~/.local/bin") + ":/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:"
       + os.environ["PATH"], "NO_COLOR": "1", "CI": "1", "NODE_OPTIONS": "--max-old-space-size=2048"}


def mem_free() -> int:
    """macOS 的「可用内存百分比」（memory_pressure 最后一行）"""
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=30).stdout
        return int(re.search(r"free percentage:\s*(\d+)", out).group(1))
    except Exception:  # noqa: BLE001
        return 100


def wait_for_memory(minimum=MEM_START_MIN, max_wait=1800) -> bool:
    t0 = time.time()
    while mem_free() < minimum:
        if time.time() - t0 > max_wait:
            return False
        time.sleep(30)
    return True


def kill_orphans(d: str):
    """杀掉工作目录在 d 下面的 vitest / node 进程（agent 跑的 watch 模式、超时留下的子进程）"""
    out = subprocess.run(["pgrep", "-f", "vitest"], capture_output=True, text=True).stdout.split()
    real = os.path.realpath(d)
    for pid in out:
        cwd = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
        if real in cwd or d in cwd:
            subprocess.run(["kill", "-9", pid], capture_output=True)
MODEL = "sonnet"
MAX_SESSIONS = 7

SPRINTS = {
    "S1": {"bugs": ["t01_key-parser", "t05_tool-validate", "t08_output-cap", "t11_header-value"],
           "questions": ["What TCP port and what bind address (host) does the server use by default when the PORT and "
                         "HOST environment variables are not set?"],
           "answers": [["3001"], ["'::'", "\"::\"", "`::`", " :: "]]},
    "S2": {"bugs": ["t02_think-tags", "t06_url-guard", "t09_provider-size-parser", "t12_log-redaction"],
           "questions": ["How often does the server sync its model catalog?",
                         "What anonymous rate limit does the code document for the Kilo gateway's free routes?"],
           "answers": [["12 hours", "12h", "12 * 60 * 60", "twice daily", "twice a day", "12 小时", "12小时"], ["200"]]},
    "S3": {"bugs": ["t03_tool-args", "t04_tool-call-rescue", "t07_sampling-params", "t10_served-model"],
           "questions": ["Which environment variable overrides the chat timeout for a single provider, and what does "
                         "setting it to 0 do?",
                         "Which function mints the first-run setup code, and in which file?"],
           "answers": [["PROVIDER_TIMEOUT_"], ["generateSetupCode"]]},
}

ONE_SHOT = "Complete all the work described in TASK.md at the repository root. When you are done, reply with one sentence."
FIRST = ("Read TASK.md at the repository root. Work on exactly ONE unfinished item: fix ONE bug (one root cause), or "
         "answer the questions. When that one item is done and verified, create or update HANDOFF.md at the repository "
         "root with: what is done (with file:line), important findings, and what remains. Then stop and reply with one "
         "line. If every item in TASK.md is already done and verified, reply exactly: ALL DONE")
NEXT = "Read HANDOFF.md and TASK.md at the repository root first. " + FIRST.split("Read TASK.md at the repository root. ", 1)[1]
lock = threading.Lock()


def sh(cmd, cwd, timeout=900):
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, env=ENV, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def task_md(sprint):
    qs = "\n".join(f"   - {q}" for q in SPRINTS[sprint]["questions"])
    return ("# Task\n\n"
            "1. There are several independent bugs in `server/src/lib` that make unit tests under "
            "`server/src/__tests__/lib` fail. Fix all of them in the source code only (do not modify any test files). "
            "Verify with `cd server && npx vitest run src/__tests__/lib`.\n"
            "2. Answer these questions about the codebase in `ANSWERS.md` at the repository root, citing file:line:\n"
            f"{qs}\n\nDone when all lib unit tests pass and ANSWERS.md answers every question.\n")


def prepare(sprint, arm, rep, tasks):
    d = os.path.join(ROOT[arm], f"{sprint}_r{rep}")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(ROOT[arm], exist_ok=True)
    subprocess.run(["cp", "-Rc", CLEAN, d], check=True)
    cfg = os.path.join(d, "server", "vitest.config.ts")  # 限制 vitest 的 worker 数，避免吃满内存
    s = open(cfg, encoding="utf-8").read()
    if "maxWorkers" not in s:
        open(cfg, "w", encoding="utf-8").write(s.replace("environment: 'node',",
                                                         f"environment: 'node',\n    maxWorkers: {VITEST_WORKERS},", 1))
    for tid in SPRINTS[sprint]["bugs"]:
        t = tasks[tid]
        path = os.path.join(d, t["file"])
        lines = open(path, encoding="utf-8").read().split("\n")
        assert lines[t["line"] - 1] == t["old"], (tid, "源码和任务不一致")
        lines[t["line"] - 1] = t["new"]
        open(path, "w", encoding="utf-8").write("\n".join(lines))
    open(os.path.join(d, "TASK.md"), "w").write(task_md(sprint))
    sh("git add -A server/src/lib server/vitest.config.ts TASK.md && "
       "git -c user.email=bench@local -c user.name=bench commit -qm 'chore: snapshot'", d)
    settings = {}
    if arm == "C2":
        settings = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"/usr/bin/python3 {IU} hook-pre || true", "timeout": 10}]}]}}
    elif arm in ("X", "OPUS_X"):  # 探索闸门：自己搜代码会被拒，改用 iu explore
        settings = {"hooks": {"PreToolUse": [{"matcher": "Bash|Grep|Glob", "hooks": [
            {"type": "command", "command": f"/usr/bin/python3 {IU} hook-explore-gate || true", "timeout": 10}]}]}}
    os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
    json.dump(settings, open(os.path.join(d, ".claude", "settings.local.json"), "w"))
    return d


def grade(sprint, d):
    code, _ = sh("cd server && npx vitest run src/__tests__/lib", d)
    _, touched = sh("git status --porcelain -- server/src/__tests__", d)
    try:
        ans = open(os.path.join(d, "ANSWERS.md"), encoding="utf-8").read()
    except OSError:
        ans = ""
    low = ans.lower()
    answers_ok = [any(a.lower() in low for a in group) for group in SPRINTS[sprint]["answers"]]
    tests_ok = code == 0 and not touched.strip()
    return {"tests_pass": code == 0, "tests_touched": bool(touched.strip()), "answers_ok": answers_ok,
            "success": tests_ok and all(answers_ok)}


def parse(transcript, acc):
    """累加一个会话的用量到 acc"""
    seen = set()
    for line in open(transcript, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "assistant":
            msg = d.get("message") or {}
            for c in msg.get("content") or []:
                if c.get("type") == "tool_use":
                    acc["tools"][c.get("name")] = acc["tools"].get(c.get("name"), 0) + 1
                    inp = c.get("input") or {}
                    if c.get("name") == "Bash":
                        acc["bash"].append(inp.get("command", "")[:200])
                    if c.get("name") == "Read" and ".infiniteusage/raw" in str(inp.get("file_path", "")):
                        acc["reads_raw"] += 1
            u, mid = msg.get("usage"), msg.get("id")
            if u and mid not in seen:
                seen.add(mid)
                for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
                    acc["usage"][k] = acc["usage"].get(k, 0) + (u.get(k) or 0)
                ctx = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + \
                      (u.get("cache_read_input_tokens") or 0)
                acc["max_ctx"] = max(acc["max_ctx"], ctx)
        elif d.get("type") == "system" and d.get("subtype") in ("compact_boundary", "compaction"):
            acc["compactions"] = acc.get("compactions", 0) + 1
        elif d.get("type") == "result":
            acc["cost_usd"] += d.get("total_cost_usd") or 0
            acc["turns"] += d.get("num_turns") or 0
            acc["last_result"] = (d.get("result") or "")[:300]


def run_claude(prompt, d, tr, max_turns, arm="", stats=None):
    """跑一个无头会话。运行中每 10 秒看一次内存，可用内存跌破 MEM_KILL_BELOW% 就杀掉整个进程组。
    返回 True 表示超时或因内存被中止"""
    stats = stats if stats is not None else {}
    with open(tr, "w") as f:
        p = subprocess.Popen(["claude", "-p", prompt, "--model", ARM_MODEL.get(arm, MODEL), "--output-format",
                              "stream-json", "--verbose", "--allowedTools=Bash,Read,Edit,Write,Glob,Grep",
                              "--max-turns", str(max_turns), *ARM_ARGS.get(arm, []),
                              *(ARM_ARGS_LATE[arm]() if arm in ARM_ARGS_LATE else [])],
                             cwd=d, stdout=f, stderr=subprocess.DEVNULL, env={**ENV, **ARM_ENV.get(arm, {})},
                             start_new_session=True)
        t0 = time.time()
        while p.poll() is None:
            time.sleep(10)
            free = mem_free()
            stats["mem_min"] = min(stats.get("mem_min", 100), free)
            if free < MEM_KILL_BELOW or time.time() - t0 > 3600:
                stats["aborted"] = "memory" if free < MEM_KILL_BELOW else "timeout"
                os.killpg(p.pid, 9)
                kill_orphans(d)
                p.wait()
                print(f"!! 中止 {d}：{stats['aborted']}（可用内存 {free}%）", flush=True)
                return True
        return False


def run_one(sprint, arm, rep, tasks, out_path):
    if not wait_for_memory():
        print(f"!! 跳过 {sprint} r{rep} {arm}：等了 30 分钟可用内存还不到 {MEM_START_MIN}%", flush=True)
        return None
    d = prepare(sprint, arm, rep, tasks)
    os.makedirs(TRANSCRIPTS, exist_ok=True)
    acc = {"usage": {}, "cost_usd": 0.0, "turns": 0, "tools": {}, "bash": [], "reads_raw": 0, "max_ctx": 0,
           "last_result": ""}
    stats = {"mem_start": mem_free()}
    t0, sessions, timed_out = time.time(), 0, False
    if arm == "C1":
        for s in range(1, MAX_SESSIONS + 1):
            sessions = s
            tr = os.path.join(TRANSCRIPTS, f"{sprint}_r{rep}_{arm}_s{s}.jsonl")
            timed_out |= run_claude(FIRST if s == 1 else NEXT, d, tr, 40, arm, stats)
            parse(tr, acc)
            if stats.get("aborted") or "ALL DONE" in acc["last_result"] or grade(sprint, d)["success"]:
                break
    else:
        sessions = 1
        tr = os.path.join(TRANSCRIPTS, f"{sprint}_r{rep}_{arm}.jsonl")
        timed_out = run_claude(ONE_SHOT, d, tr, 120, arm, stats)
        parse(tr, acc)
    kill_orphans(d)
    wall = round(time.time() - t0, 1)
    g = grade(sprint, d)
    u = acc["usage"]
    eq = (u.get("input_tokens", 0) + 2.0 * u.get("cache_creation_input_tokens", 0)
          + 0.1 * u.get("cache_read_input_tokens", 0) + 5.0 * u.get("output_tokens", 0))
    rec = {"sprint": sprint, "arm": arm, "rep": rep, "sessions": sessions, "wall_s": wall, "timed_out": timed_out,
           **g, "eq_tokens": round(eq), "usage": u, "cost_usd": round(acc["cost_usd"], 4), "turns": acc["turns"],
           "max_ctx": acc["max_ctx"], "tools": acc["tools"], "repeat_cmds": len(acc["bash"]) - len(set(acc["bash"])),
           "reads_raw": acc["reads_raw"], "bash": acc["bash"], "model": ARM_MODEL.get(arm, MODEL),
           "compactions": acc.get("compactions", 0), "mem_start": stats.get("mem_start"),
           "mem_min": stats.get("mem_min"), "aborted": stats.get("aborted"),
           "limited": acc["cost_usd"] == 0 and acc["turns"] <= 1 and not stats.get("aborted")}
    if arm in ("X", "OPUS_X"):
        dd = (d, d.replace("/tmp/", "/private/tmp/"))
        try:
            ex = [json.loads(ln) for ln in open(OFFLOAD_LOG, encoding="utf-8")
                  if '"explore"' in ln and any(x in ln for x in dd)]
            rec["explore_calls"] = len(ex)
            rec["explore_refs_bad"] = sum(e.get("refs_bad", 0) for e in ex)
        except OSError:
            pass
    if arm == "C2":
        dd = (d, d.replace("/tmp/", "/private/tmp/"))
        try:
            rec["hook_rewrites"] = sum(1 for ln in open(HOOK_LOG, encoding="utf-8") if any(x in ln for x in dd))
            offl = [json.loads(ln) for ln in open(OFFLOAD_LOG, encoding="utf-8") if any(x in ln for x in dd)]
            rec["offload_calls"] = len(offl)
            rec["offload_det"] = sum(1 for o in offl if o.get("kind") == "sum-det")
        except OSError:
            pass
    with lock:
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"{sprint} r{rep} {arm}: success={rec['success']} tests={g['tests_pass']} answers={g['answers_ok']} "
              f"eq={rec['eq_tokens']:,} cost=${rec['cost_usd']} turns={rec['turns']} sessions={sessions} "
              f"max_ctx={rec['max_ctx']:,} wall={wall}s rewrites={rec.get('hook_rewrites', '-')} "
              f"compactions={rec['compactions']} mem={stats.get('mem_start')}%→min {stats.get('mem_min')}% "
              f"aborted={rec['aborted']}", flush=True)
    kill_orphans(d)
    shutil.rmtree(d, ignore_errors=True)
    return rec


def run_with_retry(sprint, arm, rep, tasks, out_path):
    """撞上 Claude 用量上限时（花费 $0、只有 1 轮）等 30 分钟再跑同一次，最多 LIMIT_RETRIES 次"""
    for attempt in range(LIMIT_RETRIES + 1):
        rec = run_one(sprint, arm, rep, tasks, out_path)
        if not rec or not rec.get("limited"):
            return rec
        print(f"!! {sprint} r{rep} {arm} 撞上用量上限（第 {attempt + 1} 次），{LIMIT_WAIT // 60} 分钟后重试", flush=True)
        time.sleep(LIMIT_WAIT)
    return rec


def selftest(tasks):
    """假 agent：直接把 4 个 bug 改回去、写好答案 → 应该成功；只修 3 个 → 应该失败；答案缺一个 → 应该失败"""
    ok = True
    for sprint in SPRINTS:
        for case in ("all", "miss_bug", "miss_answer"):
            d = prepare(sprint, "C0", 99, tasks)
            bugs = SPRINTS[sprint]["bugs"][: (3 if case == "miss_bug" else 4)]
            for tid in bugs:
                t = tasks[tid]
                path = os.path.join(d, t["file"])
                lines = open(path, encoding="utf-8").read().split("\n")
                lines[t["line"] - 1] = t["old"]
                open(path, "w", encoding="utf-8").write("\n".join(lines))
            groups = SPRINTS[sprint]["answers"][: (1 if case == "miss_answer" else None)]
            open(os.path.join(d, "ANSWERS.md"), "w").write("\n".join(f"- {g[0]}" for g in groups))
            g = grade(sprint, d)
            expect = case == "all"
            print(f"selftest {sprint} {case}: success={g['success']} (期望 {expect}) {g}", flush=True)
            ok &= g["success"] == expect
            shutil.rmtree(d, ignore_errors=True)
    print("SELFTEST", "PASS" if ok else "FAIL")


def main():
    tasks = {t["id"]: t for t in json.load(open(TASKS, encoding="utf-8"))}
    if sys.argv[1] == "selftest":
        return selftest(tasks)
    out_path = sys.argv[2]

    def opt(name, default):
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default

    sprints = opt("--sprints", "S1").split(",")
    arms = opt("--arms", "C0").split(",")
    reps = int(opt("--reps", "1"))
    parallel = int(opt("--parallel", "1"))
    rep_from = int(opt("--rep-from", "1"))  # 补跑某一轮时用，比如 --rep-from 2 --reps 2
    try:  # 跑完要恢复原来的试点名单（iu install 写的 "*" 不能被测试清掉）
        pilot_before = open(PILOT, encoding="utf-8").read()
    except OSError:
        pilot_before = ""
    if "*" in pilot_before.split():
        print("!! 试点名单里有 *（iu install 已装），A 组也会被 hook 改写，测试不公平。先 iu uninstall 再跑", flush=True)
        return
    open(PILOT, "w").write("\n".join([ROOT["C2"], ROOT["X"], ROOT["OPUS_X"]]) + "\n")  # 只有用 hook 的组在试点名单里
    try:
        for rep in range(rep_from, reps + 1):
            for sprint in sprints:  # 同一冲刺同一次重复的各组同时开跑
                with ThreadPoolExecutor(max_workers=parallel) as ex:  # 默认 1：一次只跑一个会话，保护 Mac 内存
                    futs = [ex.submit(run_with_retry, sprint, arm, rep, tasks, out_path) for arm in arms]
                    for f in futs:
                        try:
                            f.result()
                        except Exception as e:  # noqa: BLE001
                            print("run failed:", repr(e), flush=True)
    finally:
        open(PILOT, "w").write(pilot_before)
    print("done", flush=True)


if __name__ == "__main__":
    main()
