"""汇总 bench/codex_sprint.py 的结果：每组的等效 token、请求数、最大上下文、压缩次数、成功率。

问题答案按会话记录里写进 ANSWERS.md 的内容，用 ab_sprint.py 当前的关键字重新判（第一次跑时关键字只认英文，
Mac 的全局 AGENTS.md 要求中文，答案写成「每 12 小时」被误判）。
用法（Mac）: python3 bench/report_codex_sprint.py bench/results/codex_sprint_ab.jsonl
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ab_sprint as ab  # noqa: E402


def answers_text(rollout: str) -> str:
    text = ""
    try:
        for line in open(rollout, encoding="utf-8", errors="replace"):
            if "ANSWERS.md" in line and ("Add File" in line or "Update File" in line):
                p = json.loads(line).get("payload") or {}
                text += str(p.get("input") or p.get("arguments") or "")
    except (OSError, TypeError):
        pass
    return text


def main():
    rows = [json.loads(ln) for ln in open(sys.argv[1], encoding="utf-8") if ln.strip()]
    by = defaultdict(list)
    for r in rows:
        low = answers_text(r.get("rollout")).lower()
        if low:
            r["answers_ok"] = [any(a.lower() in low for a in g) for g in ab.SPRINTS[r["sprint"]]["answers"]]
        r["success"] = r["tests_pass"] and not r["tests_touched"] and all(r["answers_ok"])
        by[r["arm"]].append(r)
        print(f"{r['sprint']} r{r['rep']} {r['arm']:<5} success={r['success']!s:<5} tests={r['tests_pass']!s:<5} "
              f"answers={r['answers_ok']} eq={r['eq']:>8,} req={r['requests']:>3} max_ctx={r['max_ctx']:>7,} "
              f"comp={r['compactions']} wall={r['wall_s']}s")
    base = statistics.mean(r["eq"] for r in by.get("D", [])) if by.get("D") else None
    print(f"\n{'组':<6}{'次数':>4}{'成功':>6}{'测试全过':>8}{'平均等效':>10}{'相对 D':>8}{'平均请求':>8}{'最大上下文':>10}{'压缩':>6}")
    for arm, rs in by.items():
        m = statistics.mean(r["eq"] for r in rs)
        print(f"{arm:<6}{len(rs):>4}{sum(r['success'] for r in rs):>6}{sum(r['tests_pass'] for r in rs):>8}{m:>10,.0f}"
              f"{(m / base - 1) if base else 0:>+8.0%}{statistics.mean(r['requests'] for r in rs):>8.1f}"
              f"{max(r['max_ctx'] for r in rs):>10,}{sum(r['compactions'] for r in rs):>6}")


if __name__ == "__main__":
    main()
