"""「失忆鹈鹕」：早早定好 12 个细节，中间塞无关工作撑大上下文，（按组）压缩，最后让它画鹈鹕骑自行车。
计划和规格：docs/visual_compaction_test_plan.md；打分：bench/pelican_check.py。

组：
  ceil：规格给完马上画（天花板）
  A   ：撑大上下文，不压缩
  B   ：撑大上下文，压缩一次再画
  C   ：同 B，但开头让它把规格记进 NOTES.md（skill 第 3 节的建议）；Claude Code 上再加 iu hook-compact，
        压缩一结束把 NOTES.md 塞回上下文（2026-10-24 rep 3 起；措辞同时改成新版第 3 节的「逐条、含小细节」）

宿主：
  opencode（阶段 0，免费模型，零付费）：压缩用 OpenCode 本地服务的 summarize 接口，在最后一轮前做一次
  claude  （阶段 1 起，付费）：B / C 用 --settings 设 CLAUDE_CODE_AUTO_COMPACT_WINDOW 让它自己压缩；A 用默认窗口（不会触发）

用法（Mac）: python3 bench/pelican_compact.py --host opencode --arms ceil,A,B,C [--reps 1] [--model deepseek-v4.1-flash-free]
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
import pelican_check  # noqa: E402

OUT_DIR = os.path.join(HERE, "results", "pelican")
WORK = "/tmp/pelican"
ENV = {**os.environ, "PATH": os.path.expanduser("~/.local/bin") + ":/opt/homebrew/bin:" + os.environ["PATH"],
       "NO_COLOR": "1", "CI": "1", "NODE_OPTIONS": "--max-old-space-size=2048",
       "IU_NO_OFFLOAD": "1", "IU_NO_DELEGATE": "1"}

SPEC = """We will make one SVG illustration together later in this session. Do NOT draw anything yet. Here is the spec:
1. Canvas: viewBox="0 0 800 600".
2. A pelican riding a bicycle. The pelican wears a scarf in color #E4572E.
3. The bicycle frame is #8338EC.
4. The pelican wears a helmet in color #2E86AB.
5. A sun, a circle filled #FFB703, in the upper-left corner of the canvas.
6. The sky is a gradient from #FFD166 to #06D6A0.
7. A roadside sign with the text IU-42.
8. A fish in the bicycle's front basket, filled #3A86FF.
9. Exactly 5 birds in the sky, each drawn with stroke #2B2D42.
10. Each wheel has exactly 6 spokes, stroke #5C677D (12 spokes in total).
11. The ground line uses stroke-dasharray="12 8".
"""
# 2026-10-24 rep 3 起改成新版 skill 第 3 节的要求（rep 2 及以前是「write this spec verbatim … keep it up to date」，
# 那次漏记了第 4 段的小细节）
NOTES = (" Before replying, write this spec into a file NOTES.md in the current directory: every requirement on its own "
         "line with its exact values, including details that look minor. Keep that file up to date whenever the spec "
         "changes, and after any context compaction read NOTES.md before continuing.")
CHANGE = ("Two changes to the illustration spec: the roadside sign should now read IU-43 instead of IU-42, and drop the "
          "helmet entirely: the pelican wears no helmet and the color #2E86AB must not appear anywhere. Still do not "
          "draw anything yet.")
FINAL = ("Generate an SVG of a pelican riding a bicycle. Use every detail of the illustration spec we agreed on earlier, "
         "including the later changes. Write it to the file pelican.svg in the current directory.")
NOTED = " Reply with just: noted."
UPDATE_NOTES = (" Update NOTES.md now: add every item above as its own line with its exact values, including small "
                "details, and replace any value that changed.")

# 加难版：规格分 4 次给、同一细节改几次、穿插无关工作、压缩两次、加琐碎细节（docs/visual_compaction_test_plan.md）
HARD = [
    """We will make one SVG illustration together near the end of this session. Do NOT draw anything yet. This is a plain exercise: do not load any skills and do not run any iu commands. First part of the spec:
- Canvas: viewBox="0 0 800 600".
- A pelican riding a bicycle; the pelican wears a scarf in color #E4572E.
- The bicycle frame is #8338EC.
- The pelican wears a helmet in color #2E86AB.
- A roadside sign with the text IU-42.""",
    """More of the illustration spec (still do not draw):
- A sun, a circle filled #FFB703, in the upper-left corner, with exactly 8 straight rays drawn with stroke #FB8500.
- The sky is a gradient from #FFD166 to #06D6A0.
- A fish in the bicycle's front basket, filled #3A86FF.
- Exactly 5 birds in the sky, each drawn with stroke #2B2D42.""",
    """Changes and additions to the illustration spec (still do not draw):
- The sign now reads IU-43.
- Drop the helmet entirely: no helmet, and the color #2E86AB must not appear anywhere.
- Each wheel has exactly 6 spokes, stroke #5C677D.
- The ground line uses stroke-dasharray="12 8".""",
    """A few small details for the illustration (still do not draw):
- The pelican's eye is a small circle filled #0B132B.
- A tiny ladybug, filled #D7263D, sits on top of the sign.
- One cloud, filled #F1FAEE with opacity 0.8.
- Put the comment <!-- iu-pelican-v3 --> as the very first thing inside the <svg> element.""",
    """Changes to the illustration spec (still do not draw):
- The sign now reads IU-45.
- The scarf color is now #F95738; the old scarf color #E4572E must not appear anywhere.
- There are now exactly 7 birds instead of 5.""",
    """Last changes to the illustration spec (still do not draw):
- The sign now reads IU-46.
- The fish is now filled #00B4D8; the old fish color #3A86FF must not appear anywhere.""",
]


def schedule(level: str, arm: str, n_filler: int) -> list:
    """[("say", 提示词) / ("filler", 序号) / ("compact",) / ("final",)]；ceil 没有中间工作，只有 B/C 压缩"""
    notes = arm == "C"
    long_ = arm != "ceil"
    if level == "easy":
        steps = [("say", SPEC + (NOTES if notes else "") + NOTED), ("say", CHANGE + (UPDATE_NOTES if notes else "") + NOTED)]
        steps += [("filler", i) for i in range(n_filler)] if long_ else []
        steps += [("compact",)] if arm in ("B", "C") else []
        return steps + [("final",)]
    per = n_filler // 6  # 6 段规格之间各夹 per 轮无关工作；压缩放在第 4 段之后和最后
    steps, k = [], 0
    for i, spec in enumerate(HARD):
        extra = (NOTES if i == 0 else UPDATE_NOTES) if notes else ""
        steps.append(("say", spec + extra + NOTED))
        if long_:
            steps += [("filler", k + j) for j in range(per)]
            k += per
            if arm in ("B", "C") and i in (3, 5):
                steps.append(("compact",))
    return steps + [("final",)]


def filler_turns(n: int, per: int = 3) -> list:
    """无关工作：分批完整读本仓库的代码，回答问题。返回 (提示词, 要复制进工作目录的文件) 列表"""
    # 不能读到这个测试自己的脚本：里面写着完整规格和每次改动，等于中途把答案又给了一遍（2026-10-24 发现，之前的结果作废）
    files = [f for f in glob.glob(os.path.join(REPO, "bench", "*.py")) + glob.glob(os.path.join(REPO, "iu", "*.py"))
             if not os.path.basename(f).startswith("pelican")
             and "IU-4" not in open(f, encoding="utf-8", errors="replace").read()]
    files = sorted(files, key=os.path.getsize, reverse=True)[:per * n]
    turns = []
    for i in range(n):
        group = [os.path.relpath(f, REPO) for f in files[i::n]]
        turns.append((f"Unrelated task: read these files completely, from first line to last: {', '.join('ref/' + g for g in group)}. "
                      "Then list every top-level function in each file with a one-line description of what it does.", group))
    return turns


def prepare(name: str, filler: list) -> str:
    d = os.path.join(WORK, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    for _, group in filler:
        for g in group:
            os.makedirs(os.path.dirname(os.path.join(d, "ref", g)), exist_ok=True)
            shutil.copy2(os.path.join(REPO, g), os.path.join(d, "ref", g))
    subprocess.run("git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init", shell=True, cwd=d)
    return d


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ---------------- OpenCode 宿主 ----------------

def oc_bin():
    from iu.explore import opencode_bin
    return opencode_bin()


def oc_turn(d, model, msg, sid, log):
    cmd = [oc_bin(), "run", "-m", f"opencode/{model}", "--agent", "build", "--format", "json", "--dir", d]
    if sid:
        cmd += ["-s", sid]
    with open(log, "ab") as f:
        start = f.tell()
        p = subprocess.Popen(cmd + [msg], cwd=d, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, env=ENV,
                             start_new_session=True)
        try:
            p.wait(timeout=900)
        except subprocess.TimeoutExpired:
            pass
        kill_group(p.pid)
    sid2, ctx = sid, 0
    with open(log, "rb") as f:
        f.seek(start)
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            sid2 = sid2 or e.get("sessionID")
            t = ((e.get("part") or {}).get("tokens") or {})
            if t.get("total"):
                ctx = t["total"]  # 这一步发给模型的上下文（含输出）
    return sid2, ctx


def oc_compact(d, model, sid):
    """起一个本地 OpenCode 服务，调 summarize 压缩这个会话，然后关掉"""
    port = 4190 + os.getpid() % 500
    p = subprocess.Popen([oc_bin(), "serve", "--port", str(port)], cwd=d, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV, start_new_session=True)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(30):
            try:
                urllib.request.urlopen(base + "/session", timeout=2)
                break
            except OSError:
                time.sleep(2)
        req = urllib.request.Request(f"{base}/session/{sid}/summarize?directory={d}", method="POST",
                                     data=json.dumps({"providerID": "opencode", "modelID": model}).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=600).read().decode()[:100]
    except OSError as e:
        return f"压缩失败：{e}"
    finally:
        kill_group(p.pid)


# ---------------- Claude Code 宿主 ----------------

def cc_turn(d, model, msg, sid, log, env, extra=()):
    cmd = ["claude", "-p", msg, "--model", model, "--output-format", "json",
           "--allowedTools=Bash,Read,Edit,Write,Glob,Grep", "--max-turns", "40", *extra]
    if sid:
        cmd += ["--resume", sid]
    p = subprocess.Popen(cmd, cwd=d, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         env=env, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=1200)
    finally:
        kill_group(p.pid)
    open(log, "ab").write(out + b"\n")
    try:
        res = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        return sid, 0
    sid = res.get("session_id") or sid
    trace = cc_trace(sid)["ctx"]  # -p 输出里的 usage 是这一轮所有请求的总和，不是上下文；上下文从会话记录里取最后一次请求
    return sid, trace[-1] if trace else 0


def cc_trace(sid) -> dict:
    """会话记录里每次请求的上下文（输入 + 缓存读 + 缓存写），以及每次压缩发生在第几次请求之前"""
    ctx, compact_at, seen = [], [], set()
    for p in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")):
        for line in open(p, encoding="utf-8", errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("subtype") == "compact_boundary":  # 只认真正的压缩记录；读进来的文件里也可能有这个词
                compact_at.append(len(ctx))
                continue
            m = d.get("message") or {}
            u = m.get("usage")
            if d.get("type") != "assistant" or not u or m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            ctx.append((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                       + (u.get("cache_creation_input_tokens") or 0))
    return {"ctx": ctx, "compact_at": compact_at}


def cc_cost(sid):
    from e2e_delegate import session_cost
    main, _s, _n, _u = session_cost(sid)
    return round(main), len(cc_trace(sid)["compact_at"])


# ---------------- 一次运行 ----------------

def run(host, arm, model, rep, n_filler, compact_window, level="easy"):
    name = f"{host}_{level}_{arm}_{rep}"
    filler = [] if arm == "ceil" else filler_turns(n_filler, 3 if level == "easy" else 2)
    d = prepare(name, filler)
    log = os.path.join(d, "events.jsonl")
    env = dict(ENV)
    extra = ()
    if host == "claude" and arm in ("B", "C"):  # 用 --settings 传：用户 settings.json 里的 env 会盖过进程环境变量（实测）
        cfg = {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(compact_window)}}
        if arm == "C":  # 压缩后的 hook 只在这次运行里加，不改用户的 settings.json（Mac 上还有用户别的会话）
            cfg["hooks"] = {"SessionStart": [{"matcher": "compact", "hooks": [{
                "type": "command", "command": f'"{sys.executable}" "{os.path.join(REPO, "run_iu.py")}" hook-compact || true',
                "timeout": 10}]}]}
        extra = ("--settings", json.dumps(cfg))
    steps = schedule(level, arm, n_filler)
    talk = [s for s in steps if s[0] != "compact"]
    sid, ctx_trace, t0, compact_notes, i, sids = None, [], time.time(), [], 0, []
    for step in steps:
        if step[0] == "compact":
            if host == "opencode" and sid:  # Claude Code 靠窗口设置自己压缩
                compact_notes.append(oc_compact(d, model, sid))
            continue
        msg = step[1] if step[0] == "say" else filler[step[1]][0] if step[0] == "filler" else FINAL
        if host == "opencode":
            sid, ctx = oc_turn(d, model, msg, sid, log)
        else:
            sid, ctx = cc_turn(d, model, msg, sid, log, env, extra)
        ctx_trace.append(ctx)
        if sid and sid not in sids:
            sids.append(sid)
        i += 1
        print(f"  {name} 第 {i}/{len(talk)} 轮 上下文 {ctx:,}", flush=True)
        if not sid:
            break
    compact_note = ",".join(compact_notes)
    svg = os.path.join(d, "pelican.svg")
    s = pelican_check.score(svg, level)
    os.makedirs(OUT_DIR, exist_ok=True)
    keep = os.path.join(OUT_DIR, name + ".svg")
    if os.path.exists(svg):
        shutil.copy2(svg, keep)
        pelican_check.render(keep, keep[:-4] + ".png")
    eq, compactions, req = None, None, None
    if host == "claude":  # 续聊后会话号可能变：把这次运行出现过的会话都算上
        eq = compactions = 0
        req = {"ctx": [], "compact_at": []}
        for s_ in sids:
            e_, c_ = cc_cost(s_)
            eq, compactions = eq + e_, compactions + c_
            t_ = cc_trace(s_)
            req["compact_at"] += [len(req["ctx"]) + k for k in t_["compact_at"]]
            req["ctx"] += t_["ctx"]
    r = {"ts": time.time(), "host": host, "level": level, "arm": arm, "model": model, "rep": rep, "score": s["score"],
         "total": s["total"],
         "missing": s["missing"], "error": s.get("error"), "ctx_trace": ctx_trace, "max_ctx": max(ctx_trace or [0]),
         "final_ctx": (ctx_trace or [0])[-1], "compact": compact_note, "compactions": compactions, "eq_total": eq,
         "notes_md": os.path.exists(os.path.join(d, "NOTES.md")), "wall_s": round(time.time() - t0), "session": sid,
         "sessions": sids, "req_trace": req,
         "svg": keep if os.path.exists(svg) else None}
    open(os.path.join(OUT_DIR, "runs.jsonl"), "a").write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{name}: {s['score']}/{s['total']} 缺 {s['missing']} 最大上下文 {r['max_ctx']:,} 最后一轮 {r['final_ctx']:,} "
          f"{compact_note or ''} {r['wall_s']}s", flush=True)
    return r


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


LABEL = {"ceil": "马上画（天花板）", "A": "长上下文·不压缩", "B": "长上下文·压缩", "C": "压缩 + 笔记"}


def main():
    host = arg("--host", "opencode")
    model = arg("--model", "deepseek-v4.1-flash-free" if host == "opencode" else "sonnet")
    arms = arg("--arms", "ceil,A,B,C").split(",")
    reps, rep0 = int(arg("--reps", "1")), int(arg("--rep0", "1"))
    level = arg("--level", "easy")
    n_filler = int(arg("--filler", "8" if level == "easy" else "12"))
    window = int(arg("--window", "100000"))
    results = []
    for rep in range(rep0, rep0 + reps):
        for arm in arms:
            results.append(run(host, arm, model, rep, n_filler, window, level))
    cards = [{"label": LABEL[r["arm"]], "svg": r["svg"] or "", "score": r["score"], "total": r["total"],
              "missing": r["missing"],
              "cost": r["eq_total"] or r["max_ctx"],
              "cost_label": (f"等效 {r['eq_total']:,} token" if r["eq_total"] else f"最大上下文 {r['max_ctx']:,}")}
             for r in results[-len(arms):]]
    gj = os.path.join(OUT_DIR, f"grid_{host}_{level}_{rep0 + reps - 1}.json")
    json.dump(cards, open(gj, "w", encoding="utf-8"), ensure_ascii=False)
    pelican_check.grid(cards, gj[:-5] + ".png")
    print("四宫格：" + gj[:-5] + ".png")


if __name__ == "__main__":
    main()
