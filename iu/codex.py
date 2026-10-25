"""Codex：读 ~/.codex/sessions 和 archived_sessions 下的 rollout-*.jsonl。

- 新版日志每次回复有一条 token_usage_record（带 response_id），直接累加、按 response_id 去重
- 旧版只有 token_count 事件：用 total_token_usage 的差值（同一会话里单调递增）
- token_count 事件里带官方额度 rate_limits（已用百分比、窗口分钟数、resets_at），取时间最新的一条
OpenAI 的 input_tokens 包含缓存命中部分，output_tokens 包含推理 token。
"""

from __future__ import annotations

import glob
import json
import os

from .store import Store, hour_of, read_new_lines

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def log_files(home: str = CODEX_HOME) -> list:
    out = []
    for sub in ("sessions", "archived_sessions"):
        out += glob.glob(os.path.join(home, sub, "**", "*.jsonl"), recursive=True)
    return sorted(out)


WANTED = (b'"token_usage_record"', b'"token_count"', b'"turn_context"', b'"session_meta"')


def _buckets(u: dict) -> dict:
    inp = u.get("input_tokens") or 0
    cached = u.get("cached_input_tokens") or 0
    return {"n": 1, "in": max(inp - cached, 0), "cr": cached, "out": u.get("output_tokens") or 0,
            "think": u.get("reasoning_output_tokens") or 0}


def scan(store: Store, host: str, home: str = CODEX_HOME) -> int:
    paths = log_files(home)
    if store.needs_rebuild(paths):
        store.reset()
    added = 0
    limits = store.extra.get("codex_limits") or {}
    for path in paths:
        st = store.files.setdefault(path, {"offset": 0, "mode": None, "model": "", "effort": "", "cwd": "",
                                           "session": "", "side": "main", "last_total": None})
        lines, st["offset"] = read_new_lines(path, st["offset"])
        pending_totals = []  # 模式未定时，先暂存差值算法的结果
        for line in lines:
            if not any(w in line for w in WANTED):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t, p, ts = d.get("type"), d.get("payload") or {}, d.get("timestamp", "")
            if t == "session_meta":
                st["session"] = p.get("id") or p.get("session_id") or st["session"]
                st["cwd"] = p.get("cwd") or st["cwd"]
                src = p.get("thread_source")
                st["side"] = "sub" if src and src != "user" else "main"
            elif t == "turn_context":
                st["model"] = p.get("model") or st["model"]
                st["effort"] = p.get("effort") or (p.get("collaboration_mode") or {}).get("settings", {}).get(
                    "reasoning_effort") or st["effort"]
                st["cwd"] = p.get("cwd") or st["cwd"]
            dims = {"hour": hour_of(ts), "tool": "codex", "model": st["model"], "effort": st["effort"],
                    "project": os.path.basename((st["cwd"] or "").rstrip("\\/")), "session": st["session"],
                    "side": st["side"], "host": host}
            if t == "token_usage_record":
                if st["mode"] is None:
                    st["mode"] = "records"
                    pending_totals = []
                rid = p.get("response_id") or f"{p.get('turn_id')}|{ts}"
                if st["mode"] == "records" and store.first_time("codex|" + rid):
                    store.add(dims, _buckets(p.get("usage") or {}))
                    added += 1
            elif t == "event_msg" and p.get("type") == "token_count":
                rl = p.get("rate_limits")
                if rl and ts > limits.get("ts", "") and (rl.get("primary") or rl.get("secondary")):
                    limits = {"ts": ts, "host": host, "rate_limits": rl}
                info = p.get("info") or {}
                total = info.get("total_token_usage")
                if not total or st["mode"] == "records":
                    continue
                prev = st["last_total"]
                st["last_total"] = total
                if prev and total.get("total_tokens", 0) < prev.get("total_tokens", 0):
                    prev = None  # 计数被重置
                delta = {k: total.get(k, 0) - (prev or {}).get(k, 0) for k in
                         ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")}
                if not any(delta.values()):
                    continue  # 同一个总数重复报告
                key = f"codex-total|{st['session']}|{total.get('total_tokens')}"
                pending_totals.append((key, dims, _buckets(delta)))
        if pending_totals and st["mode"] != "records":
            st["mode"] = "totals"
            for key, dims, b in pending_totals:
                if store.first_time(key):
                    store.add(dims, b)
                    added += 1
    store.extra["codex_limits"] = limits
    return added
