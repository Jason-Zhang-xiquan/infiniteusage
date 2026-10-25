"""汇总阶段 B v2 冲刺 A/B 的结果，按事先定好的门槛给结论（docs/PLAN_phase_B_v2.md 第 4 节）。
用法: python bench/report_sprint.py bench/results/sprint_full.jsonl [--out docs/phase_B_results.md]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict

GATES = {"C1": ("交接 + 新会话", 0.15), "C2": ("外包 hook v2", 0.10),
         "W": ("提前压缩（100K 窗口）", 0.10), "HAI": ("主线程 Haiku", 0.10), "X": ("探索外包（iu explore）", 0.10),
         "P": ("ponytail", 0.10), "E": ("降推理强度 --effort medium", 0.10), "O": ("调小输出上限", 0.10)}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--out")
    args = ap.parse_args()
    recs = [r for r in (json.loads(line) for line in open(args.path, encoding="utf-8"))
            if not r.get("limited")]  # 撞上用量上限、Claude 没工作的运行不算
    by_arm = defaultdict(list)
    cell = {}
    for r in recs:
        by_arm[r["arm"]].append(r)
        cell[(r["sprint"], r["rep"], r["arm"])] = r
    arms = [a for a in ("C0", "C1", "C2", "P", "E", "O", "HAI", "W", "X", "OPUS", "OPUS_W", "OPUS_E", "OPUS_X") if a in by_arm]

    L = ["# 阶段 B v2 结果：长会话冲刺 A/B", "", f"数据：`{args.path}`，{len(recs)} 次运行（Sonnet，Mac，公开仓库）。", "",
         "## 各组汇总", "",
         "| 组 | 成功 | 花费中位数（API 等价） | 花费合计 | 等效 token 中位数 | 轮数中位数 | 会话数 | 最大上下文中位数 | 耗时中位数 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for a in arms:
        rs = by_arm[a]
        L.append(f"| {a} | {sum(r['success'] for r in rs)}/{len(rs)} | ${statistics.median(r['cost_usd'] for r in rs):.3f} | "
                 f"${sum(r['cost_usd'] for r in rs):.2f} | {statistics.median(r['eq_tokens'] for r in rs):,.0f} | "
                 f"{statistics.median(r['turns'] for r in rs):.0f} | {statistics.median(r['sessions'] for r in rs):.0f} | "
                 f"{statistics.median(r['max_ctx'] for r in rs):,.0f} | {statistics.median(r['wall_s'] for r in rs):.0f}s |")

    L += ["", "## 配对比较（同一冲刺、同一次重复，相对 C0）", "",
          "| 冲刺 | 重复 | 组 | 成功（C0→本组） | 花费 C0 → 本组 | 变化 | 耗时变化 |", "|---|---|---|---|---|---|---|"]
    ratios = defaultdict(list)
    wall_ratios = defaultdict(list)
    succ = defaultdict(lambda: [0, 0])
    for (s, rep, a), r in sorted(cell.items()):
        if a == "C0":
            continue
        base = cell.get((s, rep, "C0"))
        if not base:
            continue
        ch = r["cost_usd"] / base["cost_usd"] - 1 if base["cost_usd"] else 0
        wch = r["wall_s"] / base["wall_s"] - 1 if base["wall_s"] else 0
        ratios[a].append(ch)
        wall_ratios[a].append(wch)
        succ[a][0] += base["success"]
        succ[a][1] += r["success"]
        L.append(f"| {s} | {rep} | {a} | {'✓' if base['success'] else '✗'}→{'✓' if r['success'] else '✗'} | "
                 f"${base['cost_usd']:.3f} → ${r['cost_usd']:.3f} | {ch:+.0%} | {wch:+.0%} |")

    L += ["", "## 按事先定好的门槛下结论", ""]
    for a, (name, need) in GATES.items():
        if not ratios[a]:
            continue
        med = statistics.median(ratios[a])
        wall = statistics.median(wall_ratios[a])
        s0, s1 = succ[a]
        passed = med <= -need and s1 >= s0 - 1 and wall <= 0.30
        L.append(f"- **{a} {name}**：花费中位数变化 {med:+.0%}（门槛 ≤ -{need:.0%}），成功 {s0}→{s1}，"
                 f"耗时中位数变化 {wall:+.0%}（门槛 ≤ +30%）。范围 {min(ratios[a]):+.0%} ~ {max(ratios[a]):+.0%}。"
                 f"**{'通过' if passed else '未通过'}**")
    c2 = by_arm.get("C2", [])
    if c2:
        L += ["", "## C2 外包 hook 的细节", "",
              f"- 改写次数：{sum(r.get('hook_rewrites', 0) for r in c2)}；外包调用 {sum(r.get('offload_calls', 0) for r in c2)} 次，"
              f"其中规则过滤兜底 {sum(r.get('offload_det', 0) for r in c2)} 次",
              f"- 回头读原始输出文件：{sum(r.get('reads_raw', 0) for r in c2)} 次；重复跑同一条命令：{sum(r['repeat_cmds'] for r in c2)} 次"]
    c1 = by_arm.get("C1", [])
    if c1:
        L += ["", "## C1 交接的细节", "", f"- 会话数：{[r['sessions'] for r in c1]}",
              f"- 每次运行的最大上下文：{[r['max_ctx'] for r in c1]}（C0 为 {[r['max_ctx'] for r in by_arm.get('C0', [])]}）"]
    text = "\n".join(L) + "\n"
    print(text)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)


if __name__ == "__main__":
    main()
