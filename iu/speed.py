"""免费模型测速：真实调用顺便记下快慢和成败，过期了才在后台补测；排序只在合格的（路由表、委托顺序里已有的）之间挑快的。

- 记录：~/.infiniteusage/speed.json，每个后端一条：real（真实调用秒数的滑动平均）、probe（小探测的秒数）、
  最近一次成功 / 失败的时间、连续失败次数。几个进程同时写可能丢一条更新，不要紧
- 排序：最近失败过、之后还没成功、还在冷却期的放最后；其余按估计耗时分档（加上 5 秒余量后每慢 1.5 倍一档），同一档里保持原来的
  顺序（路由表是从强到弱排的），所以差不多快的时候还是强的先试。没数据的按已知的中位数算
- 补测：超过 PROBE_TTL 没测，就在后台起一个进程并行测一遍，调用方不等它；
  按次数限额的免费层（OpenRouter 没充值一天 50 次）不测，只靠真实调用的记录
- 键：freellmapi:<模型>、api:<名字>:<模型>、agent:<委托尝试名>（比如 agent:opencode/deepseek-v4.1-flash-free）
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .i18n import t
from .store import DATA_DIR

PROBE_TTL = 3600  # 多久没测就补测
KICK_GAP = 600  # 刚起过补测进程的话，这么久之内不再起
COOLDOWN = 900  # 失败后多久内排到最后；连续失败越多冷却越久（最多 4 倍）
ALPHA = 0.4  # 滑动平均里新数据的权重
PROBE_FACTOR = 4  # 只有探测数据时，估计一次真实调用大约是探测的几倍时间
BAND = 1.5  # 估计耗时差不到这个倍数算一样快，不为了噪声打乱强弱顺序
SLACK = 5  # 秒：分档前先都加上这么多，几秒以内的差别不算（一次真实摘要本来就要 5~20 秒）
NO_PROBE = {"openrouter"}  # 按次数限额，不拿来探测


def path() -> str:
    return os.environ.get("IU_SPEED_FILE") or os.path.join(DATA_DIR, "speed.json")  # 测试里指到临时文件


def load() -> dict:
    try:
        with open(path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(d: dict):
    os.makedirs(os.path.dirname(path()), exist_ok=True)
    tmp = path() + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path())


def record(key: str, ok: bool, seconds: float = None, probe: bool = False):
    try:
        d = load()
        e = d.setdefault(key, {})
        now = time.time()
        if ok:
            field = "probe" if probe else "real"
            if seconds is not None:
                e[field] = round(seconds if field not in e else ALPHA * seconds + (1 - ALPHA) * e[field], 2)
            e["ok_at"], e["fails"] = now, 0
        else:
            e["fail_at"], e["fails"] = now, e.get("fails", 0) + 1
        save(d)
    except OSError:  # 记不下来不影响调用本身
        pass


def estimate(e: dict):
    if "real" in e:
        return e["real"]
    if "probe" in e:
        return e["probe"] * PROBE_FACTOR
    return None


def cooling(e: dict, now: float = None) -> bool:
    now = now or time.time()
    fail = e.get("fail_at", 0)
    return fail > e.get("ok_at", 0) and now - fail < COOLDOWN * min(max(e.get("fails", 1), 1), 4)


def rank(items: list, key_of=lambda x: x) -> list:
    d, now = load(), time.time()
    entries = [d.get(key_of(x), {}) for x in items]
    ests = [estimate(e) for e in entries]
    known = sorted(x for x in ests if x is not None)
    mid = known[len(known) // 2] if known else 0
    fastest = known[0] if known else 0

    def band(x):
        return int(math.log((max(x, fastest) + SLACK) / (fastest + SLACK)) / math.log(BAND) + 1e-9)

    order = sorted(range(len(items)),
                   key=lambda i: (cooling(entries[i], now), band(ests[i] if ests[i] is not None else mid), i))
    return [items[i] for i in order]


# ---------------- 探测 ----------------

def targets() -> list:
    """[(键, backend, 模型)]：配了 key 的 FreeLLMAPI 路由表里的模型，加上用户用 iu api add 加的（按次数限额的除外）"""
    from .offload import ROUTES, setting
    from .providers import ready
    out = []
    if setting("FREELLMAPI_KEY"):
        try:
            with open(ROUTES, encoding="utf-8") as f:
                routes = json.load(f).get("routes", {})
        except (OSError, ValueError):
            routes = {}
        models = []
        for chain in routes.values() or [["auto"]]:
            models += [m for m in chain if m not in models]
        out += [(f"freellmapi:{m}", None, m) for m in models or ["auto"]]
    out += [(f"api:{b['id']}:{b['model']}", b, b["model"]) for b in ready() if b["id"] not in NO_PROBE]
    return out


def probe_all(timeout: int = 25) -> list:
    """并行给每个目标发一句很短的话，记下耗时。返回 [(键, 秒数或 None, 错误)]"""
    from .offload import chat

    def one(tg):
        key, backend, model = tg
        t0 = time.time()
        try:
            chat("Reply with exactly: OK", "Say OK.", model=model, max_tokens=64, timeout=timeout, attempts=1,
                 backend=backend)
        except SystemExit as e:
            record(key, False)
            return key, None, str(e)[:100]
        secs = round(time.time() - t0, 2)
        record(key, True, secs, probe=True)
        return key, secs, None

    tgs = targets()
    with ThreadPoolExecutor(max_workers=min(8, len(tgs) or 1)) as ex:
        res = list(ex.map(one, tgs))
    d = load()
    d.setdefault("_meta", {})["probed_at"] = time.time()
    save(d)
    return res


def kick_if_stale():
    """超过 PROBE_TTL 没测过就在后台起一个进程补测，不等它。IU_NO_SPEED_PROBE=1 关掉"""
    if os.environ.get("IU_NO_SPEED_PROBE"):
        return
    try:
        d = load()
        meta = d.setdefault("_meta", {})
        now = time.time()
        if now - meta.get("probed_at", 0) < PROBE_TTL or now - meta.get("kicked_at", 0) < KICK_GAP:
            return
        meta["kicked_at"] = now
        save(d)
        runner = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_iu.py")
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
        else:
            kw["start_new_session"] = True
        subprocess.Popen([sys.executable, runner, "speed", "--probe", "--quiet"], **kw)
    except OSError:
        pass


def cmd_speed(args):
    if args.probe:
        res = probe_all()
        if args.quiet:
            return
        for key, secs, err in res:
            print(f"  {key:<52} " + (f"{secs:>6.1f} s" if secs is not None else t("失败：", "failed: ") + err))
        if not res:
            print(t("没有可以测的免费 API：先 iu api add <名字>，或者配好 FreeLLMAPI",
                    "Nothing to probe: add a free API with iu api add <name>, or set up FreeLLMAPI"))
        return
    d = load()
    rows = {k: v for k, v in d.items() if not k.startswith("_")}
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return
    if not rows:
        print(t("还没有测速数据：iu speed --probe 测一次，或者用一次 iu sum / iu delegate",
                "No speed data yet: run iu speed --probe, or use iu sum / iu delegate once"))
        return
    print(f"  {'':<52} {t('真实调用', 'real'):>8} {t('探测', 'probe'):>7}  {t('状态', 'status')}")
    for k in sorted(rows, key=lambda k: (cooling(rows[k]), estimate(rows[k]) or 1e9)):
        e = rows[k]
        real = f"{e['real']:.1f} s" if "real" in e else "-"
        pr = f"{e['probe']:.1f} s" if "probe" in e else "-"
        state = t(f"冷却中（连续失败 {e.get('fails')} 次）", f"cooling down ({e.get('fails')} failures in a row)") \
            if cooling(e) else t("正常", "ok")
        print(f"  {k:<52} {real:>8} {pr:>7}  {state}")
    probed = (d.get("_meta") or {}).get("probed_at")
    print(t("\n上次探测：", "\nLast probe: ") + (time.strftime("%m-%d %H:%M", time.localtime(probed)) if probed else t("没有", "never")))
