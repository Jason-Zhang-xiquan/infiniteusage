"""中英双语：每句给用户看的话都同时写中文和英文，t(中文, English) 按语言选一句。

语言怎么定：环境变量 IU_LANG（zh / en）优先；没设就看系统语言（LANG / LC_ALL / LC_MESSAGES，Windows 看界面语言），
中文系统用中文，其余用英文。给模型看的 hook 文字一律英文，不走这里。
"""

from __future__ import annotations

import functools
import locale
import os


@functools.lru_cache(maxsize=1)
def lang() -> str:
    v = (os.environ.get("IU_LANG") or "").lower()
    if v.startswith("zh") or v == "cn":
        return "zh"
    if v.startswith("en"):
        return "en"
    for k in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        if os.environ.get(k):
            return "zh" if os.environ[k].lower().startswith("zh") else "en"
    if os.name == "nt":
        try:
            import ctypes
            # 0x04 = 中文（简体、繁体都是这个主语言号）
            return "zh" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x04 else "en"
        except Exception:  # noqa: BLE001
            pass
    try:
        loc = locale.getlocale()[0] or ""
    except ValueError:
        loc = ""
    return "zh" if loc.lower().startswith(("zh", "chinese")) else "en"


def t(zh: str, en: str) -> str:
    return zh if lang() == "zh" else en
