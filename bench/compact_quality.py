"""压缩之后有没有「变笨」：只读本机 Claude Code 会话记录，不调用模型。

看两种信号，按「刚压缩完」和「其余时候」分开，再按装前 / 装后分开：
1. 用户纠正：用户消息里出现「不对」「你忘了」「我说过」这类话的比例。刚压缩完的那几句明显变高，说明压缩丢了东西
2. 重读：压缩后，把压缩前已经读过的文件又读一遍的比例。这是丢了信息之后的补救，花钱但不一定犯错

用法: python bench/compact_quality.py [--after-n 3] [--days 14] [--samples 12]
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from collections import defaultdict

HOME = os.path.expanduser("~")
NOT_CORRECT = re.compile(r"不错了|出错了|报错了|启动出错|错误")  # 夸奖、程序报错，不是在纠正 AI
CORRECT = re.compile(r"不对|错了|不是这个|不是这样|你忘|忘了|我说过|我刚说|刚才说了|之前说过|说了[^。]{0,6}遍|搞错|理解错|没听懂|看错了|"
                     r"\bwrong\b|\bforgot\b|\bI said\b|\bI already\b|not what I asked", re.I)
AUTO = ("Background (from the session", "<pasted_content", "<task-notification", "<system-reminder", "<side-sessions", "[SYSTEM", "Caveat:", "<ci-monitor", "<local-command",
        "<command-", "This session is being continued")


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return None


def ts_of(d):
    try:
        return time.mktime(time.strptime(d.get("timestamp", "")[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except ValueError:
        return None


def scan(path, after_n):
    """一个会话：每句用户消息（是否刚压缩完）、每次压缩后的重读情况"""
    prompts, compactions = [], []
    since, seen_reads, reads_before, post = None, set(), set(), None
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain"):
            continue
        if d.get("subtype") == "compact_boundary":
            reads_before = set(seen_reads)
            post = {"ts": ts_of(d), "reads": 0, "rereads": 0}
            compactions.append(post)
            since = 0
            continue
        m = d.get("message") or {}
        if d.get("type") == "user" and not d.get("isMeta") and not d.get("isCompactSummary"):
            t = text_of(m.get("content"))
            if t and t.strip() and not t.lstrip().startswith(AUTO):
                prompts.append({"ts": ts_of(d), "text": t.strip(), "post": since is not None and since < after_n})
                if since is not None:
                    since += 1
        if d.get("type") == "assistant":
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Read":
                    fp = os.path.normcase(str((c.get("input") or {}).get("file_path", "")))
                    if post is not None and post["reads"] < 30:  # 压缩后前 30 次读文件
                        post["reads"] += 1
                        post["rereads"] += fp in reads_before
                    seen_reads.add(fp)
    return prompts, compactions


def main():
    after_n, days, samples = int(arg("--after-n", "3")), int(arg("--days", "14")), int(arg("--samples", "12"))
    try:
        installed = json.load(open(os.path.join(HOME, ".infiniteusage", "install.json"))).get("installed_at")
    except OSError:
        installed = None
    if not installed:
        raise SystemExit("找不到装机时间（~/.infiniteusage/install.json）")
    start = installed - days * 86400
    stats = defaultdict(lambda: [0, 0])  # (时期, 刚压缩完?) -> [纠正, 总数]
    rr = defaultdict(lambda: [0, 0, 0])  # 时期 -> [压缩次数, 读文件, 重读]
    flagged, seen = [], set()
    for p in glob.glob(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl")):
        if os.path.getmtime(p) < start:
            continue
        prompts, comps = scan(p, after_n)
        for q in prompts:
            if not q["ts"] or q["ts"] < start:
                continue
            period = "装后" if q["ts"] >= installed else "装前"
            key = (q["text"][:200], round(q["ts"]))
            if key in seen:  # 续聊的会话会复制一份历史，同一句话只算一次
                continue
            seen.add(key)
            if len(q["text"]) > 400:  # 长需求、粘贴进来的内容不算「一句纠正」
                continue
            hit = bool(CORRECT.search(NOT_CORRECT.sub("", q["text"])))
            s = stats[(period, q["post"])]
            s[0] += hit
            s[1] += 1
            if hit:
                flagged.append((period, q["post"], q["text"][:90].replace("\n", " ")))
        for c in comps:
            if not c["ts"] or c["ts"] < start:
                continue
            period = "装后" if c["ts"] >= installed else "装前"
            r = rr[period]
            r[0] += 1
            r[1] += c["reads"]
            r[2] += c["rereads"]
    print(f"范围：装机前 {days} 天到现在；「刚压缩完」= 压缩后的前 {after_n} 句用户消息\n")
    print("用户纠正率（含「不对」「你忘了」「我说过」等）")
    print(f"{'':8}{'刚压缩完':>18}{'其余时候':>18}")
    for period in ("装前", "装后"):
        cells = []
        for post in (True, False):
            h, n = stats[(period, post)]
            cells.append(f"{h}/{n} = {h / n * 100:.1f}%" if n else "—")
        print(f"{period:8}{cells[0]:>18}{cells[1]:>18}")
    print("\n压缩后重读（压缩后前 30 次读文件里，读的是压缩前已经读过的文件）")
    for period in ("装前", "装后"):
        n, reads, re_ = rr[period]
        print(f"{period}：压缩 {n} 次，读文件 {reads} 次，其中重读 {re_} 次" + (f"（{re_ / reads * 100:.0f}%）" if reads else ""))
    print(f"\n被判为「纠正」的消息抽样（最多 {samples} 条，用来人工核对正则有没有误判）")
    for period, post, t in flagged[-samples:]:
        print(f"  [{period}{'·刚压缩完' if post else ''}] {t}")


if __name__ == "__main__":
    main()
