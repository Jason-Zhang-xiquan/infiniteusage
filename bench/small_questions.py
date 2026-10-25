"""「在已经很大的上下文里问的小问题」占多少用量：只读本机 Claude Code 会话记录，不调用模型、不花额度。

问题：长会话里顺手问一句小问题，每次请求都要把几十万 token 的上下文缓存读一遍。这类回合占总用量多少？
如果改成开新会话用 Haiku，或者交给免费模型（`iu ask`），最多能省多少？

口径
- 只看主会话（跳过 subagents/ 和 isSidechain）。一次请求 = 一个 assistant message.id，全局只算一次
- 等效 token：输入 1、缓存写 2（1h）/1.25（5m）、缓存读 0.1、输出 5（和 session_explore.py 一样）
- 真实用户消息：type=user、不是 tool_result、不是 isMeta / isCompactSummary、origin.kind 是 human 或没有；
  去掉 <system-reminder> 块后不以 AUTO 前缀开头（后台任务、通知、粘贴、斜杠命令、中断提示等）。按 (text, ts) 去重
- 回合 = 一条真实用户消息开始，到下一条真实用户消息之前的全部请求（中间的 task-notification、子 agent 回报都算在里面）
- 小问题（宽松，阈值都可调）：用户消息 ≤ --max-chars 字（默认 120）；回合内请求 ≤ --max-reqs 次（默认 5）；
  回合内没有改文件（Edit/Write/MultiEdit/NotebookEdit，或会写文件的 Bash/PowerShell）；
  回合第一次请求的上下文（输入 + 缓存写 + 缓存读）≥ --min-ctx（默认 5 万）
- 岔题（严格）= 宽松里再满足其一：
  a) 回合里的工具只有 WebSearch/WebFetch（和记账工具）
  b) 回合碰过的文件（Read/Grep/Glob 的路径、Bash 里 cat/head/sed -n 的文件）至少一个，并且和本会话之前碰过的文件完全不重叠
  没用工具、纯靠上下文回答的短问题算「追问」，只进宽松档（多半是「为什么」「那这个呢」，离开上下文答不了）
- 可省上限：
  免费模型：整个回合的花费都省掉（上限，没算换地方问的额外成本、答不好重问）
  Haiku 新会话（估计）：新会话上下文 = 2.5 万系统提示 + 回合内新增的部分；第一次全部写缓存（×2），之后每次读上一次的
  上下文（×0.1）、写新增部分（×2），输出 ×5；整体再乘 Haiku/Opus 单价比 1/5
- 扣掉缓存重建：回合第一次请求如果缓存过期（缓存写远大于上一回合结束后新增的部分），这笔重建费换个地方问也省不掉，
  只是挪到长会话的下一个回合。所以另报一列「扣重建」：把这部分按缓存读（×0.1）计

用法: python bench/small_questions.py [--days 0] [--max-chars 120] [--max-reqs 5] [--min-ctx 50000] [--samples 15]
"""

from __future__ import annotations

import argparse
import glob
import os
import random
import re
import sys
import time
import json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_explore import HOME, EDIT_TOOLS, NEUTRAL, SHELLS, cost, norm, shell_kind, targets  # noqa: E402

AUTO = ("Background (from the session", "<pasted_content", "<task-notification", "<system-reminder", "<side-sessions",
        "[SYSTEM", "Caveat:", "<ci-monitor", "<local-command", "<command-", "This session is being continued",
        "[Request interrupted", "Another Claude session sent")
WEB = {"WebSearch", "WebFetch"}
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
SYS_PROMPT, HAIKU_RATIO = 25_000, 0.2


def user_text(d):
    """真实用户消息的文字；不是就返回 None"""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary"):
        return None
    if (d.get("origin") or {}).get("kind") not in (None, "human"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    if not isinstance(c, str):
        return None
    t = REMINDER.sub("", c).strip()
    if t.startswith(AUTO):
        return None
    return t


def ts_of(s):
    try:
        return time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except ValueError:
        return 0


def touched(name, inp, cwd):
    """这次工具调用碰了哪些文件/目录"""
    out = list(targets(name, inp, cwd))
    if name in ("Grep", "Glob") and inp.get("path"):
        out.append(norm(inp["path"]))
    if name in EDIT_TOOLS:
        out.append(norm(inp.get("file_path") or inp.get("notebook_path")))
    return [p for p in out if p]


def parse(path):
    """一个会话文件 → 回合列表；每个回合 = 用户消息 + 请求（按出现顺序，同 id 合并）"""
    turns, by_id, cur = [], {}, None
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain"):
            continue
        t = user_text(d)
        if t is not None:
            cur = {"text": t, "ts": d.get("timestamp", ""), "reqs": []}
            turns.append(cur)
            continue
        if d.get("type") != "assistant" or cur is None:
            continue
        m = d.get("message") or {}
        mid, u = m.get("id"), m.get("usage")
        if not mid or not u or m.get("model") == "<synthetic>":
            continue
        r = by_id.get(mid)
        if r is None:
            r = by_id[mid] = {"mid": mid, "usage": dict(u), "tools": [], "cwd": d.get("cwd", "")}
            cur["reqs"].append(r)
        elif (u.get("output_tokens") or 0) > (r["usage"].get("output_tokens") or 0):
            r["usage"] = dict(u)
        for c in m.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                r["tools"].append((c.get("name"), c.get("input") or {}))
    return [x for x in turns if x["reqs"]]


def ctx(u):
    return (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)


def haiku_cost(reqs):
    """同一回合放到 Haiku 新会话的估计花费（Opus 等效 token）"""
    start, prev, total = ctx(reqs[0]["usage"]), None, 0.0
    for r in reqs:
        u = r["usage"]
        new_ctx = SYS_PROMPT + max(0, ctx(u) - start)
        total += (new_ctx * 2 if prev is None else prev * 0.1 + max(0, new_ctx - prev) * 2) + (u.get("output_tokens") or 0) * 5
        prev = new_ctx
    return total * HAIKU_RATIO


def rebuild(r, prev_end):
    """回合第一次请求里「缓存过期重建」多花的部分（换地方问也省不掉，会挪到下一回合）"""
    u = r["usage"]
    cw = u.get("cache_creation_input_tokens") or 0
    if not prev_end or not cw:
        return 0.0
    extra = max(0, cw - max(0, ctx(u) - prev_end))
    cc = u.get("cache_creation") or {}
    cw1h = cc.get("ephemeral_1h_input_tokens", cw) if cc else cw
    rate = 2.0 if cw1h >= cw - cw1h else 1.25
    return extra * (rate - 0.1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=0, help="只看最近几天（0 = 全部）")
    ap.add_argument("--max-chars", type=int, default=120)
    ap.add_argument("--max-reqs", type=int, default=5)
    ap.add_argument("--min-ctx", type=int, default=50_000)
    ap.add_argument("--samples", type=int, default=15)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    cutoff = time.time() - a.days * 86400 if a.days else 0
    files = [p for p in glob.glob(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl")) if os.path.getmtime(p) >= cutoff]
    seen_mids, seen_msg = set(), set()
    A = defaultdict(float)
    small, by_proj = [], defaultdict(lambda: defaultdict(float))
    nfiles = 0
    for p in sorted(files):
        turns = parse(p)
        if not turns:
            continue
        nfiles += 1
        proj = os.path.basename(os.path.dirname(p)).replace("D--AllPersonalProjects-", "").replace("C--Users-jason-", "~")
        before = set()  # 本会话之前回合碰过的文件
        prev_end = 0  # 上一回合最后一次请求的上下文 + 输出
        for tn in turns:
            fresh = [r for r in tn["reqs"] if r["mid"] not in seen_mids]
            seen_mids.update(r["mid"] for r in fresh)
            c = sum(cost(r["usage"]) for r in fresh)
            names = [nm for r in tn["reqs"] for nm, _ in r["tools"] if nm not in NEUTRAL]
            files_here = {fp for r in tn["reqs"] for nm, inp in r["tools"] for fp in touched(nm, inp, r["cwd"])}
            key = (tn["text"][:200], round(ts_of(tn["ts"])))
            dup = key in seen_msg
            seen_msg.add(key)
            A["total"] += c
            by_proj[proj]["total"] += c
            last = tn["reqs"][-1]["usage"]
            rb = rebuild(tn["reqs"][0], prev_end) if fresh and fresh[0] is tn["reqs"][0] else 0.0
            prev_end = ctx(last) + (last.get("output_tokens") or 0)
            if dup or not fresh:
                before |= files_here
                continue
            A["turns"] += 1
            edits = any(nm in EDIT_TOOLS or (nm in SHELLS and shell_kind(inp.get("command", "")) == "write")
                        for r in tn["reqs"] for nm, inp in r["tools"])
            c0 = ctx(tn["reqs"][0]["usage"])
            short = 0 < len(tn["text"]) <= a.max_chars
            if short and c0 >= a.min_ctx:
                A["short_big"] += c  # 只看「短消息 + 大上下文」，不管请求数和改不改
                A["n_short_big"] += 1
            if short and len(tn["reqs"]) <= a.max_reqs and not edits and c0 >= a.min_ctx:
                web_only = bool(names) and all(nm in WEB for nm in names)
                disjoint = bool(files_here) and not (files_here & before)
                tier = "web" if web_only else "disjoint" if disjoint else "notool" if not names else "followup"
                h = haiku_cost(fresh)
                small.append({"proj": proj, "text": tn["text"], "tier": tier, "cost": c, "haiku": h, "ctx": c0,
                              "nreq": len(tn["reqs"]), "tools": names, "rb": rb})
                for g in (A, by_proj[proj]):
                    g["loose"] += c
                    g["n_loose"] += 1
                    g["loose_haiku"] += max(0.0, c - h)
                    g["loose_rb"] += max(0.0, c - rb)
                    g["loose_haiku_rb"] += max(0.0, c - rb - h)
                    if tier in ("web", "disjoint"):
                        g["strict"] += c
                        g["n_strict"] += 1
                        g["strict_haiku"] += max(0.0, c - h)
                        g["strict_rb"] += max(0.0, c - rb)
                        g["strict_haiku_rb"] += max(0.0, c - rb - h)
                A["tier_" + tier] += c
                A["ntier_" + tier] += 1
                A["rbtier_" + tier] += max(0.0, c - rb)
            before |= files_here

    T = A["total"] or 1
    pc = lambda x: f"{x / T * 100:.2f}%"  # noqa: E731
    print(f"会话文件 {nfiles} 个，真实用户回合 {int(A['turns'])} 个，总等效 token {A['total'] / 1e6:.0f}M"
          f"（{'最近 %g 天' % a.days if a.days else '全部日志'}）")
    print(f"判定：消息 ≤ {a.max_chars} 字，回合 ≤ {a.max_reqs} 次请求，不改文件，开始时上下文 ≥ {a.min_ctx // 1000}K\n")
    print("可省 = 占总用量的比例。免费模型 = 回合花费全省；Haiku = 回合花费 - Haiku 新会话估计花费；"
          "「扣重建」= 再扣掉缓存过期重建（挪到下一回合，省不掉）")
    print(f"{'档':24}{'回合数':>7}{'占总用量':>9}{'Haiku可省':>11}{'免费可省':>10}{'Haiku扣重建':>13}{'免费扣重建':>12}")
    for lab, k in (("宽松：所有短的只读回合", "loose"), ("严格：只算岔题", "strict")):
        print(f"{lab:24}{int(A['n_' + k]):>7}{pc(A[k]):>9}{pc(A[k + '_haiku']):>11}{pc(A[k]):>10}"
              f"{pc(A[k + '_haiku_rb']):>13}{pc(A[k + '_rb']):>12}")
    print("\n宽松档拆开：")
    for k, lab in (("web", "只用 WebSearch/WebFetch（岔题）"), ("disjoint", "碰的文件和之前不重叠（岔题）"),
                   ("notool", "没用工具，靠上下文回答（追问）"), ("followup", "碰的文件之前碰过 / 别的工具（追问）")):
        print(f"  {lab:34}{int(A['ntier_' + k]):>6} 回合 {pc(A['tier_' + k]):>8}（扣重建 {pc(A['rbtier_' + k])}）")
    print(f"\n参照：只要「消息 ≤ {a.max_chars} 字 + 上下文 ≥ {a.min_ctx // 1000}K」（不管请求数、改不改文件）"
          f"{int(A['n_short_big'])} 回合，占 {pc(A['short_big'])}")
    if small:
        cs = sorted(x["cost"] for x in small)
        print(f"小问题回合：中位花费 {cs[len(cs) // 2] / 1e3:.0f}K，平均 {sum(cs) / len(cs) / 1e3:.0f}K，"
              f"开始上下文中位 {sorted(x['ctx'] for x in small)[len(small) // 2] / 1e3:.0f}K")
    print("\n按项目（宽松占该项目用量，前 8）：")
    for proj, g in sorted(by_proj.items(), key=lambda kv: -kv[1]["loose"])[:8]:
        print(f"  {proj[:40]:40}{int(g['n_loose']):>5} 回合  宽松 {g['loose'] / (g['total'] or 1) * 100:5.1f}%"
              f"  严格 {g['strict'] / (g['total'] or 1) * 100:5.1f}%  （项目 {g['total'] / 1e6:.0f}M）")
    rnd = random.Random(a.seed)
    lab = {"web": "严格·网页", "disjoint": "严格·新文件", "notool": "宽松·无工具", "followup": "宽松·追问"}
    strict = [x for x in small if x["tier"] in ("web", "disjoint")]
    for name, pool in (("全部小问题", small), ("其中严格档（岔题）", strict)):
        pick = rnd.sample(pool, min(a.samples, len(pool)))
        print(f"\n抽样{name} {len(pick)} 条（seed={a.seed}；[档] 花费 / 其中重建 / 开始上下文 / 请求数 / 工具）：")
        for x in pick:
            t = x["text"].replace("\n", " ")[:80]
            tools = ",".join(sorted(set(x["tools"])))[:30] or "-"
            print(f"  [{lab[x['tier']]}] {x['cost'] / 1e3:4.0f}K / {x['rb'] / 1e3:4.0f}K / {x['ctx'] / 1e3:4.0f}K / "
                  f"{x['nreq']} / {tools:18}  {t}")


if __name__ == "__main__":
    main()
