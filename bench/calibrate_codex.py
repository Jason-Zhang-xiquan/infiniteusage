"""A3：用 Codex 日志里官方的「已用 %」反推每种 token 占多少额度。只读本机日志。

1. 所有会话合成一条时间线（额度按账号算，多个会话同时跑也计入同一个 %）
2. 每次官方已用 % 上升一格，把上一格以来的 token 用量加总，得到一个样本：(Δ%, 未缓存输入, 缓存读, 输出[按模型])
3. 非负最小二乘拟合，得到「每百万 token 占多少 % 额度」；按时间留出最后 20% 样本检验预测误差
注意：云端任务、其他设备上的用量不在本机日志里，会表现为拟合不了的残差。已用 % 是整数，所以只用累计变化。

用法: python bench/calibrate_codex.py [--days 14]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

HOME = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def family(model: str) -> str:
    m = (model or "").lower()
    for k in ("sol", "astra", "luna"):
        if k in m:
            return k
    return "other"


def load_events(days):
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - days * 86400))
    files = [p for sub in ("sessions", "archived_sessions")
             for p in glob.glob(os.path.join(HOME, sub, "**", "*.jsonl"), recursive=True)
             if os.path.getmtime(p) > time.time() - (days + 1) * 86400]
    events, seen = [], set()
    for path in files:
        model = ""
        for line in open(path, "rb"):
            if not (b'"token_usage_record"' in line or b'"rate_limits"' in line or b'"turn_context"' in line):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts, t, p = d.get("timestamp", ""), d.get("type"), d.get("payload") or {}
            if t == "turn_context":
                model = p.get("model") or model
                continue
            if ts < since:
                continue
            if t == "token_usage_record":
                rid = p.get("response_id")
                if rid in seen:
                    continue
                seen.add(rid)
                u = p.get("usage") or {}
                inp, cached = u.get("input_tokens") or 0, u.get("cached_input_tokens") or 0
                events.append((ts, "use", family(model), max(inp - cached, 0), cached, u.get("output_tokens") or 0))
            elif t == "event_msg" and p.get("type") == "token_count":
                prim = (p.get("rate_limits") or {}).get("primary") or {}
                if prim.get("used_percent") is not None and prim.get("resets_at"):
                    events.append((ts, "pct", round(prim["resets_at"] / 3600), float(prim["used_percent"]),
                                   prim.get("window_minutes")))
    events.sort(key=lambda e: e[0])
    return events, len(files)


FEATURES = ["in", "cached", "out_sol", "out_astra", "out_other"]


def segments(events):
    acc = {f: 0.0 for f in FEATURES}
    window, pct, rows = None, None, []
    for e in events:
        if e[1] == "use":
            fam = e[2] if e[2] in ("sol", "astra") else "other"
            acc["in"] += e[3]
            acc["cached"] += e[4]
            acc["out_" + fam] += e[5]
        else:
            w, p = e[2], e[3]
            if w != window:  # 新的计费窗口：清零重来
                window, pct, acc = w, p, {f: 0.0 for f in FEATURES}
                continue
            if p > pct:  # 只认超过本窗口历史最高值的部分：多个会话的快照新旧不一，数字会来回跳
                rows.append((e[0], p - pct, dict(acc)))
                pct, acc = p, {f: 0.0 for f in FEATURES}
    return rows


def nnls(X, y, iters=20000):
    """非负最小二乘，坐标下降。特征先按尺度归一化"""
    n = len(X[0])
    scale = [max(max(abs(r[j]) for r in X), 1e-12) for j in range(n)]
    Z = [[r[j] / scale[j] for j in range(n)] for r in X]
    b = [0.0] * n
    col_sq = [sum(z[j] ** 2 for z in Z) for j in range(n)]
    resid = list(y)
    for _ in range(iters):
        delta = 0.0
        for j in range(n):
            if col_sq[j] == 0:
                continue
            g = sum(Z[i][j] * resid[i] for i in range(len(Z)))
            new = max(0.0, b[j] + g / col_sq[j])
            d = new - b[j]
            if d:
                for i in range(len(Z)):
                    resid[i] -= d * Z[i][j]
                b[j] = new
                delta = max(delta, abs(d))
        if delta < 1e-10:
            break
    return [b[j] / scale[j] for j in range(n)]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    events, nfiles = load_events(args.days)
    rows = segments(events)
    print(f"{nfiles} 个日志文件，{sum(1 for e in events if e[1] == 'use')} 次请求，{len(rows)} 个「已用 % 上升」的样本，"
          f"合计 +{sum(r[1] for r in rows):.0f}%")
    if len(rows) < 10:
        print("样本太少，没法拟合")
        return
    used = [f for f in FEATURES if any(r[2][f] for r in rows)]
    X = [[r[2][f] / 1e6 for f in used] for r in rows]
    y = [r[1] for r in rows]
    cut = int(len(rows) * 0.8)
    b_train = nnls(X[:cut], y[:cut])
    pred = [sum(bj * xj for bj, xj in zip(b_train, x)) for x in X[cut:]]
    actual = sum(y[cut:])
    print(f"\n留出检验（最后 {len(rows) - cut} 个样本）：实际 +{actual:.0f}%，预测 +{sum(pred):.1f}%，"
          f"误差 {abs(sum(pred) - actual) / max(actual, 1e-9):.0%}")
    b = nnls(X, y)
    tot = {f: sum(r[2][f] for r in rows) for f in used}
    contrib = {f: b[i] * tot[f] / 1e6 for i, f in enumerate(used)}
    print("\n全部样本拟合：每百万 token 占多少 % 周额度，以及这段时间各自贡献了多少 %")
    print(f"{'token 类型':<12}{'百万 token':>12}{'%/百万':>10}{'贡献':>9}{'占比':>8}")
    total_c = sum(contrib.values()) or 1
    for i, f in enumerate(used):
        print(f"{f:<12}{tot[f] / 1e6:>12.1f}{b[i]:>10.4f}{contrib[f]:>8.1f}%{contrib[f] / total_c:>8.0%}")
    print(f"拟合合计 {total_c:.1f}% vs 实际 {sum(y):.0f}%")
    rel = {f: b[i] / b[used.index('in')] for i, f in enumerate(used)} if 'in' in used and b[used.index('in')] else {}
    if rel:
        print("相对「未缓存输入 = 1」的权重：" + "、".join(f"{f} {v:.2f}" for f, v in rel.items()))


if __name__ == "__main__":
    main()
