"""iu compare：装之前 vs 装之后的对比账本（Claude Code）。

先按会话算压缩的账（iu/compactsim.py audit）：装之后每一次提前压缩，不压缩的话后面那些请求要多带多少上下文，
减去压缩本身的花费。装前装后干的活不一样，按天汇总的对比会被会话长短带偏，所以这一项是主要结论。

再给按天汇总的对比做参考。用量会随工作量起伏，所以不比总量，比「每次请求」：
  - 每次请求的等效 token（权重：新输入 1、缓存写 2/1.25、缓存读 0.1、输出 5）
  - 每次请求平均带多少上下文（压缩的直接效果）
  - 每次请求的输出 token（看思考有没有变化）
  - 压缩次数、外包次数和外包挡在上下文外面的字符
比较区间：装之前 N 天（默认 7）vs 装之后到现在。只读本机日志。
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import socket
import time

from . import claude, compactsim
from .i18n import t
from .install import STATE
from .offload import LOG as OFFLOAD_LOG
from .store import DATA_DIR, Store, iter_rows

W = {"in": 1.0, "cw5": 1.25, "cw1h": 2.0, "cr": 0.1, "out": 5.0}


def window_stats(rows, t0: float, t1: float) -> dict:
    lo, hi = time.strftime("%Y-%m-%dT%H", time.gmtime(t0)), time.strftime("%Y-%m-%dT%H", time.gmtime(t1))
    acc = {k: 0.0 for k in ("n", "in", "cw5", "cw1h", "cr", "out")}
    for d, b in rows:
        if d["tool"] == "claude" and lo <= d["hour"] <= hi:
            for k in acc:
                acc[k] += b.get(k, 0)
    n = acc["n"] or 1
    eq = sum(acc[k] * w for k, w in W.items())
    return {"requests": int(acc["n"]), "eq": eq, "eq_per_req": eq / n,
            "ctx_per_req": (acc["in"] + acc["cw5"] + acc["cw1h"] + acc["cr"]) / n, "out_per_req": acc["out"] / n}


def compactions(t0: float, t1: float) -> int:
    lo = dt.datetime.fromtimestamp(t0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    hi = dt.datetime.fromtimestamp(t1, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    seen = set()  # 接着旧会话继续时旧记录会抄进新文件：同一次压缩只算一次
    for p in glob.glob(os.path.join(claude.ROOT, "**", "*.jsonl"), recursive=True):
        if os.path.getmtime(p) < t0:
            continue
        for line in open(p, encoding="utf-8", errors="replace"):
            if '"compact_boundary"' in line:
                try:
                    ts = json.loads(line).get("timestamp", "")
                except ValueError:
                    continue
                if lo <= ts <= hi:
                    seen.add(ts)
    return len(seen)


def offload_stats(t0: float, t1: float) -> dict:
    calls = det = 0
    kept_out = 0
    try:
        for line in open(OFFLOAD_LOG, encoding="utf-8"):
            r = json.loads(line)
            if t0 <= r.get("ts", 0) <= t1 and r.get("kind") in ("sum", "sum-det"):
                calls += 1
                det += r["kind"] == "sum-det"
                kept_out += max((r.get("raw_chars") or 0) - 1300, 0)
    except OSError:
        pass
    return {"calls": calls, "det": det, "chars_kept_out": kept_out}


def cmd_compare(args):
    try:
        state = json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit(t("还没装（没有 ~/.infiniteusage/install.json）。先运行 iu install", "Not installed yet (no ~/.infiniteusage/install.json). Run iu install first"))
    t_install = state["installed_at"]
    now = time.time()
    st = Store("claude")
    claude.scan(st, socket.gethostname())
    st.save()
    rows = list(iter_rows(st.rows))
    before = window_stats(rows, t_install - args.days * 86400, t_install)
    after = window_stats(rows, t_install, now)
    cb, ca = compactions(t_install - args.days * 86400, t_install), compactions(t_install, now)
    off = offload_stats(t_install, now)
    hours = (now - t_install) / 3600
    when = time.strftime('%Y-%m-%d %H:%M', time.localtime(t_install))
    print(t(f"InfiniteUsage 装前 / 装后对比（Claude Code，本机）· 装于 {when}，已过 {hours:.1f} 小时",
            f"InfiniteUsage before / after (Claude Code, this machine) · installed {when}, {hours:.1f} h ago"))
    print(t(f"设置：约 {state.get('compact_at', 0) // 1000}K 压缩；外包长输出 {'开' if state.get('offload') else '关'}\n",
            f"Settings: compact at ~{state.get('compact_at', 0) // 1000}K; long-output offload {'on' if state.get('offload') else 'off'}\n"))
    compact_at = state.get("compact_at", 0)
    if compact_at:
        print_audit(compactsim.audit(compactsim.load_sessions(t_install), t_install, now, compact_at), compact_at)
    print(t("按天汇总的对比（参考：装前装后做的活不一样，会话长短一变数字就跟着变，不能直接算成工具的效果）",
            "Daily totals (for reference only: the work before and after differs, so these move with session length, "
            "not just with the tool)"))
    print(f"{'':<22}{t('装前 ' + str(args.days) + ' 天', 'before ' + str(args.days) + 'd'):>14}{t('装后', 'after'):>14}{t('变化', 'change'):>9}")

    def line(name, a, b, fmt):
        ch = (b / a - 1) if a else 0
        print(f"{name:<22}{fmt(a):>14}{fmt(b):>14}{ch:>+9.0%}")

    print(f"{t('请求数', 'requests'):<22}{before['requests']:>14,}{after['requests']:>14,}")
    line(t("每次请求等效 token", "eq. tokens / request"), before["eq_per_req"], after["eq_per_req"], lambda v: f"{v / 1e3:.1f}K")
    line(t("每次请求平均上下文", "context / request"), before["ctx_per_req"], after["ctx_per_req"], lambda v: f"{v / 1e3:.0f}K")
    line(t("每次请求输出 token", "output tokens / request"), before["out_per_req"], after["out_per_req"], lambda v: f"{v:.0f}")
    print(f"{t('压缩次数', 'compactions'):<22}{cb:>14}{ca:>14}")
    print(f"{t('外包长输出', 'offloaded outputs'):<22}{'—':>14}{off['calls']:>14}   "
          + t(f"其中规则过滤兜底 {off['det']} 次，约 {off['chars_kept_out'] / 1e3:.0f}K 字符没进上下文",
              f"{off['det']} by rule filter; ~{off['chars_kept_out'] / 1e3:.0f}K chars kept out of context"))
    if after["requests"] < 200:
        print(t("\n装后请求还太少（< 200），数字波动会很大，过几天再看", "\nToo few requests since install (< 200); numbers are noisy, check again in a few days"))
    print(t("\n注意：按天汇总的数字是观察数据，不是对照实验；压缩的效果看上面按会话算的账，严格的对照见 EVIDENCE.md",
            "\nNote: the daily totals are observational, not a controlled experiment; for compaction see the per-session "
            "ledger above, and EVIDENCE.md for controlled tests"))


def project_of(x: dict) -> str:
    name = os.path.basename(x.get("cwd", "").rstrip("/\\")) or os.path.basename(os.path.dirname(x["path"]))
    return name if len(name) <= 22 else name[:21] + "…"


def local_time(ts: str) -> str:
    try:
        sec = dt.datetime.fromisoformat(ts[:19]).replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return ts[5:16]
    return time.strftime("%m-%d %H:%M", time.localtime(sec))


def print_audit(a: dict, compact_at: int):
    """按会话算的压缩账：每次压缩一行，最后汇总"""
    print(t(f"提前压缩按会话算账（约 {compact_at // 1000}K 压缩；单位：等效 token）",
            f"Early compaction, per session (compacts at ~{compact_at // 1000}K; unit: equivalent tokens)"))
    items = a["items"]
    if items:
        print(f"  {t('时间', 'time'):<13}{t('项目', 'project'):<24}{t('压缩前', 'before'):>9}{t('之后请求', 'reqs after'):>12}"
              f"{t('压缩花', 'cost'):>10}{t('之后省', 'saved'):>10}{t('净省', 'net'):>10}")
        show = items[-15:]
        if len(show) < len(items):
            print(t(f"  …（前面还有 {len(items) - len(show)} 次，只列最近 15 次）", f"  … ({len(items) - len(show)} earlier, showing the last 15)"))
        for x in show:
            print(f"  {local_time(x['ts']):<13}{project_of(x):<24}{x['pre'] / 1e3:>8.0f}K{x['after_requests']:>12}"
                  f"{x['spent'] / 1e3:>9.0f}K{x['saved'] / 1e3:>9.0f}K{x['net'] / 1e3:>+9.0f}K")
        losing = sum(1 for x in items if x["net"] < 0)
        base = a["total"] + a["net"]  # 不提前压缩的话，这段时间大约要花多少
        pct = a["net"] / base if base else 0
        print(t(f"  合计 {len(items)} 次：压缩本身花 {a['spent'] / 1e6:.2f}M，之后少花 {a['saved'] / 1e6:.2f}M，"
                f"净省 {a['net'] / 1e6:+.2f}M（装后全部会话实际 {a['total'] / 1e6:.1f}M，不提前压缩约 {base / 1e6:.1f}M，省 {pct:.0%}）",
                f"  {len(items)} compactions: they cost {a['spent'] / 1e6:.2f}M, then saved {a['saved'] / 1e6:.2f}M, "
                f"net {a['net'] / 1e6:+.2f}M (all sessions since install {a['total'] / 1e6:.1f}M, ~{base / 1e6:.1f}M without "
                f"early compaction, {pct:.0%} saved)"))
        if losing:
            print(t(f"  其中 {losing} 次亏了：压缩完会话很快就结束了，压缩本身的花费没赚回来",
                    f"  {losing} of them lost: the session ended soon after compacting, so the compaction did not pay off"))
    else:
        print(t("  装之后还没有发生过提前压缩", "  No early compaction since install"))
    print(t(f"  装后 {a['sessions']} 个会话里，{a['short_sessions']} 个没长到压缩点（压缩设置对它们没有影响）",
            f"  Of {a['sessions']} sessions since install, {a['short_sessions']} never reached the compaction point "
            f"(the setting did not affect them)"))
    if a["other_compactions"]:
        print(t(f"  另有 {a['other_compactions']} 次压缩不是提前压缩触发的（手动 /compact，或模型窗口小时 Claude Code 自己压缩），没算进来",
                f"  {a['other_compactions']} other compactions (manual /compact, or Claude Code's own on small-window "
                f"models) are not counted"))
    print(t("  估算方法：不压缩的话，压掉的那部分上下文之后每次请求都要再读一遍（缓存读 0.1；缓存过期按写算）；"
            "压缩那次请求不写进日志，按读一遍旧上下文 + 写摘要估；到约 950K 时不改设置也会自己压缩，之后不再算省\n",
            "  Method: without compaction, the removed context is re-read on every later request (cache read 0.1; "
            "cache write if expired). The compaction request itself is not logged; estimated as reading the old context + "
            "writing the summary. Savings stop at ~950K, where Claude Code would compact on its own anyway\n"))
