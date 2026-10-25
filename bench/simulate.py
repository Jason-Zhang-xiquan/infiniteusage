"""反事实模拟：在你自己的真实 Claude Code 日志上，算各种做法能省多少（只读本机日志，不花额度）。

做法：每次请求的上下文大小、新写入、缓存读、输出都是日志里的实测值。一种做法只改变「上下文里还剩什么」：
  R = 当前被这种做法拿掉的 token 数，于是这次请求的上下文 C' = C - R。
  - 拿掉的是已经缓存的前缀（压缩、遮蔽、删旧思考）→ 这次请求要把整段 C' 重新写进缓存（按缓存写计价），记一次「缓存重建」
  - 外包长输出是在输出进来的那一刻就换成摘要，不破坏缓存
  - 压缩额外要花一次请求：读一遍旧上下文 + 写约 4K token 的摘要；之后上下文从 base（约 3 万）重新长起来
假设：agent 的行为不变（同样的轮数、同样的输出）。所以这里只算「省多少」，**不评估质量**。

权重（等效 token，和 iu 账本一致）：新输入 1、缓存写 2（1 小时）/1.25（5 分钟）、缓存读 0.1、输出 5。
用法: python bench/simulate.py [--days 7] [--json out.json]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict

K_ASCII, K_NON = 0.40, 1.53  # context_breakdown.py 用你的日志拟合出的每字符 token 数
W_IN, W_CW1H, W_CW5M, W_CR, W_OUT = 1.0, 2.0, 1.25, 0.1, 5.0
SUMMARY_CHARS = 1300  # 外包摘要的实测长度
COMPACT_BASE = 30_000  # 压缩后的起点：系统提示约 1.2 万 + 摘要 + 最近改过的文件
COMPACT_SUMMARY_OUT = 4_000
CLEAR_AT_LEAST = 20_000  # 遮蔽、删思考攒够这么多才动手，免得频繁重建缓存
WORKER_BASE = 35_000  # 子 agent 起步上下文：自己的系统提示 + 工具 + CLAUDE.md + 任务说明
RESULT_TOKENS = 3_000  # 子 agent 交回主会话的结果


def est_chars(s: str) -> float:
    a = sum(1 for ch in s if ord(ch) < 128)
    return a * K_ASCII + (len(s) - a) * K_NON


def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(text_of(x) for x in c)
    if isinstance(c, dict):
        if c.get("type") == "image":
            return ""
        return text_of(c.get("text") or c.get("content") or "")
    return ""


def is_prompt(d, cont) -> bool:
    """用户真正打的一句话（不是工具结果、不是系统注入、不是压缩后的续写说明）"""
    if d.get("isMeta") or d.get("isSidechain"):
        return False
    if isinstance(cont, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in cont):
            return False
        cont = text_of(cont)
    if not isinstance(cont, str) or not cont.strip():
        return False
    return not cont.startswith(("<local-command", "<command-", "This session is being continued"))


def ts_secs(ts: str) -> float:
    try:
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0.0


def load_sessions(root, since):
    """每个日志文件 → 事件列表：('out', tokens, chars) 工具输出 / ('req', usage, visible_tokens) / ('reset',)"""
    files = [p for p in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
             if os.path.getmtime(p) > time.time() - 9 * 86400]
    sessions = []
    for path in files:
        evs, msgs, order = [], {}, []
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("isSidechain") and "subagents" not in path:
                continue
            t = d.get("type")
            if t == "system" and d.get("subtype") == "compact_boundary":
                evs.append(("reset",))
            elif t == "user":
                cont = (d.get("message") or {}).get("content")
                if is_prompt(d, cont) and "subagents" not in path:
                    evs.append(("turn",))
                if isinstance(cont, list):
                    for c in cont:
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            s = text_of(c.get("content"))
                            evs.append(("out", est_chars(s), len(s)))
            elif t == "assistant":
                msg = d.get("message") or {}
                mid, u = msg.get("id"), msg.get("usage")
                if not mid or not u or msg.get("model") == "<synthetic>":
                    continue
                vis = 0.0
                for c in msg.get("content") or []:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            vis += est_chars(c.get("text") or "")
                        elif c.get("type") == "tool_use":
                            vis += est_chars(json.dumps(c.get("input") or {}, ensure_ascii=False))
                if mid not in msgs:
                    msgs[mid] = {"usage": u, "vis": 0.0, "in_window": d.get("timestamp", "") >= since,
                                 "ts": d.get("timestamp", "")}
                    evs.append(("req", mid))
                msgs[mid]["vis"] += vis  # 同一条回复会分成多行写，正文/工具调用分开记
        sessions.append([e if e[0] != "req" else ("req", msgs[e[1]]) for e in evs])
    return sessions


def cost_actual(u):
    cc = u.get("cache_creation") or {}
    cw = u.get("cache_creation_input_tokens") or 0
    cw1h = cc.get("ephemeral_1h_input_tokens", cw) if cc else cw
    return ((u.get("input_tokens") or 0) * W_IN + cw1h * W_CW1H + (cw - cw1h) * W_CW5M
            + (u.get("cache_read_input_tokens") or 0) * W_CR + (u.get("output_tokens") or 0) * W_OUT)


def simulate(sessions, policy):
    """policy: dict(offload=bool, mask_keep=N|None, think_keep=K|None, compact_at=T|None, think_scale=float)"""
    base_cost = pol_cost = 0.0
    breaks = compactions = 0
    dmin = policy.get("delegate_min")
    wbase = policy.get("worker_base", WORKER_BASE)
    free = policy.get("free_success", 0.0)
    for evs in sessions:
        R = 0.0
        outs, thinks = [], []  # [当前 token 数, 是否已拿掉]
        nturn = []  # 每句话之后有几次请求
        for e in evs:
            if e[0] == "turn":
                nturn.append(0)
            elif e[0] == "req" and nturn:
                nturn[-1] += 1
        ti, k, deleg = -1, 0, False
        C0 = Cmain = t0 = 0.0
        for e in evs:
            if e[0] == "turn":
                ti, k = ti + 1, 0
                deleg = bool(dmin) and nturn[ti] > dmin
                continue
            if e[0] == "reset":
                R, outs, thinks = 0.0, [], []
                deleg = False
                continue
            if deleg and k >= 1 and e[0] == "out":
                continue  # 子 agent 里的工具输出，不进主会话
            if deleg and e[0] == "req" and k >= 1:
                k += 1
                m = e[1]
                u = m["usage"]
                inp, cw, cr = (u.get("input_tokens") or 0), (u.get("cache_creation_input_tokens") or 0), \
                    (u.get("cache_read_input_tokens") or 0)
                out = u.get("output_tokens") or 0
                C = inp + cw + cr
                if k < nturn[ti]:  # 子 agent 干活：上下文从 wbase 起步，只带这句话之内长出来的部分
                    Cw = wbase + max(C - C0, 0)
                    new = min(inp + cw, Cw)
                    wc = (cost_actual(u) - cr * W_CR - out * W_OUT) * (new / max(inp + cw, 1)) \
                        + max(Cw - new, 0) * W_CR + out * W_OUT
                    if m["in_window"]:
                        base_cost += cost_actual(u)
                        pol_cost += wc * (1 - free)
                else:  # 主会话收结果：缓存可能已过期（子 agent 跑了超过约 1 小时）
                    expired = ts_secs(m["ts"]) - t0 > 3300
                    if m["in_window"]:
                        base_cost += cost_actual(u)
                        pol_cost += Cmain * (W_CW1H if expired else W_CR) + RESULT_TOKENS * W_CW1H + out * W_OUT
                    R = C - (Cmain + RESULT_TOKENS)
                    deleg = False
                continue
            if e[0] == "out":
                tok, chars = e[1], e[2]
                if policy.get("offload") and chars > 2000:
                    keep = tok * SUMMARY_CHARS / chars
                    R += tok - keep
                    tok = keep
                outs.append([tok, False])
                continue
            m = e[1]
            u = m["usage"]
            inp, cw, cr = (u.get("input_tokens") or 0), (u.get("cache_creation_input_tokens") or 0), \
                (u.get("cache_read_input_tokens") or 0)
            out = u.get("output_tokens") or 0
            C = inp + cw + cr
            think = max(out - m["vis"], 0)
            scale = policy.get("think_scale", 1.0)
            out_p = out - think * (1 - scale)
            broke = False
            # 遮蔽旧工具输出：保留最近 N 个
            if policy.get("mask_keep") is not None:
                elig = [o for o in outs[:-policy["mask_keep"]] if not o[1]] if len(outs) > policy["mask_keep"] else []
                if sum(o[0] for o in elig) >= CLEAR_AT_LEAST:
                    for o in elig:
                        R += max(o[0] - 20, 0)
                        o[1] = True
                    broke = True
            # 删旧思考：保留最近 K 次回复的思考
            if policy.get("think_keep") is not None:
                elig = [t for t in thinks[:-policy["think_keep"]] if not t[1]] if len(thinks) > policy["think_keep"] else []
                if sum(t[0] for t in elig) >= CLEAR_AT_LEAST:
                    for t in elig:
                        R += t[0]
                        t[1] = True
                    broke = True
            Cp = max(C - R, 5_000)
            extra = 0.0
            if policy.get("compact_at") and Cp > policy["compact_at"]:
                extra = Cp * W_CR + COMPACT_SUMMARY_OUT * W_OUT  # 压缩请求本身
                R = C - COMPACT_BASE
                Cp = COMPACT_BASE
                outs = [[o[0], True] for o in outs]
                thinks = [[t[0], True] for t in thinks]
                broke = True
                compactions += 1
            if m["in_window"]:
                base_cost += cost_actual(u)
                if broke:
                    pol_cost += Cp * W_CW1H + out_p * W_OUT + extra
                    breaks += 1
                else:
                    new = min(inp + cw, Cp)
                    pol_cost += (cost_actual(u) - cr * W_CR - out * W_OUT) * (new / max(inp + cw, 1)) \
                        + max(Cp - new, 0) * W_CR + out_p * W_OUT
            thinks.append([think * scale, False])
            if scale < 1:  # 思考变少，后面每轮带着的也少
                R += think * (1 - scale)
            if deleg:  # 这句话的第一次请求：主会话写任务说明，然后交给子 agent
                k = 1
                C0, Cmain, t0 = C, Cp + out_p, ts_secs(m["ts"])
    return base_cost, pol_cost, breaks, compactions


POLICIES = [
    ("外包长输出（>2000 字符换成约 1300 字符摘要）", True, {"offload": True}),
    ("上下文超过 200K 就压缩", True, {"compact_at": 200_000}),
    ("上下文超过 300K 就压缩", True, {"compact_at": 300_000}),
    ("上下文超过 400K 就压缩", True, {"compact_at": 400_000}),
    ("思考预算减半", True, {"think_scale": 0.5}),
    ("外包 + 300K 压缩", True, {"offload": True, "compact_at": 300_000}),
    ("外包 + 300K 压缩 + 思考减半", True, {"offload": True, "compact_at": 300_000, "think_scale": 0.5}),
    ("只保留最近 30 个工具输出", False, {"mask_keep": 30}),
    ("只保留最近 10 个工具输出", False, {"mask_keep": 10}),
    ("只保留最近 10 次回复的思考", False, {"think_keep": 10}),
    ("外包 + 保留 30 个输出 + 保留 10 次思考", False, {"offload": True, "mask_keep": 30, "think_keep": 10}),
    ("递归：>15 次请求的活交给子 agent", True, {"delegate_min": 15}),
    ("递归：>30 次请求的活交给子 agent", True, {"delegate_min": 30}),
    ("300K 压缩 + 递归（>15）", True, {"compact_at": 300_000, "delegate_min": 15}),
    ("300K 压缩 + 递归（>30）", True, {"compact_at": 300_000, "delegate_min": 30}),
    ("300K 压缩 + 递归（>15）+ 免费 agent 成功一半", True,
     {"compact_at": 300_000, "delegate_min": 15, "free_success": 0.5}),
    ("外包 + 300K 压缩 + 递归（>15）+ 免费成功一半", True,
     {"offload": True, "compact_at": 300_000, "delegate_min": 15, "free_success": 0.5}),
]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json")
    args = ap.parse_args()
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - args.days * 86400))
    root = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"), "projects")
    sessions = load_sessions(root, since)
    nreq = sum(1 for s in sessions for e in s if e[0] == "req" and e[1]["in_window"])
    actual = sum(cost_actual(e[1]["usage"]) for s in sessions for e in s if e[0] == "req" and e[1]["in_window"])
    print(f"{len(sessions)} 个日志文件，最近 {args.days} 天 {nreq} 次请求，实际等效用量 {actual / 1e6:.1f}M")
    noop = simulate(sessions, {})
    print(f"核对：什么都不改时模拟 = {noop[1] / 1e6:.1f}M（应和实际一致）")
    rows = []
    print(f"\n{'做法':<36}{'Claude Code 能做到':>14}{'模拟后':>10}{'节省':>8}{'缓存重建次数':>12}{'压缩次数':>8}")
    for name, feasible, pol in POLICIES:
        b, p, br, comp = simulate(sessions, pol)
        rows.append({"policy": name, "feasible": feasible, "base": b, "sim": p, "saving": 1 - p / b, "breaks": br,
                     "compactions": comp})
        print(f"{name:<36}{'✅' if feasible else '— 做不到':>14}{p / 1e6:>9.1f}M{1 - p / b:>8.0%}{br:>12}{comp:>8}")
    if args.json:
        json.dump({"requests": nreq, "actual": actual, "rows": rows}, open(args.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
