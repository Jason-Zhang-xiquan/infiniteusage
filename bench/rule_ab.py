"""Claude Code 上，iu install 写进 ~/.claude/CLAUDE.md 的常驻规则是省还是费（A/B，Opus，修 bug 冲刺）。
  A ：现状，hooks + 规则（~/.claude/CLAUDE.md 原样）
  B ：只留 hooks，临时把 infiniteusage:begin/end 那一段去掉
  N ：hooks + 当前仓库 render_rule 生成的新版规则（--rule-file 给出），可选
三组都用全局 hooks（hook-pre 压缩长输出、hook-agent 子 agent 先试免费 agent），不设 IU_NO_* 开关。
每次开跑前按组改写 CLAUDE.md，跑完（包括出错、Ctrl-C、SIGTERM）恢复原样并核对 sha256。
冲刺、评分沿用 ab_sprint.py（4 个 bug + 查代码问题，lib 单元测试 + ANSWERS.md 判对错）。
会花付费额度：每次约一个 Opus 冲刺。Mac 上串行，一次只跑一个。

用法（Mac）: python3 bench/rule_ab.py OUT.jsonl [--pairs S1,S2,S3] [--arms A,B] [--rule-file new_rule.md]
      python3 bench/rule_ab.py reparse OUT.jsonl     从 /tmp/iu_rule_transcripts 重算用量
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ab_sprint  # noqa: E402
from ab_sprint import ENV, MEM_KILL_BELOW, ONE_SHOT, TASKS, grade, kill_orphans, mem_free, prepare, wait_for_memory  # noqa: E402

CLAUDE_MD = os.path.expanduser("~/.claude/CLAUDE.md")
BACKUP_DIR = os.path.expanduser("~/.infiniteusage/backups")
OFFLOAD_LOG = os.path.expanduser("~/.infiniteusage/offload.jsonl")
HOOK_LOG = os.path.expanduser("~/.infiniteusage/hook.jsonl")
TRANSCRIPTS = "/tmp/iu_rule_transcripts"
BEGIN, END = "<!-- infiniteusage:begin", "<!-- infiniteusage:end -->"
ab_sprint.ROOT.update({"A": "/tmp/iu_ruleA", "B": "/tmp/iu_ruleB", "N": "/tmp/iu_ruleN"})
ORIG = {}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def strip_rule(text: str) -> str:
    """去掉 begin/end 那一段和它前面的一个空行（iu install 装的时候补的 sep）"""
    a = text.index(BEGIN)
    b = text.index(END, a) + len(END)
    if text[b:b + 1] == "\n":
        b += 1
    if text[a - 1:a] == "\n" and text[a - 2:a - 1] == "\n":
        a -= 1
    return text[:a] + text[b:]


def set_claude_md(arm: str, new_rule: str):
    text = ORIG["bytes"].decode("utf-8")
    if arm == "B":
        text = strip_rule(text)
    elif arm == "N":
        base = strip_rule(text)
        text = base + ("\n" if base.endswith("\n") else "\n\n") + new_rule
    open(CLAUDE_MD, "w", encoding="utf-8").write(text)
    return sha(text.encode("utf-8"))


def restore():
    if "bytes" not in ORIG:
        return
    open(CLAUDE_MD, "wb").write(ORIG["bytes"])
    ok = sha(open(CLAUDE_MD, "rb").read()) == ORIG["sha"]
    print(f"已恢复 {CLAUDE_MD}：sha256 {'一致' if ok else '不一致！备份在 ' + ORIG['backup']}", flush=True)


def on_signal(signum, _frame):
    if ORIG.get("child"):  # 正在跑的 claude 在自己的进程组里，一起杀掉
        try:
            os.killpg(ORIG["child"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    restore()
    sys.exit(128 + signum)


def parse_stream(path: str) -> dict:
    """stream-json 事件流：主会话和子 agent 的 assistant 消息都在里面，按 message.id 去重累加用量"""
    acc = {"usage": {}, "tools": {}, "bash": [], "skill_loaded": False, "skill_calls": [], "turns": 0,
           "cost_usd": 0.0, "result": "", "is_error": None, "max_ctx": 0, "sub_msgs": 0}
    seen = set()
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "assistant":
            msg = d.get("message") or {}
            for c in msg.get("content") or []:
                if c.get("type") != "tool_use":
                    continue
                name, inp = c.get("name"), c.get("input") or {}
                acc["tools"][name] = acc["tools"].get(name, 0) + 1
                if name == "Skill":
                    acc["skill_calls"].append(str(inp.get("skill") or inp.get("command") or inp)[:80])
                    acc["skill_loaded"] |= "infiniteusage" in json.dumps(inp)
                if name == "Read" and "skills/infiniteusage" in str(inp.get("file_path", "")):
                    acc["skill_loaded"] = True
                if name == "Bash":
                    acc["bash"].append(inp.get("command", "")[:300])
            u, mid = msg.get("usage"), msg.get("id")
            if u and mid and mid not in seen:
                seen.add(mid)
                acc["sub_msgs"] += bool(d.get("parent_tool_use_id"))
                for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
                    acc["usage"][k] = acc["usage"].get(k, 0) + (u.get(k) or 0)
                ctx = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                   "cache_read_input_tokens"))
                acc["max_ctx"] = max(acc["max_ctx"], ctx)
        elif d.get("type") == "result":
            acc["cost_usd"] = d.get("total_cost_usd") or 0
            acc["turns"] = d.get("num_turns") or 0
            acc["result"] = (d.get("result") or "")[-300:]
            acc["is_error"] = d.get("is_error")
            acc["model_usage"] = d.get("modelUsage") or {}
    # 流里同一条消息分块出现，每块的 output_tokens 只是当时的数，不准；有 result.modelUsage（各模型合计，含子 agent）就用它
    if acc.get("model_usage"):
        names = {"input_tokens": "inputTokens", "cache_creation_input_tokens": "cacheCreationInputTokens",
                 "cache_read_input_tokens": "cacheReadInputTokens", "output_tokens": "outputTokens"}
        acc["usage"] = {k: sum(m.get(v) or 0 for m in acc["model_usage"].values()) for k, v in names.items()}
    return acc


def eq_tokens(u: dict) -> int:
    return round(u.get("input_tokens", 0) + 2.0 * u.get("cache_creation_input_tokens", 0)
                 + 0.1 * u.get("cache_read_input_tokens", 0) + 5.0 * u.get("output_tokens", 0))


def reparse(out_path: str):
    """用修正后的解析，从保存的事件流重算每条记录的用量"""
    recs = [json.loads(x) for x in open(out_path, encoding="utf-8")]
    for r in recs:
        tr = os.path.join(TRANSCRIPTS, f"{r['sprint']}_r{r['rep']}_{r['arm']}.jsonl")
        acc = parse_stream(tr)
        r.update({"usage": acc["usage"], "eq_tokens": eq_tokens(acc["usage"]),
                  "models": sorted(acc.get("model_usage") or {})})
    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
    print(f"重算了 {len(recs)} 条", flush=True)


def log_lines(path, offset, d):
    dd = (d, d.replace("/tmp/", "/private/tmp/"))
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        out = []
        for ln in f:
            if any(x in ln for x in dd):
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
        return out


def run(sprint, arm, rep, tasks, out_path, new_rule):
    if not wait_for_memory():
        print(f"!! 跳过 {sprint} {arm}：可用内存一直不够", flush=True)
        return None
    d = prepare(sprint, arm, rep, tasks)
    md_sha = set_claude_md(arm, new_rule)
    os.makedirs(TRANSCRIPTS, exist_ok=True)
    tr = os.path.join(TRANSCRIPTS, f"{sprint}_r{rep}_{arm}.jsonl")
    off = {p: (os.path.getsize(p) if os.path.exists(p) else 0) for p in (OFFLOAD_LOG, HOOK_LOG)}
    t0, aborted, mem_min = time.time(), None, 100
    with open(tr, "w") as f:
        p = subprocess.Popen(["claude", "-p", ONE_SHOT, "--model", "opus", "--output-format", "stream-json",
                              "--verbose", "--allowedTools=Bash,Read,Edit,Write,Glob,Grep,Agent,Task,Skill",
                              "--max-turns", "120"],
                             cwd=d, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.DEVNULL, env=ENV,
                             start_new_session=True)
        ORIG["child"] = p.pid
        while p.poll() is None:
            time.sleep(10)
            free = mem_free()
            mem_min = min(mem_min, free)
            if free < MEM_KILL_BELOW or time.time() - t0 > 3600:
                aborted = "memory" if free < MEM_KILL_BELOW else "timeout"
                break
        try:  # 不管怎么结束，都按进程组杀干净（hook、vitest、iu delegate 起的 opencode）
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        p.wait()
        ORIG["child"] = None
    wall =round(time.time() - t0, 1)
    open(CLAUDE_MD, "wb").write(ORIG["bytes"])  # 评分前先恢复，别的会话不受影响
    kill_orphans(d)
    acc = parse_stream(tr)
    g = grade(sprint, d)
    kill_orphans(d)
    u = acc["usage"]
    eq = eq_tokens(u)
    dl =[x for x in log_lines(OFFLOAD_LOG, off[OFFLOAD_LOG], d) if x.get("kind") == "delegate"]
    offl = [x for x in log_lines(OFFLOAD_LOG, off[OFFLOAD_LOG], d) if x.get("kind") != "delegate"]
    hooks = log_lines(HOOK_LOG, off[HOOK_LOG], d)
    bash = acc["bash"]
    _, notes = ab_sprint.sh("git status --porcelain -- NOTES.md", d)
    rec = {"ts": time.time(), "sprint": sprint, "arm": arm, "rep": rep, "model": "opus", "claude_md_sha": md_sha[:12],
           "wall_s": wall, "aborted": aborted, **g, "eq_tokens": round(eq), "usage": u,
           "cost_usd": round(acc["cost_usd"], 4), "turns": acc["turns"], "max_ctx": acc["max_ctx"],
           "sub_msgs": acc["sub_msgs"], "tools": acc["tools"], "skill_loaded": acc["skill_loaded"],
           "skill_calls": acc["skill_calls"],
           "self_delegate_cmds": sum("delegate" in b and "run_iu.py" in b for b in bash),
           "self_sum_cmds": sum(" sum " in b and "run_iu.py" in b for b in bash),
           "self_agents_cmds": sum(" agents" in b and "run_iu.py" in b for b in bash),
           "delegate_calls": [{"ok": x.get("ok"), "s": x.get("seconds"), "model": x.get("routed")} for x in dl],
           "offload_calls": len(offl), "hook_rewrites": len(hooks), "wrote_notes": bool(notes.strip()),
           "mem_min": mem_min, "is_error": acc["is_error"], "result": acc["result"], "bash": bash,
           "limited": acc["cost_usd"] == 0 and acc["turns"] <= 1 and not aborted}
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"{sprint} {arm}: 成功={g['success']} 测试={g['tests_pass']} 答案={g['answers_ok']} 等效={rec['eq_tokens']:,} "
          f"${rec['cost_usd']} 轮={rec['turns']} {wall}s skill={rec['skill_loaded']} "
          f"delegate={rec['self_delegate_cmds']}{rec['delegate_calls']} sum={rec['self_sum_cmds']} "
          f"hook改写={rec['hook_rewrites']} 外包={rec['offload_calls']} NOTES={rec['wrote_notes']} "
          f"内存最低={mem_min}% 中止={aborted}", flush=True)
    shutil.rmtree(d, ignore_errors=True)
    return rec


def main():
    if sys.argv[1] == "reparse":
        return reparse(sys.argv[2])
    out_path = sys.argv[1]

    def opt(name, default):
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default

    pairs = opt("--pairs", "S1,S2,S3").split(",")
    arms = opt("--arms", "A,B").split(",")
    rep0 = int(opt("--rep-from", "1"))
    new_rule = open(opt("--rule-file", ""), encoding="utf-8").read() if "--rule-file" in sys.argv else ""
    if "N" in arms and BEGIN not in new_rule:
        sys.exit("N 组要用 --rule-file 给出新版规则")
    raw = open(CLAUDE_MD, "rb").read()
    if BEGIN.encode() not in raw:
        sys.exit("~/.claude/CLAUDE.md 里没有 infiniteusage 规则，A 组不成立")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup = os.path.join(BACKUP_DIR, time.strftime("CLAUDE.md.rule_ab-%Y%m%d-%H%M%S"))
    shutil.copy2(CLAUDE_MD, backup)
    ORIG.update({"bytes": raw, "sha": sha(raw), "backup": backup})
    print(f"原 CLAUDE.md sha256={ORIG['sha'][:12]}，备份 {backup}", flush=True)
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, on_signal)
    tasks = {t["id"]: t for t in json.load(open(TASKS, encoding="utf-8"))}
    try:
        for i, sprint in enumerate(pairs):  # 同一冲刺 A、B 紧挨着跑，组的顺序每对轮换，抵消时间漂移
            order = arms if i % 2 == 0 else arms[::-1]
            for arm in order:
                rec = run(sprint, arm, rep0 + i, tasks, out_path, new_rule)
                if rec and rec.get("limited"):
                    print("!! 撞上用量上限，停", flush=True)
                    return
    finally:
        restore()
    print("done", flush=True)


if __name__ == "__main__":
    main()
