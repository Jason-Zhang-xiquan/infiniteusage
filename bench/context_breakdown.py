"""基准 1b：Claude Code 主会话的上下文里到底装了什么（只读本机日志，不外发）。

做法：
1. 按时间顺序把会话拆成「内容块」：用户消息、附件（system reminder 等）、助手正文、助手思考、
   助手的工具调用参数、工具输出（按类别）。遇到 compact 边界就清零
2. 每次请求的实测上下文 = input + cache_creation + cache_read（来自 usage）
3. 用相邻两次请求之间新增的内容，对实测增量做最小二乘，拟合「每个字符多少 token」：
   ASCII、非 ASCII（中文）、思考分开拟合。思考的系数接近 0 说明旧思考没有被重发
4. 用拟合的系数把每个内容块换成 token，乘以它在后面被带上的请求数，得到各类内容占的「token×请求」份额；
   实测总量减去这些，剩下的是固定前缀（系统提示、工具定义、skill 列表等）和误差

用法: python bench/context_breakdown.py [--days 7]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from history_potential import categorize  # noqa: E402


def split_chars(s: str):
    a = sum(1 for ch in s if ord(ch) < 128)
    return a, len(s) - a


def block_text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(block_text(x) for x in c)
    if isinstance(c, dict):
        if c.get("type") == "image":
            return "\x00" * 4000  # 图片：约 1.5K token，当非 ASCII 计
        if "text" in c:
            return c.get("text") or ""
        if "content" in c:
            return block_text(c.get("content"))
    return ""


def session_pieces(path, since):
    """产出 ('req', measured) / ('compact',) / ('piece', category, ascii, nonascii, is_thinking)"""
    names, seen = {}, set()
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain"):
            continue
        t = d.get("type")
        if t == "system" and d.get("subtype") == "compact_boundary":
            yield ("compact",)
        elif t == "attachment":
            a = d.get("attachment") or {}
            txt = a.get("text") or a.get("content") or ""
            if not isinstance(txt, str):
                txt = json.dumps(txt, ensure_ascii=False)
            if txt:
                yield ("piece", "附件/提醒", *split_chars(txt), False)
        elif t == "user":
            cont = (d.get("message") or {}).get("content")
            if isinstance(cont, str):
                yield ("piece", "压缩摘要" if d.get("isCompactSummary") else "用户消息", *split_chars(cont), False)
            elif isinstance(cont, list):
                for c in cont:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_result":
                        name, cmd = names.get(c.get("tool_use_id"), ("?", ""))
                        if name.startswith("mcp__"):
                            name = "mcp:" + name.split("__")[1][:20]
                        yield ("piece", "工具输出:" + categorize(name, cmd), *split_chars(block_text(c.get("content"))), False)
                    else:
                        yield ("piece", "用户消息", *split_chars(block_text(c)), False)
        elif t == "assistant":
            msg = d.get("message") or {}
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "tool_use":
                    inp = c.get("input") or {}
                    names[c.get("id")] = (c.get("name") or "?", str(inp.get("command") or inp.get("cmd") or ""))
                    yield ("piece", "助手工具调用参数", *split_chars(json.dumps(inp, ensure_ascii=False)), False)
                elif ct == "thinking":
                    yield ("piece", "助手思考", *split_chars(c.get("thinking") or ""), True)
                elif ct == "text":
                    yield ("piece", "助手正文", *split_chars(c.get("text") or ""), False)
            u = msg.get("usage")
            if u and msg.get("id") not in seen and msg.get("model") != "<synthetic>":
                seen.add(msg.get("id"))
                measured = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + \
                           (u.get("cache_read_input_tokens") or 0)
                yield ("req", measured, d.get("timestamp", "") >= since)


def solve(rows):
    """最小二乘 y = x·b（3 个特征，无截距），手写正规方程避免依赖 numpy"""
    n = 3
    A = [[0.0] * n for _ in range(n)]
    v = [0.0] * n
    for x, y in rows:
        for i in range(n):
            v[i] += x[i] * y
            for j in range(n):
                A[i][j] += x[i] * x[j]
    # 高斯消元
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(A[r][i]))
        A[i], A[p], v[i], v[p] = A[p], A[i], v[p], v[i]
        if abs(A[i][i]) < 1e-9:
            continue
        for r in range(n):
            if r != i:
                f = A[r][i] / A[i][i]
                for c in range(n):
                    A[r][c] -= f * A[i][c]
                v[r] -= f * v[i]
    return [v[i] / A[i][i] if abs(A[i][i]) > 1e-9 else 0.0 for i in range(n)]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - args.days * 86400))
    cut = time.time() - (args.days + 1) * 86400
    root = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"), "projects")
    files = [p for p in glob.glob(os.path.join(root, "*", "*.jsonl")) if os.path.getmtime(p) > cut]  # 只要主会话，不含 subagents/

    sessions = [list(session_pieces(p, since)) for p in files]
    # 1) 拟合每字符 token 数：相邻请求之间新增内容 vs 实测增量
    rows = []
    for evs in sessions:
        prev, add = None, [0, 0, 0]
        for e in evs:
            if e[0] == "compact":
                prev, add = None, [0, 0, 0]
            elif e[0] == "piece":
                _, cat, a, na, th = e
                if th:
                    add[2] += a + na
                else:
                    add[0] += a
                    add[1] += na
            else:
                if e[2] and prev is not None and e[1] >= prev and e[1] - prev < 200_000 and any(add):
                    rows.append((list(add), e[1] - prev))
                prev, add = e[1], [0, 0, 0]
    k_ascii, k_non, k_think = solve(rows)
    print(f"拟合（{len(rows)} 个相邻请求增量）：ASCII 每字符 {k_ascii:.3f} token（{1 / k_ascii if k_ascii else 0:.1f} 字符/token），"
          f"非 ASCII 每字符 {k_non:.3f} token，思考每字符 {k_think:.3f} token")

    # 2) 各类内容的 token×请求
    share = defaultdict(float)
    measured_total, nreq = 0.0, 0
    for evs in sessions:
        live = defaultdict(float)  # 当前上下文里每类内容的 token 数
        for e in evs:
            if e[0] == "compact":
                live = defaultdict(float)
            elif e[0] == "piece":
                _, cat, a, na, th = e
                live[cat] += (a + na) * max(k_think, 0) if th else a * k_ascii + na * k_non
            elif e[2]:
                measured_total += e[1]
                nreq += 1
                for cat, tok in live.items():
                    share[cat] += tok
    modeled = sum(share.values())
    print(f"\n{len(files)} 个主会话，{nreq} 次请求，实测上下文合计 {measured_total / 1e9:.2f}B token·次，"
          f"平均每次 {measured_total / max(nreq, 1) / 1e3:.0f}K")
    print(f"{'内容':<20}{'占比':>8}{'平均每次请求带着':>16}")
    for cat, v in sorted(share.items(), key=lambda kv: -kv[1]):
        print(f"{cat:<20}{v / measured_total:>8.1%}{v / max(nreq, 1) / 1e3:>14.1f}K")
    rest = measured_total - modeled
    print(f"{'固定前缀+误差':<20}{rest / measured_total:>8.1%}{rest / max(nreq, 1) / 1e3:>14.1f}K")


if __name__ == "__main__":
    main()
