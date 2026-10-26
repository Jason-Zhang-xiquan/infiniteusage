"""Claude Code 提前压缩：装之前在你自己的日志上模拟选压缩点（iu install），装之后按会话算实际省了多少（iu compare）。

只读本机日志（~/.claude/projects/**/*.jsonl，不含子 agent），不花额度。

模拟（bench/simulate.py 的压缩部分）：每次请求的上下文、新写入、缓存读、输出都是日志里的实测值。
上下文超过压缩点就压缩：多花一次请求（读一遍旧上下文 + 写摘要），这次请求要把新的上下文重新写进缓存，
之后上下文从 base（约 3 万）重新长起来。假设 agent 的行为不变，所以只算钱，不评估质量。

按会话算账：对装之后真实发生的每一次压缩，算「不压缩的话，后面这些请求要多带多少上下文」，
减去压缩本身的花费（压缩那次请求不写进日志，按 compact_boundary 里的 preTokens / postTokens 估）。
压缩完会话很快就结束的，会算成亏。
"""

from __future__ import annotations

import glob
import json
import os
import time

W_IN, W_CW1H, W_CW5M, W_CR, W_OUT = 1.0, 2.0, 1.25, 0.1, 5.0  # 等效 token，和 iu 账本一致
COMPACT_BASE = 30_000  # 压缩后的起点：系统提示约 1.2 万 + 摘要 + 最近改过的文件
COMPACT_SUMMARY_OUT = 4_000
PESSIMISTIC = {"base": 60_000, "summary_out": 8_000}  # 悲观假设：压缩后起点翻倍、摘要翻倍
CANDIDATES = (300_000, 400_000, 500_000, 600_000)  # 不低于 300K：质量测试（失忆鹈鹕）是在 300K 及更频繁的压缩下做的
MIN_REQUESTS = 300  # 日志里的请求少于这个数就不模拟，用默认值
MIN_SAVING = 0.03  # 模拟省不到 3% 就不改压缩设置
NEAR_BEST = 0.02  # 和最好的差不到 2 个百分点时，选更大的压缩点（压缩少，丢东西的风险小）
DEFAULT_LIMIT = 950_000  # 不改设置时 Claude Code 约在 967K 自己压缩：反事实的上下文到这里就不再算省
OURS_RATIO = 0.9  # 压缩前上下文不到压缩点的 90%：手动 /compact 或小窗口模型的默认压缩，不算本工具


def root() -> str:
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"), "projects")


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))


def ctx(u: dict) -> float:
    return (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)


def new_tokens(u: dict) -> float:
    return (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)


def write_weight(u: dict) -> float:
    cc = u.get("cache_creation") or {}
    return W_CW5M if cc and not cc.get("ephemeral_1h_input_tokens") and cc.get("ephemeral_5m_input_tokens") else W_CW1H


def cost(u: dict) -> float:
    cc = u.get("cache_creation") or {}
    cw = u.get("cache_creation_input_tokens") or 0
    cw1h = cc.get("ephemeral_1h_input_tokens", cw) if cc else cw
    return ((u.get("input_tokens") or 0) * W_IN + cw1h * W_CW1H + (cw - cw1h) * W_CW5M
            + (u.get("cache_read_input_tokens") or 0) * W_CR + (u.get("output_tokens") or 0) * W_OUT)


def load_sessions(since: float, base: str = None) -> list:
    """每个主会话日志 → {"path", "events": [("req", usage, ts) | ("reset", compactMetadata, ts)]}。
    只读最近改过的文件；同一条回复会分成多行写，按 message id 只算一次。
    接着上次的会话继续时会把旧记录抄进新文件，所以 message id 和压缩记录跨文件也只算一次"""
    base = base or root()
    out, seen = [], set()
    files = [p for p in glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True)
             if "subagents" not in p.replace("\\", "/").split("/") and os.path.getmtime(p) >= since]
    for path in sorted(files, key=os.path.getmtime):  # 先读旧文件，抄过去的记录算在原来的会话里
        evs, cwd = [], ""
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = list(f)
        for line in lines:
            if '"assistant"' not in line and '"compact_boundary"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            cwd = cwd or d.get("cwd") or ""
            if d.get("isSidechain"):
                continue
            if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                meta = d.get("compactMetadata") or {}
                key = ("reset", d.get("timestamp", ""), meta.get("preTokens"))
                if key in seen:
                    continue
                seen.add(key)
                evs.append(("reset", meta, d.get("timestamp", "")))
            elif d.get("type") == "assistant":
                msg = d.get("message") or {}
                mid, u = msg.get("id"), msg.get("usage")
                if not mid or not u or mid in seen or msg.get("model") == "<synthetic>":
                    continue
                seen.add(mid)
                evs.append(("req", u, d.get("timestamp", "")))
        if evs:
            out.append({"path": path, "cwd": cwd, "events": evs})
    return out


def simulate(sessions: list, compact_at: int, since: str = "", until: str = "~", base: int = COMPACT_BASE,
             summary_out: int = COMPACT_SUMMARY_OUT) -> dict:
    """在真实日志上重放：上下文（扣掉已经压掉的部分）超过 compact_at 就压缩。返回实际 / 模拟的等效 token"""
    actual = sim = 0.0
    n = comps = 0
    for s in sessions:
        R = 0.0  # 被模拟的压缩拿掉的 token
        for e in s["events"]:
            if e[0] == "reset":  # 真实发生的压缩：模拟也从这里重新长
                R = 0.0
                continue
            u, ts = e[1], e[2]
            C = ctx(u)
            Cp = max(C - R, 5_000)
            broke, extra = False, 0.0
            if compact_at and Cp > compact_at:
                extra = Cp * W_CR + summary_out * W_OUT  # 压缩请求本身
                R, Cp, broke = C - base, base, True
                if since <= ts <= until:
                    comps += 1
            if not since <= ts <= until:
                continue
            n += 1
            out = u.get("output_tokens") or 0
            actual += cost(u)
            if broke:  # 这次请求要把整段新上下文写进缓存
                sim += Cp * W_CW1H + out * W_OUT + extra
            else:
                nw = new_tokens(u)
                new = min(nw, Cp)
                sim += (cost(u) - (u.get("cache_read_input_tokens") or 0) * W_CR - out * W_OUT) * (new / max(nw, 1)) \
                    + max(Cp - new, 0) * W_CR + out * W_OUT
    return {"requests": n, "actual": actual, "sim": sim, "saving": 1 - sim / actual if actual else 0.0,
            "compactions": comps}


def choose(sessions: list, since: str, until: str = "~") -> dict:
    """返回 {"compact_at": 选的压缩点（0 = 不改）, "reason": 原因代码, "rows": 每个候选的模拟结果, "requests", "max_ctx"}"""
    reqs = [e[1] for s in sessions for e in s["events"] if e[0] == "req" and since <= e[2] <= until]
    res = {"compact_at": 0, "rows": [], "requests": len(reqs), "max_ctx": max((ctx(u) for u in reqs), default=0)}
    if len(reqs) < MIN_REQUESTS:
        return {**res, "compact_at": CANDIDATES[0], "reason": "few"}
    for at in CANDIDATES:
        r = simulate(sessions, at, since, until)
        p = simulate(sessions, at, since, until, **PESSIMISTIC)
        res["rows"].append({"compact_at": at, "saving": r["saving"], "pessimistic": p["saving"],
                            "compactions": r["compactions"]})
    ok = [r for r in res["rows"] if r["pessimistic"] > 0]
    best = max((r["saving"] for r in ok), default=0.0)
    if best < MIN_SAVING:
        return {**res, "reason": "short" if res["max_ctx"] <= CANDIDATES[0] else "no_gain"}
    pick = max(r["compact_at"] for r in ok if r["saving"] >= best - NEAR_BEST)
    return {**res, "compact_at": pick, "reason": "sim"}


def audit(sessions: list, t0: float, t1: float, compact_at: int) -> dict:
    """装之后每一次压缩的账：不压缩的话后面要多花多少，减去压缩本身。返回 {"items": [...], 汇总}"""
    lo, hi = iso(t0), iso(t1)
    items, other, short, touched, total = [], 0, 0, 0, 0.0
    for s in sessions:
        evs = s["events"]
        reqs_after = [e for e in evs if e[0] == "req" and lo <= e[2] <= hi]
        if not reqs_after:
            continue
        touched += 1
        total += sum(cost(e[1]) for e in reqs_after)
        if max(ctx(e[1]) for e in reqs_after) < compact_at * OURS_RATIO and not any(e[0] == "reset" for e in evs):
            short += 1
        for i, e in enumerate(evs):
            if e[0] != "reset" or not (lo <= e[2] <= hi):
                continue
            meta = e[1]
            prev = next((evs[j][1] for j in range(i - 1, -1, -1) if evs[j][0] == "req"), None)
            pre = meta.get("preTokens") or (ctx(prev) if prev else 0)
            if meta.get("trigger") == "manual" or pre < compact_at * OURS_RATIO:
                other += 1
                continue
            post = []
            for x in evs[i + 1:]:
                if x[0] == "reset":
                    break
                post.append(x[1])
            post_tokens = meta.get("postTokens") or (ctx(post[0]) if post else COMPACT_BASE)
            removed = max(pre - post_tokens, 0)
            spent = pre * W_CR + min(post_tokens, 8_000) * W_OUT  # 压缩那次请求（日志里没有，按读一遍旧上下文 + 写摘要估）
            new_est = min(new_tokens(prev), 20_000) if prev else 0
            saved = 0.0
            for k, u in enumerate(post):
                C = ctx(u)
                if C + removed > DEFAULT_LIMIT:  # 不改设置也会在这里自己压缩：之后两边一样
                    break
                if k == 0:  # 实际：整段新上下文重写进缓存；不压缩的话：旧上下文读缓存，只写新加的
                    cf = pre * W_CR + min(new_tokens(u), new_est) * write_weight(u) + (u.get("output_tokens") or 0) * W_OUT
                else:  # 缓存命中就按读算，没命中（隔太久过期了）就按写算
                    hit = (u.get("cache_read_input_tokens") or 0) >= C / 2
                    cf = cost(u) + removed * (W_CR if hit else write_weight(u))
                saved += cf - cost(u)
            items.append({"ts": e[2], "path": s["path"], "cwd": s.get("cwd", ""), "pre": pre, "post_tokens": post_tokens, "after_requests": len(post),
                          "spent": spent, "saved": saved, "net": saved - spent})
    items.sort(key=lambda x: x["ts"])
    return {"items": items, "sessions": touched, "short_sessions": short, "other_compactions": other, "total": total,
            "spent": sum(x["spent"] for x in items), "saved": sum(x["saved"] for x in items),
            "net": sum(x["net"] for x in items)}
