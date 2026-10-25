"""官方额度快照。

- Claude：日志里没有额度百分比，只有 statusline 的 stdin JSON 里有 rate_limits.five_hour / seven_day。
  `iu statusline` 当 statusline 命令用：每次刷新把快照存到 ~/.infiniteusage/claude_limits.json，
  百分比变化时追加到 claude_limits_history.jsonl（以后用来算「1% 额度 ≈ 多少 token」）。
- Codex：日志里的 token_count 事件自带 rate_limits，codex.scan 时顺手取最新的。
"""

from __future__ import annotations

import json
import os
import socket
import time

from .store import DATA_DIR
from .i18n import t

CLAUDE_LATEST = os.path.join(DATA_DIR, "claude_limits.json")
CLAUDE_HISTORY = os.path.join(DATA_DIR, "claude_limits_history.jsonl")


def fmt_left(seconds: float) -> str:
    if seconds <= 0:
        return t("已重置", "reset")
    m = int(seconds // 60)
    d, h, mm = m // 1440, (m % 1440) // 60, m % 60
    if d:
        return t(f"{d}天{h}时", f"{d}d{h}h")
    if h:
        return t(f"{h}时{mm:02d}分", f"{h}h{mm:02d}m")
    return t(f"{mm}分", f"{mm}m")


def record_statusline(payload: dict) -> dict:
    """把 statusline 输入里的额度和缓存信息存下来，返回存下的快照"""
    rl = payload.get("rate_limits") or {}
    snap = {"ts": time.time(), "host": socket.gethostname(), "session_id": payload.get("session_id"),
            "model": (payload.get("model") or {}).get("id"), "rate_limits": rl}
    if not rl:
        return snap
    os.makedirs(DATA_DIR, exist_ok=True)
    prev = load_claude()
    tmp = CLAUDE_LATEST + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f)
    os.replace(tmp, CLAUDE_LATEST)

    def pcts(s):
        r = (s or {}).get("rate_limits") or {}
        return tuple((r.get(w) or {}).get("used_percentage") for w in ("five_hour", "seven_day"))

    if pcts(prev) != pcts(snap):
        with open(CLAUDE_HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap) + "\n")
    return snap


def load_claude() -> dict:
    try:
        with open(CLAUDE_LATEST, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def claude_windows(snap: dict, now: float = None) -> list:
    """[(名称, 已用%, resets_at)]，已经过了 resets_at 的窗口视为已重置"""
    now = now or time.time()
    out = []
    rl = snap.get("rate_limits") or {}
    for key, label in (("five_hour", t("5小时", "5h")), ("seven_day", t("7天", "7d")), ("spend_limit", t("花费上限", "spend limit"))):
        w = rl.get(key)
        if w and w.get("used_percentage") is not None:
            reset = w.get("resets_at") or 0
            out.append((label, 0.0 if reset and reset <= now else float(w["used_percentage"]), reset))
    return out


def codex_windows(limits: dict, now: float = None) -> list:
    now = now or time.time()
    out = []
    rl = (limits or {}).get("rate_limits") or {}
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not w:
            continue
        minutes = w.get("window_minutes") or 0
        label = {300: t("5小时", "5h"), 10080: t("7天", "7d")}.get(minutes, t(f"{minutes}分钟", f"{minutes}min"))
        reset = w.get("resets_at") or 0
        used = w.get("used_percent", w.get("used_percentage")) or 0  # Codex 日志里叫 used_percent
        out.append((label, 0.0 if reset and reset <= now else float(used), reset))
    return out


def statusline_text(payload: dict, now: float = None) -> str:
    now = now or time.time()
    parts = [(payload.get("model") or {}).get("display_name") or ""]
    for label, pct, reset in claude_windows({"rate_limits": payload.get("rate_limits") or {}}, now):
        parts.append(f"{label} {pct:.0f}% ↻{fmt_left(reset - now)}" if reset else f"{label} {pct:.0f}%")
    ctx = (payload.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(t(f"上下文 {ctx:.0f}%", f"context {ctx:.0f}%"))
    pc = payload.get("prompt_cache") or {}
    if pc.get("hit_ratio") is not None:
        cold = "" if pc.get("warm", True) else t("（已冷）", " (cold)")
        parts.append(t(f"缓存命中 {pc['hit_ratio'] * 100:.0f}%{cold}", f"cache hit {pc['hit_ratio'] * 100:.0f}%{cold}"))
    return " · ".join(p for p in parts if p)
