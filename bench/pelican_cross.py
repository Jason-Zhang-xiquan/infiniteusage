"""「跨会话失忆鹈鹕」：第一个会话分 6 次定好 16 条细节（含多次改动），结束；开一个全新的会话（不 --resume），
只说「按上次定好的规格画」。看新会话能保住几条。规格、打分沿用 bench/pelican_compact.py 的加难版和 pelican_check.py。

组：
  X：第一个会话不提笔记。新会话只能靠 Claude Code 自己的东西（自动记忆 memory/、CLAUDE.md）
  N：第一个会话按 skill 第 3 节让它把规格逐条记进 NOTES.md；新会话不给任何提示
  H：同 N，新会话开始时 SessionStart(startup) hook 把 NOTES.md 塞进上下文（只在本次运行用 --settings 加，不改 settings.json）

  R：同 N，新会话所在项目有 AGENTS.md，内容是 iu 常驻规则里讲 NOTES.md 的几条（含「新会话开始先读 NOTES.md」）。
     看只靠规则、没有 hook 时够不够（Codex、OpenCode 等读 AGENTS.md 的 agent 都能用）

宿主（--host）：
  claude  （默认，付费）：H 用 --settings 加 SessionStart(startup) hook
  opencode（免费模型）：H 在新会话前往项目里放 bench/opencode_notes.js（.opencode/plugins/），把 NOTES.md 放进系统提示
  codex   （付费，Codex 额度）：H 用 -c 传 SessionStart hook，命令就是 iu hook-compact；hook 默认要先信任，
           这里加 --dangerously-bypass-hook-trust（只在这次测试里）。推理强度用 --effort（默认 medium）

用法（Mac）: python3 bench/pelican_cross.py --arms X,N [--reps 1] [--model sonnet] [--final ref|plain] [--host claude]
  --final plain：新会话不提「上次定好的规格」，只说画一只骑车的鹈鹕（看没提的时候会不会自己去读 NOTES.md）
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pelican_check  # noqa: E402
from pelican_compact import (ENV, HARD, NOTED, NOTES, OUT_DIR, REPO, UPDATE_NOTES, cc_cost, cc_turn, kill_group,  # noqa: E402
                             oc_turn, prepare)

FINAL = ("Generate an SVG of a pelican riding a bicycle. Use every detail of the illustration spec we agreed on in our "
         "previous session, including the later changes. Write it to the file pelican.svg in the current directory.")
# 「不提上次」版：新会话只说画什么，不说要按之前的规格（--final plain）
FINAL_PLAIN = "Generate an SVG of a pelican riding a bicycle. Write it to the file pelican.svg in the current directory."
# 原版第一段说「near the end of this session」；跨会话版不预告会换会话，和平时用法一样
SPEC = [HARD[0].replace("near the end of this session", "later")] + HARD[1:]


def rule_lines() -> str:
    """iu 常驻规则（iu/universal.py render_rule）里讲 NOTES.md 的几条，原样写进 R 组项目的 AGENTS.md"""
    from iu.universal import render_rule
    return "\n".join(x for x in render_rule("iu").splitlines() if "NOTES.md" in x) + "\n"


# ---------------- Codex 宿主 ----------------

def cx_turn(d, model, msg, sid, log, effort, extra=()):
    """Codex 一轮：新会话 codex exec，续聊 codex exec resume <sid>。返回 (sid, 这一轮的 usage)"""
    opts = ["--json", "--skip-git-repo-check", "-m", model, "-c", f'model_reasoning_effort="{effort}"',
            "-c", 'sandbox_mode="workspace-write"', *extra]
    cmd = ["codex", "exec", "resume", *opts, sid, msg] if sid else ["codex", "exec", *opts, "-C", d, msg]
    with open(log, "ab") as f:
        start = f.tell()
        p = subprocess.Popen(cmd, cwd=d, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.DEVNULL, env=ENV,
                             start_new_session=True)
        try:
            p.wait(timeout=1200)
        except subprocess.TimeoutExpired:
            pass
        kill_group(p.pid)
    usage = {}
    with open(log, "rb") as f:
        f.seek(start)
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "thread.started":
                sid = sid or e.get("thread_id")
            if e.get("type") == "turn.completed":
                for k, v in (e.get("usage") or {}).items():
                    usage[k] = usage.get(k, 0) + (v or 0)
    return sid, usage


def cx_eq(usage) -> int:
    """等效 token：没命中缓存的输入 1、缓存读 0.1、输出 5（Codex 的 output_tokens 已经含推理）"""
    cached = usage.get("cached_input_tokens", 0)
    return round(usage.get("input_tokens", 0) - cached + 0.1 * cached + 5 * usage.get("output_tokens", 0))


def cx_limits(sids) -> list:
    """这次运行里 Codex 官方额度（已用 %）的第一条和最后一条，用来看这次花了多少额度"""
    out = []
    for sid in sids:
        for p in glob.glob(os.path.expanduser(f"~/.codex/sessions/*/*/*/rollout-*{sid}.jsonl")):
            for line in open(p, encoding="utf-8", errors="replace"):
                if '"rate_limits"' not in line:
                    continue
                rl = ((json.loads(line).get("payload") or {}).get("rate_limits")) or {}
                out.append({k: (rl.get(k) or {}).get("used_percent") for k in ("primary", "secondary")})
    return [out[0], out[-1]] if out else []


def tools_in_log(log, start, host) -> list:
    """新会话里调了哪些工具（OpenCode / Codex 从事件流里取）"""
    out = []
    with open(log, "rb") as f:
        f.seek(start)
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if host == "codex":
                it = e.get("item") or {}
                if e.get("type") == "item.completed" and it.get("type") == "command_execution":
                    out.append("exec:" + str(it.get("command", ""))[:80])
            else:
                part = e.get("part") or {}
                if part.get("type") == "tool" and (part.get("state") or {}).get("status") == "completed":
                    i = (part.get("state") or {}).get("input") or {}
                    out.append(f"{part.get('tool')}:{str(i.get('filePath') or i.get('command') or i.get('pattern') or '')[:80]}")
    return out


def hook_startup():
    """SessionStart(startup) hook：把当前目录的 NOTES.md 原样塞进上下文"""
    payload = json.loads(sys.stdin.read() or "{}")
    p = os.path.join(payload.get("cwd") or os.getcwd(), "NOTES.md")
    if payload.get("source") == "startup" and os.path.exists(p):
        text = open(p, encoding="utf-8", errors="replace").read()
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                          f"Project notes file {p} (requirements and decisions from earlier sessions):\n\n{text}"}}))


def memory_files(d):
    """Claude Code 自动记忆给这个目录写了什么（~/.claude/projects/<目录编码>/memory/）"""
    out = {}
    for p in glob.glob(os.path.expanduser("~/.claude/projects/*/memory/*.md")):
        if os.path.basename(d) in p.replace("-", "_") or os.path.basename(d).replace("_", "-") in p:
            out[p] = open(p, encoding="utf-8", errors="replace").read()
    return out


def tools_used(sid):
    """新会话里调了哪些工具（看它有没有去找 NOTES.md / 记忆）"""
    out = []
    for p in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")):
        for line in open(p, encoding="utf-8", errors="replace"):
            if '"tool_use"' not in line:
                continue
            for c in (json.loads(line).get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    i = c.get("input") or {}
                    out.append(f"{c.get('name')}:{str(i.get('file_path') or i.get('command') or i.get('pattern') or '')[:80]}")
    return out


def run(arm, model, rep, final="ref", host="claude", effort="medium"):
    tag = host if host != "claude" else "claude"
    name = f"{tag}_cross_{arm}_{rep}" if final == "ref" else f"{tag}_cross_{final}_{arm}_{rep}"
    d = prepare(name, [])
    real = os.path.realpath(d)
    if host == "claude":
        for p in glob.glob(os.path.expanduser("~/.claude/projects/*")):  # 清掉同名目录以前留下的自动记忆
            if os.path.basename(p).endswith("-pelican-" + name.replace("_", "-")):
                shutil.rmtree(p, ignore_errors=True)
    log = os.path.join(d, "events.jsonl")
    t0 = time.time()
    sid1, usage1 = None, {}

    def turn(msg, sid, extra=()):
        if host == "claude":
            return cc_turn(d, model, msg, sid, log, dict(ENV), extra)
        if host == "opencode":
            return oc_turn(d, model, msg, sid, log)
        sid, u = cx_turn(d, model, msg, sid, log, effort, extra)
        return sid, u

    for i, spec in enumerate(SPEC):
        extra = (NOTES if i == 0 else UPDATE_NOTES) if arm in ("N", "H", "R") else ""
        sid1, ctx = turn(spec + extra + NOTED, sid1)
        if host == "codex":
            for k, v in ctx.items():
                usage1[k] = usage1.get(k, 0) + v
            ctx = ctx.get("input_tokens", 0)
        print(f"  {name} 会话 1 第 {i + 1}/{len(SPEC)} 轮 上下文 {ctx:,}", flush=True)
    mem = memory_files(real) if host == "claude" else {}
    notes = open(os.path.join(d, "NOTES.md"), encoding="utf-8").read() if os.path.exists(os.path.join(d, "NOTES.md")) else None
    extra = ()
    if arm == "R":  # 只在新会话前放：第一个会话的条件和 N 组一样
        open(os.path.join(d, "AGENTS.md"), "w", encoding="utf-8").write(rule_lines())
        if host == "claude":
            shutil.copy2(os.path.join(d, "AGENTS.md"), os.path.join(d, "CLAUDE.md"))
    if arm == "H":
        if host == "claude":
            cfg = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{
                "type": "command", "command": f'"{sys.executable}" "{os.path.abspath(__file__)}" --hook-startup', "timeout": 10}]}]}}
            extra = ("--settings", json.dumps(cfg))
        elif host == "opencode":
            os.makedirs(os.path.join(d, ".opencode", "plugins"), exist_ok=True)
            shutil.copy2(os.path.join(REPO, "bench", "opencode_notes.js"), os.path.join(d, ".opencode", "plugins", "iu-notes.js"))
        else:
            cmd = f'"{sys.executable}" "{os.path.join(REPO, "run_iu.py")}" hook-compact'
            extra = ("-c", f"hooks.SessionStart=[{{matcher=\"startup|compact\",hooks=[{{type=\"command\",command='{cmd}',"
                           "timeout=10,additionalContextLimit=5000}]}]", "--dangerously-bypass-hook-trust")
    s2_start = os.path.getsize(log) if os.path.exists(log) else 0
    sid2, ctx = turn(FINAL if final == "ref" else FINAL_PLAIN, None, extra)  # 不续聊：全新会话
    usage2 = ctx if host == "codex" else {}
    print(f"  {name} 会话 2 上下文 {(ctx.get('input_tokens', 0) if host == 'codex' else ctx):,}", flush=True)
    svg = os.path.join(d, "pelican.svg")
    s = pelican_check.score(svg, "hard")
    os.makedirs(OUT_DIR, exist_ok=True)
    keep = os.path.join(OUT_DIR, name + ".svg")
    if os.path.exists(svg):
        shutil.copy2(svg, keep)
        pelican_check.render(keep, keep[:-4] + ".png")
    if host == "claude":
        c1, c2 = cc_cost(sid1)[0], cc_cost(sid2)[0] if sid2 else 0
        tools = tools_used(sid2) if sid2 else []
    else:
        c1, c2 = (cx_eq(usage1), cx_eq(usage2)) if host == "codex" else (0, 0)  # OpenCode 免费模型不算钱
        tools = tools_in_log(log, s2_start, host)
    r = {"ts": time.time(), "test": "cross", "host": host, "final": final, "arm": arm, "model": model, "rep": rep,
         "score": s["score"], "total": s["total"], "missing": s["missing"], "error": s.get("error"), "eq_s1": c1,
         "eq_s2": c2, "wall_s": round(time.time() - t0), "sessions": [sid1, sid2], "notes_md": notes, "memory": mem,
         "s2_tools": tools, "svg": keep if os.path.exists(svg) else None}
    if host == "codex":
        r.update(effort=effort, usage=[usage1, usage2], codex_limits=cx_limits([sid1, sid2]))
    open(os.path.join(OUT_DIR, "cross_runs.jsonl"), "a").write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{name}: {s['score']}/{s['total']} 缺 {s['missing']} 等效 token 会话1 {c1:,} 会话2 {c2:,} "
          f"自动记忆 {len(mem)} 个文件 NOTES.md {'有' if notes else '无'} {r['wall_s']}s"
          + (f" Codex 额度 {r['codex_limits']}" if host == "codex" else ""), flush=True)
    print("  会话 2 工具：" + "; ".join(tools[:12]), flush=True)
    return r


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    if "--hook-startup" in sys.argv:
        try:
            hook_startup()
        except Exception:  # noqa: BLE001  hook 出错不影响会话
            pass
        return
    host = arg("--host", "claude")
    model = arg("--model", {"claude": "sonnet", "opencode": "deepseek-v4.1-flash-free", "codex": "gpt-6-sol"}[host])
    kw = {"final": arg("--final", "ref" if host == "claude" else "plain"), "host": host, "effort": arg("--effort", "medium")}
    if "--rep-name" in sys.argv:  # 重跑用的名字，比如 3b（隔离重跑）
        for arm in arg("--arms", "X,N").split(","):
            run(arm, model, arg("--rep-name", ""), **kw)
        return
    reps, rep0 = int(arg("--reps", "1")), int(arg("--rep0", "1"))
    for rep in range(rep0, rep0 + reps):
        for arm in arg("--arms", "X,N").split(","):
            run(arm, model, rep, **kw)


if __name__ == "__main__":
    main()
