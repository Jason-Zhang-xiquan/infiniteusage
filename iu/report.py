"""把聚合好的行算成报告：官方额度 + 重置倒计时、按模型/项目/天的用量、浪费信号。

「等效 token」：把各种 token 按价格折算成「未缓存输入 token」，这样缓存读、缓存写、输出能放在一起比。
权重是按公开 API 价格比例估的，不是官方额度算法，只用来排序「钱/额度花在哪」：
  Claude：输入 1、缓存写 5 分钟 1.25、缓存写 1 小时 2、缓存读 0.1、输出 5
  Codex ：未缓存输入 1、缓存读 0.1、输出 8（输出含推理 token）
"""

from __future__ import annotations

import datetime as dt
import time
import unicodedata
from collections import defaultdict

from .limits import claude_windows, codex_windows, fmt_left
from .i18n import t as tr

WEIGHTS = {
    "claude": {"in": 1.0, "cw5": 1.25, "cw1h": 2.0, "cr": 0.1, "out": 5.0},
    "codex": {"in": 1.0, "cr": 0.1, "out": 8.0},
}


def eq(tool: str, b: dict) -> float:
    return sum(b.get(k, 0) * w for k, w in WEIGHTS.get(tool, {}).items())


def human(n: float) -> str:
    n = float(n)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:.0f}"


def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def table(headers: list, rows: list, right: set = None) -> str:
    right = right if right is not None else set(range(1, len(headers)))
    cols = [headers] + [[str(c) for c in r] for r in rows]
    widths = [max(width(r[i]) for r in cols) for i in range(len(headers))]

    def fmt(r):
        out = []
        for i, c in enumerate(r):
            pad = " " * (widths[i] - width(c))
            out.append(pad + c if i in right else c + pad)
        return "  ".join(out).rstrip()

    return "\n".join([fmt(headers), fmt(["-" * w for w in widths])] + [fmt(r) for r in cols[1:]])


def local_hour(key: str) -> dt.datetime:
    t = dt.datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=dt.timezone.utc)
    return t.astimezone()


def add_into(acc: dict, b: dict):
    for k, v in b.items():
        acc[k] = acc.get(k, 0) + v


def claude_block_estimate(rows: list, now: dt.datetime):
    """没有官方快照时，按 ccusage 的办法估 5 小时窗口：第一条活动所在整点开窗，5 小时后关；之后的第一条活动再开新窗。
    只看得到这几台机器的日志，claude.ai 网页、手机上的用量看不到，所以只是估算。"""
    hours = sorted({local_hour(d["hour"]) for d, _ in rows if d["tool"] == "claude" and d["hour"]})
    start = None
    for h in hours:
        if start is None or h >= start + dt.timedelta(hours=5):
            start = h
    if start is None or now >= start + dt.timedelta(hours=5):
        return None
    used = {}
    for d, b in rows:
        if d["tool"] == "claude" and d["hour"] and local_hour(d["hour"]) >= start:
            add_into(used, b)
    return start, start + dt.timedelta(hours=5), used


def build(rows: list, claude_snap: dict, codex_limits: dict, days: int, hosts: list) -> str:
    now_ts = time.time()
    now = dt.datetime.now().astimezone()
    since = now - dt.timedelta(days=days)
    recent = [(d, b) for d, b in rows if d["hour"] and local_hour(d["hour"]) >= since]
    out = [tr(f"InfiniteUsage 额度账本 · {now:%Y-%m-%d %H:%M} · 最近 {days} 天 · 机器：{'、'.join(hosts)}",
              f"InfiniteUsage usage ledger · {now:%Y-%m-%d %H:%M} · last {days} days · machines: {', '.join(hosts)}"), ""]

    # ---- 官方额度 ----
    out.append(tr("== 官方额度和重置倒计时 ==", "== Official limits and resets =="))
    lim_rows = []
    for label, pct, reset in codex_windows(codex_limits, now_ts):
        age = now_ts - dt.datetime.fromisoformat(codex_limits["ts"].replace("Z", "+00:00")).timestamp()
        lim_rows.append(["Codex", label, f"{pct:.0f}%", bar(pct), when(reset), fmt_left(reset - now_ts),
                         tr(f"{fmt_left(age)}前", f"{fmt_left(age)} ago")])
    if claude_snap:
        for label, pct, reset in claude_windows(claude_snap, now_ts):
            lim_rows.append(["Claude", label, f"{pct:.0f}%", bar(pct), when(reset), fmt_left(reset - now_ts),
                             tr(f"{fmt_left(now_ts - claude_snap['ts'])}前", f"{fmt_left(now_ts - claude_snap['ts'])} ago")])
    if lim_rows:
        out.append(table(tr(["工具", "窗口", "已用", "", "重置时间", "还剩", "快照"], ["tool", "window", "used", "", "resets", "left", "snapshot"]), lim_rows, right={2}))
    if not claude_snap:
        out.append(tr("Claude：还没有官方快照（装上记录器：iu install-statusline，之后每次 Claude Code 刷新状态栏都会记一次）", "Claude: no official snapshot yet (install the recorder: iu install-statusline; it records on every status line refresh)"))
        est = claude_block_estimate(rows, now)
        if est:
            start, end, used = est
            left = fmt_left((end - now).total_seconds())
            out.append(tr(f"  估算 5 小时窗口：{start:%H:%M} 开始，{end:%H:%M} 重置（还剩 {left}），"
                          f"本窗口已用 {human(eq('claude', used))} 等效 token（只算读到的机器，claude.ai 网页和手机上的用量看不到）",
                          f"  Estimated 5h window: started {start:%H:%M}, resets {end:%H:%M} ({left} left), "
                          f"{human(eq('claude', used))} eq. tokens used (machines read only; claude.ai web and mobile not included)"))
    out.append("")

    # ---- 按工具 × 模型 × effort ----
    by_model = defaultdict(dict)
    for d, b in recent:
        add_into(by_model[(d["tool"], d["model"], d["effort"])], b)
    total_eq = {t: sum(eq(t, b) for (tt, _, _), b in by_model.items() if tt == t) for t in WEIGHTS}
    out.append(tr("== 按模型（等效 token = 按价格折算成未缓存输入；缓存命中 = 缓存读 / 全部输入）==", "== By model (eq. tokens = priced as uncached input; cache hit = cache read / all input) =="))
    mrows = []
    for (tool, model, effort), b in sorted(by_model.items(), key=lambda kv: -eq(kv[0][0], kv[1])):
        e = eq(tool, b)
        if e < 1:
            continue
        mrows.append([tool, model or "?", effort or "-", f"{b.get('n', 0):,}", human(b.get("in", 0)),
                      human(b.get("cw5", 0) + b.get("cw1h", 0)), human(b.get("cr", 0)), human(b.get("out", 0)),
                      pct_str(hit(b)), human(e), pct_str(e / total_eq[tool] if total_eq[tool] else 0)])
    out.append(table(tr(["工具", "模型", "effort", "请求", "新输入", "缓存写", "缓存读", "输出", "缓存命中", "等效", "占本工具"], ["tool", "model", "effort", "requests", "input", "cache write", "cache read", "output", "cache hit", "eq.", "share"]),
                     mrows, right=set(range(3, 11))))
    out.append("")

    # ---- 按项目 ----
    by_proj = defaultdict(dict)
    for d, b in recent:
        add_into(by_proj[(d["tool"], d["project"])], b)
    prow = sorted(by_proj.items(), key=lambda kv: -eq(kv[0][0], kv[1]))[:10]
    out.append(tr("== 用量最多的项目 ==", "== Top projects =="))
    out.append(table(tr(["工具", "项目", "请求", "等效", "占本工具", "缓存命中", "平均每次请求上下文"], ["tool", "project", "requests", "eq.", "share", "cache hit", "context / request"]),
                     [[t, p or "?", f"{b.get('n', 0):,}", human(eq(t, b)),
                       pct_str(eq(t, b) / total_eq[t] if total_eq[t] else 0), pct_str(hit(b)),
                       human(ctx_per_req(b))] for (t, p), b in prow], right={2, 3, 4, 5, 6}))
    out.append("")

    # ---- 按天 ----
    by_day = defaultdict(lambda: defaultdict(dict))
    for d, b in recent:
        add_into(by_day[local_hour(d["hour"]).strftime("%m-%d %a")][d["tool"]], b)
    out.append(tr("== 每天等效 token ==", "== Eq. tokens per day =="))
    out.append(table(tr(["日期", "Claude", "Codex", "合计"], ["date", "Claude", "Codex", "total"]),
                     [[day, human(eq("claude", v.get("claude", {}))), human(eq("codex", v.get("codex", {}))),
                       human(eq("claude", v.get("claude", {})) + eq("codex", v.get("codex", {})))]
                      for day, v in sorted(by_day.items())]))
    out.append("")

    out.append(tr("== 浪费信号（按影响排序）==", "== Waste signals (by impact) =="))
    signals = waste_signals(recent, total_eq)
    out += [f"{i}. {s}" for i, s in enumerate(signals, 1)] or [tr("没发现明显的浪费", "no obvious waste found")]
    return "\n".join(out)


def bar(pct: float, n: int = 10) -> str:
    full = int(round(min(max(pct, 0), 100) / 100 * n))
    return "█" * full + "░" * (n - full)


def when(reset: float) -> str:
    return dt.datetime.fromtimestamp(reset).strftime("%m-%d %H:%M") if reset else "?"


def hit(b: dict) -> float:
    total = b.get("in", 0) + b.get("cw5", 0) + b.get("cw1h", 0) + b.get("cr", 0)
    return b.get("cr", 0) / total if total else 0.0


def ctx_per_req(b: dict) -> float:
    n = b.get("n", 0)
    return (b.get("in", 0) + b.get("cw5", 0) + b.get("cw1h", 0) + b.get("cr", 0)) / n if n else 0


def pct_str(x: float) -> str:
    return f"{x * 100:.0f}%"


def waste_signals(recent: list, total_eq: dict) -> list:
    """简单规则，每条给出影响（占本工具等效 token 的比例）和可以怎么做"""
    found = []  # (影响分, 文字)
    agg = defaultdict(lambda: defaultdict(dict))  # tool -> 维度值 -> 桶
    sessions = defaultdict(dict)
    for d, b in recent:
        t = d["tool"]
        add_into(agg[t][("effort", d["effort"])], b)
        add_into(agg[t][("side", d["side"])], b)
        add_into(agg[t][("all", "")], b)
        add_into(sessions[(t, d["session"], d["project"])], b)

    for t in ("codex", "claude"):
        tot = total_eq.get(t) or 0
        if not tot:
            continue
        name = "Codex" if t == "codex" else "Claude"
        allb = agg[t][("all", "")]
        # 输出（含推理）占比
        out_share = allb.get("out", 0) * WEIGHTS[t]["out"] / tot
        if t == "codex":
            for effort in ("ultra", "xhigh", "max"):
                b = agg[t].get(("effort", effort))
                if b and eq(t, b) / tot > 0.3:
                    think_share = b.get("think", 0) / max(b.get("out", 0), 1)
                    found.append((eq(t, b) / tot,
                                  tr(f"{name} {pct_str(eq(t, b) / tot)} 的等效用量跑在 effort={effort}，其中输出的 {pct_str(think_share)} 是推理 token。"
                                     f"日常改代码、跑命令用 medium/high，只在规划和难 bug 上开 {effort}，通常是最大的一刀",
                                     f"{name}: {pct_str(eq(t, b) / tot)} of eq. usage runs at effort={effort}; {pct_str(think_share)} of output is reasoning. "
                                     f"Use medium/high for everyday edits and commands, {effort} only for planning and hard bugs; usually the biggest cut")))
        if out_share > 0.35:
            found.append((out_share * 0.5, tr(f"{name} 输出（含推理）占等效用量 {pct_str(out_share)}：输出比输入贵 {WEIGHTS[t]['out']:.0f} 倍，"
                                              "让它少写长解释、少重复贴代码、降低推理强度",
                                              f"{name}: output (incl. reasoning) is {pct_str(out_share)} of eq. usage; output costs {WEIGHTS[t]['out']:.0f}x input. "
                                              "Ask for shorter explanations, less re-pasted code, lower effort")))
        reread = allb.get("cr", 0) * WEIGHTS[t]["cr"] / tot
        if reread > 0.3:
            found.append((reread, tr(f"{name} 等效用量的 {pct_str(reread)} 是每轮重读已缓存的上下文（平均每次请求 {human(ctx_per_req(allb))}）："
                                     "缓存读虽然只要 1 折，但上下文越长、轮数越多，累计就越大。缩短上下文是最大的杠杆",
                                     f"{name}: {pct_str(reread)} of eq. usage is re-reading cached context every turn ({human(ctx_per_req(allb))} per request). "
                                     "Cache reads cost 10%, but longer context x more turns adds up. Shorter context is the biggest lever")))
        h = hit(allb)
        if h < 0.85:
            miss = (allb.get("in", 0) * WEIGHTS[t]["in"] + allb.get("cw5", 0) * 1.25 + allb.get("cw1h", 0) * 2) / tot
            found.append((miss, tr(f"{name} 缓存命中只有 {pct_str(h)}，没命中的输入占等效用量 {pct_str(miss)}。"
                                   "常见原因：中途切模型/effort、增删 MCP、/compact、离开超过缓存有效期再回来",
                                   f"{name}: cache hit only {pct_str(h)}; uncached input is {pct_str(miss)} of eq. usage. "
                                   "Common causes: switching model/effort mid-session, adding/removing MCP, /compact, coming back after the cache expired")))
        sub = agg[t].get(("side", "sub"))
        if sub and eq(t, sub) / tot > 0.25:
            found.append((eq(t, sub) / tot * 0.4, tr(f"{name} 子 agent 占 {pct_str(eq(t, sub) / tot)}：子 agent 只有 5 分钟缓存、每次重建上下文，"
                                                     "能一个 agent 干完的不要拆，拆的话给子 agent 用便宜模型",
                                                     f"{name}: sub-agents are {pct_str(eq(t, sub) / tot)}; they have a 5-minute cache and rebuild context each time. "
                                                     "Don't split what one agent can do; if you split, give sub-agents a cheaper model")))
        if t == "claude":
            cw5, cw1h = allb.get("cw5", 0), allb.get("cw1h", 0)
            if cw5 > cw1h and cw5 * 1.25 / tot > 0.1:
                found.append((cw5 * 1.25 / tot, tr(f"Claude 缓存写里 5 分钟缓存占多数（{human(cw5)} vs 1 小时 {human(cw1h)}），"
                                                    "说明很多是子 agent 或用量超额后的请求，停顿超过 5 分钟就要重建",
                                                    f"Claude: most cache writes are 5-minute ({human(cw5)} vs 1-hour {human(cw1h)}), "
                                                    "i.e. sub-agents or over-limit requests; a pause over 5 minutes rebuilds the cache")))
        # 上下文特别大的会话
        big = [(k, b) for k, b in sessions.items() if k[0] == t and b.get("n", 0) >= 20 and ctx_per_req(b) > 150_000]
        if big:
            share = sum(eq(t, b) for _, b in big) / tot
            top = sorted(big, key=lambda kb: -eq(t, kb[1]))[:3]
            desc = tr("、", ", ").join(f"{k[2] or '?'} ({human(ctx_per_req(b))}/req)" for k, b in top)
            found.append((share * 0.5, tr(f"{name} 有 {len(big)} 个会话平均每次请求带 15 万以上 token 的上下文，占 {pct_str(share)}："
                                          f"{desc}。换话题就 /clear 或开新会话，长任务分段交接",
                                          f"{name}: {len(big)} sessions average over 150K tokens of context per request, {pct_str(share)} of usage: "
                                          f"{desc}. /clear or start a new session when the topic changes")))
    found.sort(key=lambda x: -x[0])
    return [text for _, text in found]
