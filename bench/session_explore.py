"""代码地图（MAP.md）能省多少的上限：只读本机 Claude Code 会话记录，不调用模型、不花额度。

问题：同一项目开新会话后，从开始到第一次改文件之前，agent 花多少在「找代码、读代码」上？
其中有多少读的是同一项目上一个会话已经读过的文件（有地图就可以不读）？

口径
- 只看主会话（跳过 subagents/ 和 isSidechain）。「新会话」= 文件里第一条消息不是续写的（parentUuid 为空、
  sessionId 等于文件名、不是 "This session is being continued"）。续聊/压缩后续写只进分母
- 一次请求 = 一个 assistant message.id；等效 token：输入 1、缓存写 2（1h）/1.25（5m）、缓存读 0.1、输出 5
- 探索期 = 第一次改文件之前：Edit/Write/MultiEdit/NotebookEdit，或者会写文件的 Bash（sed -i、> 文件、tee、cp/mv、python 写文件…）
- 探索请求 = 探索期内、带工具调用、并且工具全是探索类的请求：Read/Grep/Glob/LS，或每一段都是只读命令的 Bash/PowerShell
  （ls、find、cat、head、grep、sed -n、git log/show/diff…）。update_plan、TodoWrite、ToolSearch 这类记账工具不影响判断
- 探索的花费 = 这次请求本身（直接）+ 它带进上下文的工具结果在之后每次请求里被重复读（携带：写缓存 2 + 之后每次 0.1，
  到压缩或会话结束为止；结果 token 按字符估）
- 可省上限两档：
  严格：探索请求里，Read 的文件全是「同项目上一个会话读过的」且本会话不会改它，Grep/Glob/只读 Bash 算「定位」也可省
        —— 但只在有上一个会话时算（地图来自之前的工作）
  宽松：所有探索请求，去掉读「本会话后来要改的文件」的那部分（改之前总得读）

用法: python bench/session_explore.py [--days 30] [--show 4] [--json out.json]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import shlex
import re
from collections import defaultdict

HOME = os.path.expanduser("~")
W_IN, W_CW1H, W_CW5M, W_CR, W_OUT = 1.0, 2.0, 1.25, 0.1, 5.0
K_ASCII, K_NON = 0.40, 1.53  # 每字符 token 数，和 simulate.py 一致

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
EXPLORE_TOOLS = {"Read", "Grep", "Glob", "LS"}
NEUTRAL = {"mcp__ccd_turn__update_plan", "mcp__ccd_turn__wrap_up", "TodoWrite", "ToolSearch", "Skill",
           "mcp__ccd_session__mark_chapter"}
SHELLS = {"Bash", "PowerShell"}
READONLY_CMD = {"ls", "dir", "find", "cat", "type", "head", "tail", "grep", "rg", "egrep", "wc", "tree", "pwd", "file",
                "stat", "du", "which", "where", "echo", "awk", "sort", "uniq", "cut", "less", "more", "realpath",
                "basename", "dirname", "nl", "column", "xxd", "od", "fd", "ag", "jq", "date", "tr",
                "get-childitem", "gci", "get-content", "gc", "select-string", "sls", "test-path", "get-item",
                "resolve-path", "measure-object", "select-object", "format-table", "format-list", "sort-object",
                "where-object", "out-string", "split-path", "get-location", "cd", "set-location"}
GIT_RO = {"status", "log", "show", "diff", "ls-files", "grep", "blame", "branch", "rev-parse", "remote", "describe",
          "shortlog", "ls-tree", "cat-file", "tag"}
WRITE_SHELL = re.compile(r"\bsed\s+-i|\btee\b|\bcp\s|\bmv\s|\brm\s|\bmkdir\b|\btouch\b|\bpatch\b|\bgit\s+(apply|checkout|"
                         r"commit|add|mv|rm|restore|stash|reset)|Set-Content|Add-Content|Out-File|New-Item|Copy-Item|"
                         r"Move-Item|Remove-Item|open\([^)]*['\"][wa]['\"]|write_text|\.write\(", re.I)
W_FIRST_EXT = defaultdict(float)  # 全程「第一次读、不改」按后缀分的花费
REDIRECT = re.compile(r"(?<![0-9&])>>?\s*(?!&|/dev/null|\$null|NUL\b)[\"']?[\w.~/$\\:-]")


def cost(u):
    cc = u.get("cache_creation") or {}
    cw = u.get("cache_creation_input_tokens") or 0
    cw1h = cc.get("ephemeral_1h_input_tokens", cw) if cc else cw
    return ((u.get("input_tokens") or 0) * W_IN + cw1h * W_CW1H + (cw - cw1h) * W_CW5M
            + (u.get("cache_read_input_tokens") or 0) * W_CR + (u.get("output_tokens") or 0) * W_OUT)


def est_tokens(s):
    a = sum(1 for ch in s if ord(ch) < 128)
    return a * K_ASCII + (len(s) - a) * K_NON


def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(text_of(x) for x in c)
    if isinstance(c, dict):
        return "" if c.get("type") == "image" else text_of(c.get("text") or c.get("content") or "")
    return ""


def norm(p):
    return os.path.normcase(os.path.normpath(str(p).replace("/", os.sep))) if p else ""


def segments(cmd):
    """按 ; | && || 换行切成一段段命令（引号里的不切），每段拆成词"""
    segs, cur, q, i = [], "", None, 0
    while i < len(cmd):
        ch = cmd[i]
        if q:
            if ch == "\\" and q == '"' and i + 1 < len(cmd):
                cur += cmd[i:i + 2]
                i += 2
                continue
            q = None if ch == q else q
            cur += ch
        elif ch in "'\"":
            q = ch
            cur += ch
        elif ch in ";|\n&":
            if ch == "&" and cmd[i + 1:i + 2] != "&" and cmd[i - 1:i] != "&":
                cur += ch  # 2>&1 之类
            else:
                segs.append(cur)
                cur = ""
        else:
            cur += ch
        i += 1
    segs.append(cur)
    out = []
    for seg in segs:
        try:
            w = shlex.split(seg, posix=True)
        except ValueError:
            w = seg.split()
        if w:
            out.append(w)
    return out


def shell_kind(cmd):
    """'write' / 'read'（全部是只读命令）/ 'other'"""
    if WRITE_SHELL.search(cmd) or REDIRECT.search(re.sub(r"<<'?(\w+)'?.*?^\1$", "", cmd, flags=re.S | re.M)):
        return "write"
    if "<<" in cmd:  # heredoc 喂给脚本：多半是在跑东西
        return "other"
    for w in segments(cmd):
        head = w[0].lower().lstrip("(")
        if head in ("sed",) and "-n" in w:
            continue
        if head == "git" and len(w) > 1 and (w[1] in GIT_RO or w[1].startswith("-")):
            continue
        if head in READONLY_CMD or re.match(r"^\$?\w+=", head) or head.startswith("#"):
            continue
        return "other"
    return "read"


def classify(name, inp):
    """'edit' / 'explore' / 'neutral' / 'other'"""
    if name in EDIT_TOOLS:
        return "edit"
    if name in EXPLORE_TOOLS:
        return "explore"
    if name in NEUTRAL:
        return "neutral"
    if name in SHELLS:
        k = shell_kind((inp or {}).get("command", ""))
        return {"write": "edit", "read": "explore"}.get(k, "other")
    return "other"


READ_CMD = {"cat", "head", "tail", "sed", "nl", "less", "more", "type", "get-content", "gc"}


def to_path(p, cwd):
    p = p.strip("\"'")
    p = re.sub(r"^/([a-zA-Z])/", r"\1:/", p)
    if p.startswith("~"):
        p = HOME + p[1:]
    if not os.path.isabs(p) and cwd:
        p = os.path.join(cwd, p)
    return norm(p)


def targets(name, inp, cwd):
    """这次工具调用读了哪些文件（Read 的 file_path；Bash 里 cat/head/sed -n 等的文件参数）"""
    if name == "Read":
        return [norm(inp.get("file_path"))]
    if name not in SHELLS:
        return []
    out = []
    for w in segments(inp.get("command", "")):
        head = w[0].lower()
        if head in ("cd", "set-location") and len(w) > 1:
            cwd = to_path(w[1], cwd)
            continue
        if head not in READ_CMD:
            continue
        args = [x for x in w[1:] if not x.startswith("-") and not x.isdigit() and not x.startswith(("2>", ">"))]
        if head == "sed" and args:
            args = args[1:]  # 第一个是 sed 脚本
        out += [to_path(x, cwd) for x in args if re.search(r"[./\\]", x) and "*" not in x]
    return out


def parse(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    reqs, by_id, results, first, ncomp = [], {}, {}, None, 0
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain"):
            continue
        t = d.get("type")
        if t == "system" and d.get("subtype") == "compact_boundary":
            reqs.append({"compact": True})
            ncomp += 1
            continue
        if t not in ("user", "assistant"):
            continue
        m = d.get("message") or {}
        if first is None:
            c = m.get("content")
            first = {"parent": d.get("parentUuid"), "sid": d.get("sessionId"), "ts": d.get("timestamp", ""),
                     "text": text_of(c)[:200] if t == "user" else ""}
        if t == "user":
            c = m.get("content")
            if isinstance(c, list):
                for x in c:
                    if isinstance(x, dict) and x.get("type") == "tool_result":
                        results[x.get("tool_use_id")] = est_tokens(text_of(x.get("content")))
            continue
        mid, u = m.get("id"), m.get("usage")
        if not mid or not u or m.get("model") == "<synthetic>":
            continue
        r = by_id.get(mid)
        if r is None:
            r = by_id[mid] = {"mid": mid, "usage": dict(u), "tools": [], "ts": d.get("timestamp", ""),
                              "cwd": d.get("cwd", "")}
            reqs.append(r)
        elif (u.get("output_tokens") or 0) > (r["usage"].get("output_tokens") or 0):
            r["usage"] = dict(u)  # 同一条回复分多行写，取输出最多的那行
        for c in m.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                r["tools"].append((c.get("id"), c.get("name"), c.get("input") or {}))
    new = bool(first) and not first["parent"] and first["sid"] == stem \
        and not first["text"].startswith("This session is being continued")
    return {"path": path, "stem": stem, "project": os.path.basename(os.path.dirname(path)), "new": new,
            "ts": first["ts"] if first else "", "reqs": reqs, "results": results, "ncomp": ncomp}


def analyze(s, prev_reads, seen_mids):
    """给一个会话算：总花费、探索期、探索请求的直接/携带花费、可省上限"""
    reqs = s["reqs"]
    # 之后还有几次请求（到下一次压缩为止），用来算携带花费
    after, n = [0] * len(reqs), 0
    for i in range(len(reqs) - 1, -1, -1):
        if reqs[i].get("compact"):
            n = 0
            continue
        after[i] = n
        n += 1
    total = 0.0
    for r in reqs:
        if not r.get("compact") and r["mid"] not in seen_mids:
            seen_mids.add(r["mid"])
            total += cost(r["usage"])
    out = {"total": total, "nreq": sum(1 for r in reqs if not r.get("compact")), "explore_direct": 0.0,
           "explore_carry": 0.0, "strict": 0.0, "loose": 0.0, "phase_reqs": 0, "explore_reqs": 0,
           "has_edit": False, "phase_cost": 0.0, "first_edit_req": None, "whole": 0.0, "read_stats": []}
    edited = set()
    for r in reqs:
        for _, name, inp in r.get("tools", []):
            if name in EDIT_TOOLS:
                edited.add(norm(inp.get("file_path") or inp.get("notebook_path")))
    out["reads"] = {fp for r in reqs for _, nm, inp in r.get("tools", []) for fp in targets(nm, inp, r.get("cwd"))}
    if not s["new"]:
        return out
    in_phase = True
    seen_here = set()
    for i, r in enumerate(reqs):
        if r.get("compact"):
            in_phase = False  # 压缩了还没改文件：后面不算探索期
            continue
        kinds = [classify(nm, inp) for _, nm, inp in r["tools"]]
        real = [(tid, nm, inp) for (tid, nm, inp), k in zip(r["tools"], kinds) if k != "neutral"]
        explore = bool(real) and all(k in ("explore", "neutral") for k in kinds)
        c = cost(r["usage"])
        carry = sum(s["results"].get(tid, 0.0) for tid, _, _ in real) * (W_CW1H + W_CR * after[i]) if explore else 0
        if explore:
            out["whole"] += c + carry  # 全程（不只是第一次改文件之前）只读探索请求的总花费：任何地图的绝对上限
            for tid, nm, inp in real:  # 全程拆开：定位 / 读之后要改的文件 / 读同会话已读过的 / 第一次读
                share = c / len(real) + s["results"].get(tid, 0.0) * (W_CW1H + W_CR * after[i])
                ts = targets(nm, inp, r.get("cwd"))
                key = ("w_locate" if not ts else "w_edit" if any(fp in edited for fp in ts) else
                       "w_reread" if all(fp in seen_here for fp in ts) else "w_first")
                out[key] = out.get(key, 0.0) + share
                if key == "w_first":
                    for fp in ts:
                        ext = os.path.splitext(fp)[1] or "(无后缀)"
                        W_FIRST_EXT[ext] += share / len(ts)
                seen_here.update(ts)
        if not explore:
            seen_here.update(fp for _, nm, inp in real for fp in targets(nm, inp, r.get("cwd")))
        if "edit" in kinds and in_phase:
            out["has_edit"] = True
            out["first_edit_req"] = i
            in_phase = False
        if not in_phase:
            continue
        out["phase_reqs"] += 1
        out["phase_cost"] += c
        if not explore:
            continue
        out["explore_reqs"] += 1
        strict_frac = loose_frac = 0.0
        for tid, nm, inp in real:
            ts = targets(nm, inp, r.get("cwd"))
            if ts:  # 读文件
                for fp in ts:
                    out["read_stats"].append((fp in prev_reads, fp in edited, fp))
                ok_loose = not any(fp in edited for fp in ts)
                strict_frac += (ok_loose and all(fp in prev_reads for fp in ts)) / len(real)
                loose_frac += ok_loose / len(real)
            else:  # 定位（ls、grep、find、Glob）：有地图就能省，但严格口径要求有上一个会话
                strict_frac += bool(prev_reads) / len(real)
                loose_frac += 1 / len(real)
        out["explore_direct"] += c
        out["explore_carry"] += carry
        out["strict"] += (c + carry) * strict_frac
        out["loose"] += (c + carry) * loose_frac
    return out


def pct(a, b):
    return f"{a / b * 100:5.1f}%" if b else "   —"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=0, help="只看最近几天（0 = 全部）")
    ap.add_argument("--show", type=int, default=4, help="抽查几个新会话的探索期明细")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--json")
    a = ap.parse_args()
    import time
    cutoff = time.time() - a.days * 86400 if a.days else 0
    files = [p for p in glob.glob(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl"))
             if os.path.getmtime(p) >= cutoff]
    sessions = [parse(p) for p in files]
    sessions = [s for s in sessions if any(not r.get("compact") for r in s["reqs"])]
    sessions.sort(key=lambda s: s["ts"])
    by_proj = defaultdict(list)
    for s in sessions:
        by_proj[s["project"]].append(s)
    seen_mids = set()
    rows, agg = [], defaultdict(lambda: defaultdict(float))
    for proj, ss in by_proj.items():
        prev_reads, any_reads = set(), set()
        for s in ss:
            res = analyze(s, prev_reads, seen_mids)
            res["any_prev"] = [x[2] in any_reads for x in res["read_stats"]]
            s["res"] = res
            rows.append(s)
            for key in ("total",):
                agg[proj]["all_total"] += res["total"]
                agg["__ALL__"]["all_total"] += res["total"]
            if s["new"]:
                for g in (proj, "__ALL__", "__EDIT__" if res["has_edit"] else "__NOEDIT__"):
                    A = agg[g]
                    A["n"] += 1
                    A["new_total"] += res["total"]
                    for k in ("explore_direct", "explore_carry", "strict", "loose", "phase_cost", "explore_reqs", "whole",
                              "w_locate", "w_edit", "w_reread", "w_first",
                              "phase_reqs", "nreq"):
                        A[k] += res.get(k, 0.0)
                    for in_prev, will_edit, _ in res["read_stats"]:
                        A["reads"] += 1
                        A["reads_prev"] += in_prev
                        A["reads_edit"] += will_edit
                    A["reads_any"] += sum(res["any_prev"])
                    A["with_prev"] += bool(prev_reads)
            prev_reads = res["reads"]  # 上一个会话读过的 = 它全程读过的文件（不只是探索期）
            any_reads |= prev_reads

    print(f"会话文件 {len(sessions)} 个，新会话 {int(agg['__ALL__']['n'])} 个，项目 {len(by_proj)} 个"
          f"（{'最近 %g 天' % a.days if a.days else '全部日志'}）\n")
    hdr = (f"{'项目':42}{'新会话':>6}{'有上个':>6}{'新会话花费':>12}{'占全部':>8}{'探索期':>8}{'探索直接':>9}"
           f"{'+携带':>8}{'全程探索':>9}{'严格可省':>9}{'宽松可省':>9}{'读文件':>7}{'上个读过':>9}{'更早读过':>9}{'后来改':>8}")
    print("每个百分比的分母 = 这些新会话的全部等效 token（「占全部」的分母是该项目所有会话）")
    print(hdr)

    def line(name, A):
        nt = A["new_total"]
        print(f"{name[:42]:42}{int(A['n']):>6}{int(A['with_prev']):>6}{nt / 1e6:>10.2f}M{pct(nt, A['all_total']):>8}"
              f"{pct(A['phase_cost'], nt):>8}{pct(A['explore_direct'], nt):>9}"
              f"{pct(A['explore_direct'] + A['explore_carry'], nt):>8}{pct(A['whole'], nt):>9}{pct(A['strict'], nt):>9}{pct(A['loose'], nt):>9}"
              f"{int(A['reads']):>7}{pct(A['reads_prev'], A['reads']):>9}{pct(A['reads_any'], A['reads']):>9}"
              f"{pct(A['reads_edit'], A['reads']):>8}")

    for proj in sorted(by_proj, key=lambda p: -agg[p]["new_total"]):
        if agg[proj]["n"]:
            line(proj.replace("D--AllPersonalProjects-", "").replace("C--Users-jason-", "~"), agg[proj])
    print("-" * 150)
    agg["__EDIT__"]["all_total"] = agg["__EDIT__"]["new_total"]
    agg["__NOEDIT__"]["all_total"] = agg["__NOEDIT__"]["new_total"]
    line("总体", agg["__ALL__"])
    line("  其中：有改文件的新会话", agg["__EDIT__"])
    line("  其中：没改文件的新会话", agg["__NOEDIT__"])
    A = agg["__ALL__"]
    print(f"\n占所有会话（含续写）的等效 token：探索直接+携带 {pct(A['explore_direct'] + A['explore_carry'], A['all_total'])}，"
          f"严格可省 {pct(A['strict'], A['all_total'])}，宽松可省 {pct(A['loose'], A['all_total'])}")
    print(f"全程探索 {pct(A['whole'], A['new_total'])} 拆开：定位（ls/grep/find/Glob）{pct(A['w_locate'], A['new_total'])}，"
          f"读本会话后来要改的文件 {pct(A['w_edit'], A['new_total'])}，重读本会话已读过的 {pct(A['w_reread'], A['new_total'])}，"
          f"第一次读、不改 {pct(A['w_first'], A['new_total'])}")
    tot = sum(W_FIRST_EXT.values()) or 1
    print("  「第一次读、不改」按后缀：" + "，".join(f"{e} {v / tot * 100:.0f}%" for e, v in
                                          sorted(W_FIRST_EXT.items(), key=lambda x: -x[1])[:12]))
    print(f"新会话平均请求 {A['nreq'] / max(A['n'], 1):.0f} 次，探索期 {A['phase_reqs'] / max(A['n'], 1):.1f} 次，"
          f"其中探索请求 {A['explore_reqs'] / max(A['n'], 1):.1f} 次")

    news = [s for s in rows if s["new"] and s["res"]["explore_reqs"]]
    random.Random(a.seed).shuffle(news)
    for s in news[:a.show]:
        r = s["res"]
        print(f"\n抽查 {s['project'][-30:]}/{s['stem'][:8]}  总 {r['total'] / 1e3:.0f}K，探索期 {r['phase_reqs']} 次请求，"
              f"探索直接 {r['explore_direct'] / 1e3:.0f}K + 携带 {r['explore_carry'] / 1e3:.0f}K，"
              f"严格 {r['strict'] / 1e3:.0f}K，宽松 {r['loose'] / 1e3:.0f}K，第一次改在第 {r['first_edit_req']} 次请求")
        for i, q in enumerate(s["reqs"][:(r["first_edit_req"] or r["phase_reqs"]) + 1]):
            if q.get("compact"):
                break
            tools = "; ".join(f"{nm}:{classify(nm, inp)[0]}:" + str(inp.get("file_path") or inp.get("command")
                                                                     or inp.get("pattern") or "")[:60].replace("\n", " ")
                              for _, nm, inp in q["tools"]) or "(无工具)"
            print(f"  {i:3} {cost(q['usage']) / 1e3:6.1f}K  {tools[:150]}")
    if a.json:
        json.dump({k: dict(v) for k, v in agg.items()}, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
