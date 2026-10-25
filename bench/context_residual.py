"""基准 1c：那 37%「没解释清楚的上下文」到底是什么？只读本机日志。

模型 A（context_breakdown.py 的做法）：助手内容按日志里的文字估 token（思考只有摘要）
模型 B：助手内容改用账单上的真实 output_tokens（包含完整思考），其他内容照旧按字符估
比较两种模型下「实测上下文 - 估算」的残差。B 的残差明显变小 → 旧的思考内容被一直带在上下文里。
还会按会话里的位置分段看残差：随轮数增长 = 有内容在累积；不随轮数变 = 固定前缀。
用法: python bench/context_residual.py [--days 7]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import time

K_ASCII, K_NON = 0.40, 1.53  # context_breakdown.py 拟合出的每字符 token 数


def est(s: str) -> float:
    a = sum(1 for ch in s if ord(ch) < 128)
    return a * K_ASCII + (len(s) - a) * K_NON


def text_of(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(text_of(x) for x in c)
    if isinstance(c, dict):
        if c.get("type") == "image":
            return "\x00" * 1000  # 约 1.5K token
        return text_of(c.get("text") or c.get("content") or "")
    return ""


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - args.days * 86400))
    root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    files = [p for p in glob.glob(os.path.join(root, "*", "*.jsonl")) if os.path.getmtime(p) > time.time() - 8 * 86400]
    resid_a, resid_b, meas_all = [], [], []
    by_pos = {"1-10": [], "11-50": [], "51-200": [], "200+": []}
    for path in files:
        user_side, asst_text, asst_billed = 0.0, 0.0, 0.0
        seen, pos = set(), 0
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("isSidechain"):
                continue
            t = d.get("type")
            if t == "system" and d.get("subtype") == "compact_boundary":
                user_side = asst_text = asst_billed = 0.0
                pos = 0
            elif t == "attachment":
                a = d.get("attachment") or {}
                txt = a.get("text") or a.get("content") or ""
                user_side += est(txt if isinstance(txt, str) else json.dumps(txt, ensure_ascii=False))
            elif t == "user":
                user_side += est(text_of((d.get("message") or {}).get("content")))
            elif t == "assistant":
                msg = d.get("message") or {}
                mid, u = msg.get("id"), msg.get("usage") or {}
                if mid in seen:
                    continue
                if not u or msg.get("model") == "<synthetic>":
                    continue
                seen.add(mid)
                measured = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + \
                           (u.get("cache_read_input_tokens") or 0)
                pos += 1
                if d.get("timestamp", "") >= since:
                    ra = measured - (user_side + asst_text)
                    rb = measured - (user_side + asst_billed)
                    resid_a.append(ra)
                    resid_b.append(rb)
                    meas_all.append(measured)
                    key = "1-10" if pos <= 10 else "11-50" if pos <= 50 else "51-200" if pos <= 200 else "200+"
                    by_pos[key].append((measured, ra, rb))
                # 这条回复之后进入上下文
                txt = "".join((c.get("text") or c.get("thinking") or json.dumps(c.get("input") or {}, ensure_ascii=False))
                              for c in msg.get("content") or [] if isinstance(c, dict))
                asst_text += est(txt)
                asst_billed += u.get("output_tokens") or 0
    tot = sum(meas_all)
    print(f"{len(files)} 个主会话，{len(meas_all)} 次请求，实测上下文合计 {tot / 1e9:.2f}B token·次")
    print(f"模型 A（助手内容按日志文字估）：残差占 {sum(resid_a) / tot:.1%}，每次请求残差中位数 {statistics.median(resid_a) / 1e3:.0f}K")
    print(f"模型 B（助手内容用账单 output_tokens）：残差占 {sum(resid_b) / tot:.1%}，每次请求残差中位数 {statistics.median(resid_b) / 1e3:.0f}K")
    print("\n按会话里的位置：")
    print(f"{'第几次请求':<10}{'次数':>8}{'实测上下文中位':>14}{'A 残差中位':>12}{'B 残差中位':>12}")
    for k, rows in by_pos.items():
        if rows:
            print(f"{k:<10}{len(rows):>8}{statistics.median(r[0] for r in rows) / 1e3:>13.0f}K"
                  f"{statistics.median(r[1] for r in rows) / 1e3:>11.0f}K{statistics.median(r[2] for r in rows) / 1e3:>11.0f}K")


if __name__ == "__main__":
    main()
