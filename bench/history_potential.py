"""基准 1：从真实日志估算「长工具输出外包给免费模型」能省多少付费额度。只在本机读日志，不外发任何内容。

对每一条工具输出（Bash、Read、Grep、WebFetch……）：
- 长度 L（字符）→ token ≈ L / CHARS_PER_TOKEN
- 它进上下文后，同一个上下文里后面还有 k 次请求（遇到压缩 compact 就停，压缩后旧输出不再被重读）
- 花费（等效 token，权重同 iu/report.py）：Claude = 写缓存 2 + 每次重读 0.1×k；Codex = 输入 1 + 0.1×k
- 如果把 L > THRESHOLD 的输出换成 SUMMARY 字符的摘要，省下 (L - SUMMARY) 部分的花费

用法: python bench/history_potential.py [--days 7] [--json out.json]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import time
from collections import defaultdict

CHARS_PER_TOKEN = 3.2  # 代码和日志的经验值（实测免费模型的分词器约 2.9，英文散文约 4）
THRESHOLD = 2000  # 和 iu sum 一致：短于这个原样给
SUMMARY = 1300  # 实测摘要长度（303 个测试的日志压到 1264 字符）
W = {"claude": (2.0, 0.1), "codex": (1.0, 0.1)}  # (首次进入上下文的权重, 每次重读的权重)


import re

CATS = [
    ("测试/构建", re.compile(r"\b(pytest|unittest|npm (run )?(test|build|lint)|npx (tsc|vitest|jest|eslint)|tsc\b|vitest|jest|"
                         r"cargo (build|test|check)|go (build|test|vet)|gradle|mvn |make\b|dotnet (build|test)|pnpm (test|build)|"
                         r"yarn (test|build)|ruff|mypy|flake8|pip install|npm (ci|install)|brew install)", re.I)),
    ("git", re.compile(r"\bgit (log|diff|show|status|blame)", re.I)),
    ("搜索", re.compile(r"\b(rg |grep |findstr|Select-String|find \S+ -name|Get-ChildItem.*-Recurse|ls -R|tree\b|glob)", re.I)),
    ("读文件内容", re.compile(r"\b(cat |sed -n|head |tail |Get-Content|type |less |nl |awk )", re.I)),
    ("脚本/远程", re.compile(r"(python3? -c|python3? -|@'|<<'?EOF|node -e|ssh )", re.I)),
]


def categorize(tool: str, cmd: str) -> str:
    if tool in ("Read", "NotebookRead"):
        return "读文件内容"
    if tool in ("Grep", "Glob"):
        return "搜索"
    if tool in ("WebFetch", "WebSearch") or tool.startswith("mcp:Claude_Browser") or tool.startswith("mcp:claude-in-chrome"):
        return "网页/浏览器"
    if tool in ("Write", "Edit", "MultiEdit"):
        return "写文件"
    if tool in ("Bash", "PowerShell", "exec", "js", "shell", "exec_command"):
        for name, pat in CATS:
            if pat.search(cmd or ""):
                return name
        return "其他命令"
    return "其他"


def text_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    n = 0
    for c in content or []:
        if isinstance(c, dict):
            if c.get("type") in ("text", "input_text", "output_text"):  # Codex 用 input_text
                n += len(c.get("text") or "")
            elif c.get("type") == "image":
                n += 5000  # 图片按约 1.5K token 粗算，不参与外包
    return n


def claude_events(path, since):
    """按顺序产出 ('req', None) / ('out', (tool, chars)) / ('compact', None)"""
    names, seen = {}, set()
    for line in open(path, encoding="utf-8", errors="replace"):
        if '"tool_use"' not in line and '"tool_result"' not in line and '"compact_boundary"' not in line \
                and '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("timestamp", "") < since:
            continue
        t = d.get("type")
        if t == "system" and d.get("subtype") == "compact_boundary":
            yield "compact", None
        elif t == "assistant":
            msg = d.get("message") or {}
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    names[c.get("id")] = (c.get("name"), str(inp.get("command") or inp.get("cmd") or "")[:2000])
            if msg.get("usage") and msg.get("id") not in seen:
                seen.add(msg.get("id"))
                yield "req", None
        elif t == "user":
            cont = (d.get("message") or {}).get("content")
            if isinstance(cont, list):
                for c in cont:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        name, cmd = names.get(c.get("tool_use_id"), ("?", ""))
                        yield "out", (name, cmd, text_len(c.get("content")))


def codex_events(path, since):
    names, seen = {}, set()
    for line in open(path, encoding="utf-8", errors="replace"):
        if not any(k in line for k in ('"function_call', '"custom_tool_call', '"token_usage_record"', '"compacted"')):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("timestamp", "") < since:
            continue
        t, p = d.get("type"), d.get("payload") or {}
        if t == "compacted":
            yield "compact", None
        elif t == "token_usage_record":
            rid = p.get("response_id")
            if rid not in seen:
                seen.add(rid)
                yield "req", None
        elif t == "response_item":
            pt = p.get("type")
            if pt in ("function_call", "custom_tool_call"):
                names[p.get("call_id")] = (p.get("name"), str(p.get("input") or p.get("arguments") or "")[:4000])
            elif pt in ("function_call_output", "custom_tool_call_output"):
                out = p.get("output")
                if isinstance(out, dict):
                    out = out.get("content") or json.dumps(out)
                name, cmd = names.get(p.get("call_id"), ("?", ""))
                yield "out", (name, cmd, text_len(out) if not isinstance(out, str) else len(out))


def analyze(tool, files, since):
    first_w, reread_w = W[tool]
    by_tool = defaultdict(lambda: defaultdict(float))
    totals = defaultdict(float)
    for path in files:
        evs = list((claude_events if tool == "claude" else codex_events)(path, since))
        # 每条输出之后、下一次压缩之前的请求数
        later, outs = 0, []
        for kind, val in reversed(evs):
            if kind == "compact":
                later = 0
            elif kind == "req":
                later += 1
            else:
                outs.append((val[0], val[1], val[2], later))
        for name, cmd, chars, k in outs:
            name = name or "?"
            if name.startswith("mcp__"):
                name = "mcp:" + name.split("__")[1][:20]
            name = categorize(name, cmd)
            tok = chars / CHARS_PER_TOKEN
            cost = tok * (first_w + reread_w * k)
            b = by_tool[name]
            b["n"] += 1
            b["chars"] += chars
            b["cost"] += cost
            if chars > THRESHOLD and name != "写文件":
                saved_tok = (chars - SUMMARY) / CHARS_PER_TOKEN
                b["big_n"] += 1
                b["big_chars"] += chars
                b["saved"] += saved_tok * (first_w + reread_w * k)
                b["big_k"] += k
            totals["cost"] += cost
    for b in by_tool.values():
        totals["saved"] += b["saved"]
    return by_tool, totals


def main():
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json")
    args = ap.parse_args()
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - args.days * 86400))
    cut = time.time() - (args.days + 1) * 86400
    home = os.path.expanduser("~")
    claude_files = [p for p in glob.glob(os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude"),
                                                      "projects", "**", "*.jsonl"), recursive=True)
                    if os.path.getmtime(p) > cut]
    codex_home = os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")
    codex_files = [p for sub in ("sessions", "archived_sessions")
                   for p in glob.glob(os.path.join(codex_home, sub, "**", "*.jsonl"), recursive=True)
                   if os.path.getmtime(p) > cut]
    result = {}
    for tool, files in (("claude", claude_files), ("codex", codex_files)):
        by_tool, totals = analyze(tool, files, since)
        result[tool] = {"files": len(files), "totals": dict(totals),
                        "by_tool": {k: dict(v) for k, v in by_tool.items()}}
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f)
    for tool, r in result.items():
        tot = r["totals"]
        print(f"\n== {tool}（{r['files']} 个日志文件，最近 {args.days} 天）工具输出总花费 {tot.get('cost', 0) / 1e6:.1f}M 等效，"
              f"外包长输出可省 {tot.get('saved', 0) / 1e6:.1f}M（{tot.get('saved', 0) / max(tot.get('cost', 1), 1):.0%}）")
        rows = sorted(r["by_tool"].items(), key=lambda kv: -kv[1]["cost"])[:12]
        print(f"{'工具':<24}{'次数':>7}{'总字符':>10}{'花费':>9}{'>2K次数':>9}{'>2K字符':>10}{'平均后续请求':>12}{'可省':>9}")
        for name, b in rows:
            avgk = b.get("big_k", 0) / b["big_n"] if b.get("big_n") else 0
            print(f"{name[:23]:<24}{int(b['n']):>7}{b['chars'] / 1e6:>9.1f}M{b['cost'] / 1e6:>8.1f}M{int(b.get('big_n', 0)):>9}"
                  f"{b.get('big_chars', 0) / 1e6:>9.1f}M{avgk:>12.0f}{b.get('saved', 0) / 1e6:>8.1f}M")


if __name__ == "__main__":
    main()
