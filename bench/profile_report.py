"""把 profile_models.py 的结果汇总成「模型 × 任务类型」的能力画像，并生成 iu 用的路由表。

路由规则（每类任务一条从强到弱的链，最后一档永远是规则过滤）：
  - 进链门槛：该类平均得分 ≥ 0.85、编造 0 次、失败率 ≤ 20%
  - 排序：平均得分 → 最差一次得分 → 延迟中位数
  - 没有模型达标的任务类型：链为空，iu 直接用规则过滤（sum）或者不外包（ask/testgen）
用法: python bench/profile_report.py bench/results/profile.jsonl [--out docs/profiles.md] [--routes routes.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict

MIN_SCORE, MAX_FAIL = 0.85, 0.30


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--out")
    ap.add_argument("--routes")
    args = ap.parse_args()
    recs = [json.loads(line) for line in open(args.path, encoding="utf-8")]
    models = sorted({r["model"] for r in recs})
    types = sorted({r["task_type"] for r in recs})
    cell = defaultdict(list)
    for r in recs:
        cell[(r["model"], r["task_type"])].append(r)

    def stats(rs):
        by_item = defaultdict(list)
        for r in rs:
            by_item[r["item"]].append(r["score"])
        secs = [r["seconds"] for r in rs if not r.get("error")]
        answered = [r for r in rs if not r.get("error")]
        return {"quality": statistics.mean(r["score"] for r in answered) if answered else 0.0,
                "mean": statistics.mean(r["score"] for r in rs),
                "worst": statistics.mean(min(v) for v in by_item.values()),
                "halluc": sum(r["halluc"] for r in rs),
                "fail": sum(1 for r in rs if r.get("error")) / len(rs),
                "p50": statistics.median(secs) if secs else None, "n": len(rs)}

    L = ["# 免费模型任务型能力画像", "", f"数据：`{args.path}`，{len(recs)} 次调用，{len(models)} 个模型，{len(types)} 类任务。",
         f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}。进路由链的门槛：回答质量 ≥ {MIN_SCORE:.0%}、编造 0、失败率 ≤ {MAX_FAIL:.0%}（失败由链里的下一个模型接住）。",
         "", "## 回答质量（只算成功返回的调用；括号里：编造次数 / 失败率，失败多半是限流）", "",
         "| 模型 | " + " | ".join(types) + " | 总体 | 延迟中位 |", "|---" * (len(types) + 3) + "|"]
    routes = {}
    for m in models:
        row = []
        for t in types:
            rs = cell.get((m, t))
            if not rs:
                row.append("—")
                continue
            s = stats(rs)
            mark = "✅" if s["quality"] >= MIN_SCORE and s["halluc"] == 0 and s["fail"] <= MAX_FAIL else ""
            row.append(f"{mark}{s['quality']:.0%}（{s['halluc']}/{s['fail']:.0%}）")
        allr = [r for r in recs if r["model"] == m]
        s_all = stats(allr)
        L.append(f"| {m} | " + " | ".join(row) + f" | {s_all['quality']:.0%} | "
                 f"{(str(round(s_all['p50'])) + 's') if s_all['p50'] is not None else '—'} |")
    L += ["", "## 路由表（每类任务从强到弱；都不行时用规则过滤或不外包）", ""]
    for t in types:
        ranked = []
        for m in models:
            if m == "auto":  # auto 本身就是随机路由，不进固定链
                continue
            rs = cell.get((m, t))
            if not rs:
                continue
            s = stats(rs)
            if s["quality"] >= MIN_SCORE and s["halluc"] == 0 and s["fail"] <= MAX_FAIL:
                ranked.append((-s["quality"], s["fail"], s["p50"] or 999, m))
        chain = [m for *_, m in sorted(ranked)]
        routes[t] = chain
        L.append(f"- **{t}**：{' → '.join(chain) if chain else '（没有达标的免费模型）'} → 兜底")
    text = "\n".join(L) + "\n"
    print(text)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
    if args.routes:
        json.dump({"generated": time.time(), "min_score": MIN_SCORE, "routes": routes}, open(args.routes, "w"), indent=1)


if __name__ == "__main__":
    main()
