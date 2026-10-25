"""确定性过滤（不用模型）：免费模型失败、限流、太慢时的兜底，也是基准测试里的对照组。

按优先级装行，装满上限为止，只按整行装：
  1. 结论和错误行（FAIL、Error、Traceback、TS 错误码、npm ERR!、测试汇总……），错误行再带后面 2 行
  2. 目标关键词所在行（goal 里像文件名、标识符的词）
  3. 最后 20 行
基准 2（docs/benchmark_2026-10-18.md）：平均命中 88%，编造 0，体积约为 Claude 原本看到的 21%。
"""

from __future__ import annotations

import re

STRONG = re.compile(r"(\bFAIL\b|✗|×|\bError\b|Error:|AssertionError|TypeError|\bfailed\b|Tests\s+\d|Test Files|"
                    r"\bTS\d{4}\b|Traceback|ERR!|E404|\b404\b|exit code|built in|\bwarn(ing)?\b|panic)")
CONTEXT_AFTER = re.compile(r"(\bFAIL\b|Error|Traceback|ERR!|panic|AssertionError)")
STOP = {"what", "which", "where", "when", "give", "with", "and", "the", "that", "this", "their", "these", "from",
        "line", "lines", "number", "numbers", "declare", "declared", "short", "hashes", "subjects", "provider",
        "source", "files", "file", "not", "tests", "documented", "anonymous", "rate", "limit", "platform", "use",
        "uses", "does", "touched", "commits", "commit", "is", "its", "on", "of", "id", "run", "runs", "check"}


def keywords(text: str) -> list:
    words = re.findall(r"[A-Za-z_][\w./\-]{2,}", text or "")
    return [w for w in words if w.lower() not in STOP]


def det_filter(raw: str, goal: str = "", numbered: bool = False, cap: int = 6000) -> str:
    lines = raw.splitlines()
    keys = [k.lower() for k in keywords(goal)]
    tiers = [set(), set(), set(range(max(0, len(lines) - 20), len(lines)))]
    for i, line in enumerate(lines):
        if STRONG.search(line):
            tiers[0].add(i)
            if CONTEXT_AFTER.search(line):
                tiers[0].update(range(i + 1, min(i + 3, len(lines))))
        elif keys and any(k in line.lower() for k in keys):
            tiers[1].update(range(i, min(i + 2, len(lines))))
    keep, used = set(), 0
    for tier in tiers:
        for i in sorted(tier - keep):
            cost = len(lines[i]) + 8
            if used + cost > cap:
                break
            keep.add(i)
            used += cost
    return "\n".join((f"{i + 1}: " if numbered else "") + lines[i] for i in sorted(keep))
