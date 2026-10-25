"""iu compare：装之前 vs 装之后的对比账本（Claude Code）。

用量会随工作量起伏，所以不比总量，比「每次请求」：
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

from . import claude
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
    n = 0
    for p in glob.glob(os.path.join(claude.ROOT, "**", "*.jsonl"), recursive=True):
        if os.path.getmtime(p) < t0:
            continue
        for line in open(p, encoding="utf-8", errors="replace"):
            if '"compact_boundary"' in line:
                try:
                    ts = json.loads(line).get("timestamp", "")
                except ValueError:
                    continue
                n += lo <= ts <= hi
    return n


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
    print(t("\n注意：装前装后做的活不一样，这是观察数据，不是对照实验；严格的对照见 EVIDENCE.md", "\nNote: the work before and after differs; this is observational, not a controlled experiment. See EVIDENCE.md for controlled tests"))
