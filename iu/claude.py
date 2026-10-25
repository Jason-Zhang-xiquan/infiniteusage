"""Claude Code：读 ~/.claude/projects/**/*.jsonl 里每条 assistant 回复的 usage。

同一条回复会被写多次（流式分块、续接会话、分叉会话），按 message.id + requestId 去重，和 ccusage 一样。
官方额度百分比不在日志里，只在 statusline 的输入里有，见 limits.py。
"""

from __future__ import annotations

import glob
import json
import os

from .store import Store, hour_of, read_new_lines

ROOT = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"), "projects")


def log_files(root: str = ROOT) -> list:
    return sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))


def project_name(cwd: str, path: str) -> str:
    if cwd:
        return os.path.basename(cwd.rstrip("\\/")) or cwd
    # 没有 cwd 时用 projects 下的目录名（形如 D--AllPersonalProjects-InfiniteUsage）
    rel = os.path.relpath(path, ROOT).replace("\\", "/").split("/")[0]
    return rel.split("-")[-1] or rel


def scan(store: Store, host: str, root: str = ROOT) -> int:
    paths = log_files(root)
    if store.needs_rebuild(paths):
        store.reset()
    added = 0
    for path in paths:
        st = store.files.setdefault(path, {"offset": 0})
        lines, st["offset"] = read_new_lines(path, st["offset"])
        is_sub_file = "subagents" in path.replace("\\", "/").split("/")
        for line in lines:
            if b'"usage"' not in line or b'"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            u = msg.get("usage")
            model = msg.get("model") or ""
            if not u or model == "<synthetic>":
                continue
            if not store.first_time(f"{msg.get('id')}|{d.get('requestId')}"):
                continue
            cc = u.get("cache_creation") or {}
            cw1h = cc.get("ephemeral_1h_input_tokens") or 0
            cw_total = u.get("cache_creation_input_tokens") or 0
            details = u.get("output_tokens_details") or {}
            store.add(
                {"hour": hour_of(d.get("timestamp", "")), "tool": "claude", "model": model, "effort": "",
                 "project": project_name(d.get("cwd", ""), path), "session": d.get("sessionId", ""),
                 "side": "sub" if (d.get("isSidechain") or is_sub_file) else "main", "host": host},
                {"n": 1, "in": u.get("input_tokens") or 0, "cw5": max(cw_total - cw1h, 0), "cw1h": cw1h,
                 "cr": u.get("cache_read_input_tokens") or 0, "out": u.get("output_tokens") or 0,
                 "think": details.get("thinking_tokens") or 0})
            added += 1
    return added
