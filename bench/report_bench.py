"""把 run_bench.py 的结果（jsonl）汇总成 markdown 表格。
用法: python bench/report_bench.py bench/results/quality_<时间>.jsonl [--out bench/results/quality.md]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--out")
    args = ap.parse_args()
    recs = [json.loads(line) for line in open(args.path, encoding="utf-8")]
    items = sorted({r["item"] for r in recs})
    arms = []
    for r in recs:
        if r["arm"] not in arms:
            arms.append(r["arm"])
    # Claude 实际看到的大小（sum 类用 raw_30k，ask 类用 raw_full）当压缩基准
    seen = {}
    for r in recs:
        if r["arm"] == ("raw_30k" if r["kind"] == "sum" else "raw_full"):
            seen[r["item"]] = r["chars"]

    by = defaultdict(list)
    for r in recs:
        by[(r["arm"], r["item"])].append(r)

    L = ["# 外包压缩质量基准", "", f"数据：`{args.path}`，{len(items)} 条语料。", "",
         "## 各组汇总", "",
         "| 组 | 平均命中率 | 最差一次的平均 | 全对的比例 | 编造位置 | 平均字符 | 相对 Claude 实际看到的 | 延迟中位/P95 | 失败 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for arm in arms:
        rs = [r for r in recs if r["arm"] == arm]
        worst = [min(x["recall"] for x in by[(arm, i)]) for i in items if by[(arm, i)]]
        ratio = [r["chars"] / seen[r["item"]] for r in rs if seen.get(r["item"]) and r["chars"]]
        secs = [r.get("seconds", 0) for r in rs if r.get("seconds")]
        fails = sum(1 for r in rs if r.get("error"))
        L.append(f"| {arm} | {statistics.mean(r['recall'] for r in rs):.0%} | {statistics.mean(worst):.0%} | "
                 f"{sum(1 for r in rs if r['recall'] >= 0.999) / len(rs):.0%} | {sum(r['halluc'] for r in rs)} | "
                 f"{statistics.mean(r['chars'] for r in rs):,.0f} | {statistics.mean(ratio) if ratio else 0:.1%} | "
                 f"{(f'{statistics.median(secs):.0f}s / {pct(secs, 0.95):.0f}s') if secs else '-'} | {fails}/{len(rs)} |")

    L += ["", "## 每条语料的命中率（多次重复取平均，括号里是最差一次）", "",
          "| 语料 | 原始字符 | " + " | ".join(arms) + " |", "|---" * (len(arms) + 2) + "|"]
    for i in items:
        raw_chars = next(r["raw_chars"] for r in recs if r["item"] == i)
        cells = []
        for arm in arms:
            rs = by[(arm, i)]
            if not rs:
                cells.append("—")
                continue
            m, w = statistics.mean(r["recall"] for r in rs), min(r["recall"] for r in rs)
            cells.append(f"{m:.0%}" + (f" ({w:.0%})" if len(rs) > 1 and w < m else ""))
        L.append(f"| {i} | {raw_chars:,} | " + " | ".join(cells) + " |")

    L += ["", "## 漏掉的内容（免费模型组）", ""]
    for arm in arms:
        if not arm.startswith("llm:"):
            continue
        miss = defaultdict(int)
        for r in recs:
            if r["arm"] == arm:
                for m in r.get("missing", []):
                    miss[f"{r['item']}: {m}"] += 1
        if miss:
            L.append(f"- **{arm}**：" + "；".join(f"{k} ×{v}" for k, v in sorted(miss.items(), key=lambda kv: -kv[1])[:8]))
    routed = defaultdict(int)
    for r in recs:
        if r.get("routed"):
            routed[(r["arm"], r["routed"])] += 1
    L += ["", "## 实际路由到的模型", ""]
    for (arm, rt), n in sorted(routed.items()):
        L.append(f"- {arm} → {rt} ×{n}")
    text = "\n".join(L) + "\n"
    print(text)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)


if __name__ == "__main__":
    main()
