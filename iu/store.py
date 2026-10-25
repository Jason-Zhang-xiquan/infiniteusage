"""增量缓存：日志是只追加的 jsonl，记住每个文件读到哪个字节，下次只读新增部分。

缓存里存的是按「小时 × 工具 × 模型 × effort × 项目 × 会话 × 是否子 agent」聚合好的 token 桶，
外加去重用的请求 id 哈希（同一条请求会因为续接/分叉会话出现在多个文件里）。
文件变小或被改写时整库重建。
"""

from __future__ import annotations

import hashlib
import json
import os
import time

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get("IU_HOME") or os.path.join(HOME, ".infiniteusage")
CACHE_VERSION = 3

# 行的维度（顺序固定，拼成 key）
DIMS = ("hour", "tool", "model", "effort", "project", "session", "side", "host")
# token 桶：Claude 用 in/cw5/cw1h/cr/out，Codex 用 in/cr/out/think；n = 请求数
BUCKETS = ("n", "in", "cw5", "cw1h", "cr", "out", "think")


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:14]


class Store:
    def __init__(self, name: str):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.path = os.path.join(DATA_DIR, f"cache-{name}.json")
        self.files: dict = {}
        self.rows: dict = {}
        self.seen: set = set()
        self.extra: dict = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") == CACHE_VERSION:
                self.files = data["files"]
                self.rows = data["rows"]
                self.seen = set(data["seen"])
                self.extra = data.get("extra", {})
        except (OSError, ValueError, KeyError):
            pass

    def reset(self):
        self.files, self.rows, self.seen, self.extra = {}, {}, set(), {}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "saved": time.time(), "files": self.files, "rows": self.rows,
                       "seen": sorted(self.seen), "extra": self.extra}, f, separators=(",", ":"))
        os.replace(tmp, self.path)

    def add(self, dims: dict, buckets: dict):
        key = "\t".join(str(dims.get(d, "")) for d in DIMS)
        row = self.rows.setdefault(key, {})
        for b, v in buckets.items():
            if v:
                row[b] = row.get(b, 0) + v

    def first_time(self, request_key: str) -> bool:
        """去重：这条请求以前没算过就记下并返回 True"""
        h = short_hash(request_key)
        if h in self.seen:
            return False
        self.seen.add(h)
        return True

    def needs_rebuild(self, paths: list) -> bool:
        """有文件变小（被改写、截断）就得重建，否则增量读就会重复计数"""
        for p in paths:
            st = self.files.get(p)
            if st and os.path.exists(p) and os.path.getsize(p) < st["offset"]:
                return True
        return False


def read_new_lines(path: str, offset: int):
    """从 offset 开始读完整的行（最后半行留到下次）。返回 (行列表 bytes, 新 offset)"""
    size = os.path.getsize(path)
    if size <= offset:
        return [], offset
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(size - offset)
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    return data[:end].split(b"\n"), offset + end + 1


def iter_rows(rows: dict):
    """把缓存里的 key 拆回维度 dict"""
    for key, buckets in rows.items():
        parts = key.split("\t")
        dims = dict(zip(DIMS, parts))
        yield dims, buckets


def hour_of(ts: str) -> str:
    """ISO 时间戳 → UTC 整点 'YYYY-MM-DDTHH'"""
    return ts[:13] if ts else ""
