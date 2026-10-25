"""Codex 提前压缩的反事实模拟（只读本机 ~/.codex 日志，不花额度）。思路同 bench/simulate.py 的 compact_at。

日志里每次回复有一条 token_usage_record（input_tokens 含缓存命中、cached_input_tokens、output_tokens 含推理），
压缩有一条 compacted 事件。一种压缩点 T 只改变「上下文里还剩什么」：
  R = 被提前压缩拿掉的 token 数，这次请求的上下文 C' = C - R。
  - C' > T 时压缩：压缩请求本身花 C'×缓存读 + 摘要输出；之后上下文从 BASE 重新长起来，这次请求整段 C' 要按未缓存输入重新算
  - 日志里真实发生的压缩照旧（R 清零）
  - 悲观：压缩后还要把丢掉的文件重读一遍，额外 REREAD token 按未缓存输入算一次，上下文起点也高 REREAD
假设：agent 的行为不变（同样的轮数、同样的输出）。所以这里只算「省多少」，不评估质量。
权重（等效 token，和 iu/report.py 一致）：未缓存输入 1、缓存读 0.1、输出 8。

用法: python bench/codex_compact_sim.py [--days 14] [--json out.json]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import time

W_IN, W_CR, W_OUT = 1.0, 0.1, 8.0
SUMMARY_OUT = 4_000  # 压缩摘要的输出
REREAD = 50_000
POINTS = [80_000, 100_000, 120_000, 150_000, 180_000, 200_000]


def load(home: str, since: str, days: int):
    cut = time.time() - (days + 1) * 86400
    files = [p for sub in ("sessions", "archived_sessions")
             for p in glob.glob(os.path.join(home, sub, "**", "*.jsonl"), recursive=True) if os.path.getmtime(p) > cut]
    sessions, windows = [], []
    for path in files:
        evs, seen = [], set()
        for line in open(path, encoding="utf-8", errors="replace"):
            if '"token_usage_record"' not in line and '"compacted"' not in line and '"model_context_window"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t, p = d.get("type"), d.get("payload") or {}
            if t == "compacted":
                evs.append(("reset",))
            elif t == "token_usage_record":
                rid = p.get("response_id")
                if rid in seen:
                    continue
                seen.add(rid)
                u = p.get("usage") or {}
                evs.append(("req", u.get("input_tokens") or 0, u.get("cached_input_tokens") or 0,
                            u.get("output_tokens") or 0, d.get("timestamp", "") >= since))
            elif t == "event_msg" and p.get("type") == "token_count":
                w = (p.get("info") or {}).get("model_context_window")
                if w:
                    windows.append(w)
        if any(e[0] == "req" for e in evs):
            sessions.append(evs)
    return files, sessions, windows


def cost(inp, cached, out):
    return (inp - cached) * W_IN + cached * W_CR + out * W_OUT


def simulate(sessions, T, base, reread=0):
    b = s = 0.0
    comps = 0
    for evs in sessions:
        R = 0.0
        for e in evs:
            if e[0] == "reset":
                R = 0.0
                continue
            _, inp, cached, out, inwin = e
            C = inp
            Cp = max(C - R, 1_000)
            if T and Cp > T:
                c = Cp * W_CR + SUMMARY_OUT * W_OUT + (base + reread) * W_IN + out * W_OUT
                R = C - (base + reread)
                comps += 1
            else:
                new = min(inp - cached, Cp)
                c = new * W_IN + (Cp - new) * W_CR + out * W_OUT
            if inwin:
                b += cost(inp, cached, out)
                s += c
    return b, s, comps


def facts(sessions):
    """真实压缩：压缩前最后一次请求的上下文、压缩后第一次请求的上下文"""
    pre, post = [], []
    for evs in sessions:
        last = None
        for i, e in enumerate(evs):
            if e[0] == "reset":
                if last:
                    pre.append(last)
                nxt = next((x for x in evs[i + 1:] if x[0] == "req"), None)
                if nxt:
                    post.append(nxt[1])
            else:
                last = e[1]
    reqs = [e[1] for s in sessions for e in s if e[0] == "req" and e[4]]
    return pre, post, reqs


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json")
    args = ap.parse_args()
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - args.days * 86400))
    files, sessions, windows = load(home, since, args.days)
    pre, post, reqs = facts(sessions)
    q = lambda xs, f: sorted(xs)[int(f * (len(xs) - 1))] if xs else 0
    base = int(statistics.median(post)) if post else 30_000
    print(f"{len(files)} 个日志文件，最近 {args.days} 天 {len(reqs)} 次请求；上下文窗口 {sorted(set(windows))[-3:]}")
    print(f"每次请求的上下文：中位 {q(reqs, .5) // 1000}K、P90 {q(reqs, .9) // 1000}K、最大 {max(reqs or [0]) // 1000}K；"
          f"超过 150K 的请求 {sum(r > 150_000 for r in reqs)} 次")
    print(f"真实压缩 {len(pre)} 次：压缩前上下文中位 {q(pre, .5) // 1000}K（{q(pre, .1) // 1000}K~{q(pre, .9) // 1000}K），"
          f"压缩后第一次请求中位 {base // 1000}K")
    b0, s0, _ = simulate(sessions, 0, base)
    parts = [sum(f(e) for x in sessions for e in x if e[0] == "req" and e[4]) for f in
             (lambda e: (e[1] - e[2]) * W_IN, lambda e: e[2] * W_CR, lambda e: e[3] * W_OUT)]
    print("实际花费构成：未缓存输入 {:.0%}、缓存读 {:.0%}、输出 {:.0%}".format(*[x / max(sum(parts), 1) for x in parts]))
    print(f"核对：什么都不改时模拟 {s0 / 1e6:.1f}M = 实际 {b0 / 1e6:.1f}M")
    rows = []
    print(f"\n{'压缩点':>8}{'节省':>8}{'悲观(+重读50K)':>16}{'额外压缩次数':>12}")
    for T in POINTS:
        b, s, n = simulate(sessions, T, base)
        ok = base + REREAD < 0.7 * T  # 起点太接近压缩点时会一轮接一轮地压缩，悲观假设没有意义
        sp = simulate(sessions, T, base, REREAD)[1] if ok else None
        rows.append({"compact_at": T, "saving": 1 - s / b, "saving_pess": None if sp is None else 1 - sp / b,
                     "compactions": n})
        print(f"{T // 1000:>7}K{1 - s / b:>8.0%}{'—' if sp is None else format(1 - sp / b, '.0%'):>16}{n:>12}")
    if args.json:
        json.dump({"files": len(files), "requests": len(reqs), "base": base, "real_compactions": len(pre),
                   "pre_median": q(pre, .5), "actual": b0, "rows": rows}, open(args.json, "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
