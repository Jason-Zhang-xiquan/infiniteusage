"""iu agents：找出这台电脑上能接手改代码子任务的 coding agent 和免费 API，没有的给出当地能用的安装 / 注册建议。

每个 agent 记录：
- 怎么找到它
- 脚本化命令模板（{prompt} 是任务说明，{dir} 是项目目录）
- 花费类别：free 免费 / byo 自带 key / sub 用户的付费订阅
- 实测状态：tested 我们测过；untested 只按官方文档写了命令

委托顺序（iu delegate 用）存在 ~/.infiniteusage/agents.json 的 "chain" 里。
默认：找到的 free + tested 的 agent。付费订阅（比如 Codex）默认不接，要用户自己加进 chain。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from concurrent.futures import ThreadPoolExecutor

from .store import DATA_DIR
from .i18n import t
from .providers import PRESETS

CONFIG = os.path.join(DATA_DIR, "agents.json")

AGENTS = [
    {"id": "opencode", "name": t("OpenCode（Zen 免费模型）", "OpenCode (Zen free models)"), "bins": ["opencode"], "cost": "free", "status": "tested",
     "cmd": None,  # 特殊处理：按 delegate.coder_models() 逐个模型试
     "install": "npm i -g opencode-ai", "region": "global",
     "note": t("不用登录；测过的 7 个免费模型修 bug 47/48 通过", "no login; 7 free models tested fixed 47/48 bugs")},
    {"id": "kilo", "name": "Kilo CLI (kilo-auto/free)", "bins": ["kilo", "kilocode"], "cost": "free", "status": "untested",
     "cmd": ["{bin}", "run", "--auto", "{prompt}"], "install": "npm i -g @kilocode/cli", "region": "global",
     "note": t("自动路由到免费模型", "routes to free models automatically")},
    {"id": "codebuddy", "name": t("腾讯 CodeBuddy Code", "Tencent CodeBuddy Code"), "bins": ["codebuddy"], "cost": "free", "status": "untested",
     "cmd": ["{bin}", "-p", "{prompt}", "-y"], "install": t("见 https://www.codebuddy.cn（国内体验版每月 500 积分，和 WorkBuddy 共用）", "see https://www.codebuddy.cn (China trial: 500 credits/month, shared with WorkBuddy)"),
     "region": "cn", "note": t("国内可直连", "reachable from mainland China")},
    {"id": "qwen", "name": t("Qwen Code（自带 key）", "Qwen Code (bring your own key)"), "bins": ["qwen"], "cost": "byo", "status": "untested",
     "cmd": ["{bin}", "-p", "{prompt}", "--yolo"], "install": "npm i -g @qwen-code/qwen-code",
     "region": "both", "note": t("免费登录已停；接智谱 GLM-4.7-Flash（免费）等 OpenAI 兼容 key", "free login ended; use an OpenAI-compatible key such as Zhipu GLM-4.7-Flash (free)")},
    {"id": "crush", "name": t("Crush（自带 key）", "Crush (bring your own key)"), "bins": ["crush"], "cost": "byo", "status": "untested",
     "cmd": ["{bin}", "run", "-y", "{prompt}"], "install": "npm i -g @charmland/crush", "region": "both",
     "note": t("接 OpenRouter :free / Groq / 国产免费 API", "use OpenRouter :free / Groq / other free APIs")},
    {"id": "aider", "name": t("Aider（自带 key）", "Aider (bring your own key)"), "bins": ["aider"], "cost": "byo", "status": "untested",
     "cmd": ["{bin}", "--message", "{prompt}", "--yes-always"], "install": "pip install aider-install && aider-install",
     "region": "both", "note": t("接 OpenRouter :free / Groq / 国产免费 API", "use OpenRouter :free / Groq / other free APIs")},
    {"id": "goose", "name": t("Goose（自带 key）", "Goose (bring your own key)"), "bins": ["goose"], "cost": "byo", "status": "untested",
     "cmd": ["{bin}", "run", "-t", "{prompt}"], "install": t("见 https://block.github.io/goose", "see https://block.github.io/goose"), "region": "both",
     "note": t("接任意 OpenAI 兼容 key", "any OpenAI-compatible key")},
    {"id": "copilot", "name": "GitHub Copilot CLI", "bins": ["copilot"], "cost": "sub", "status": "untested",
     "cmd": ["{bin}", "-p", "{prompt}", "--allow-all-tools"], "install": "npm i -g @github/copilot", "region": "global",
     "note": t("Free 版额度很小", "the Free plan quota is small")},
    {"id": "cursor", "name": "Cursor CLI", "bins": ["cursor-agent", "agent"], "cost": "sub", "status": "untested",
     "cmd": ["{bin}", "-p", "--force", "{prompt}"], "install": t("见 https://cursor.com/cli", "see https://cursor.com/cli"), "region": "global",
     "note": t("Hobby 版次数很少", "the Hobby plan has few requests")},
    {"id": "codex", "name": "OpenAI Codex CLI", "bins": ["codex"], "cost": "sub", "status": "untested",
     "cmd": ["{bin}", "exec", "--sandbox", "workspace-write", "--ephemeral", "-C", "{dir}", "{prompt}"],
     "install": "npm i -g @openai/codex", "region": "global", "note": t("用 ChatGPT 订阅的额度；默认不接", "uses your ChatGPT subscription quota; not used by default")},
]

# 免费 API：FreeLLMAPI 统一入口，加上 iu api 的那些预设（iu/providers.py，同一份清单）。只看环境变量和 keys.env，不读也不打印 key 的值
CN_ONLY = {"zhipu", "siliconflow", "dashscope"}
FREE_APIS = [
    {"id": "freellmapi", "name": t("FreeLLMAPI 统一入口", "FreeLLMAPI gateway"), "env": ["FREELLMAPI_KEY"], "region": "global",
     "how": t("装 FreeLLMAPI（docs/freellmapi_setup.md），把各家免费 key 填进它的后台", "install FreeLLMAPI (docs/freellmapi_setup.md) and add your free keys in its dashboard")},
] + [{"id": k, "name": v["name"], "env": [v["key_env"]], "region": "cn" if k in CN_ONLY else "global",
      "how": t(f"{v['site']} 注册拿 key", f"get a key at {v['site']}") + (f"（{v['note']}）" if v["note"] else "")}
     for k, v in PRESETS.items()]

PROBES = {"global": ["opencode.ai", "openrouter.ai", "api.groq.com"], "cn": ["open.bigmodel.cn", "dashscope.aliyuncs.com"]}


def find_bin(agent: dict):
    extra = []
    if agent["id"] == "opencode":
        from .explore import opencode_bin
        try:
            extra.append(opencode_bin())
        except SystemExit:
            pass
    for b in extra + [shutil.which(b) for b in agent["bins"]]:
        if b and os.path.exists(b):
            return b
    return None


def _reach(host: str, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except OSError:
        return False


def region() -> dict:
    hosts = [h for v in PROBES.values() for h in v]
    with ThreadPoolExecutor(len(hosts)) as ex:
        ok = dict(zip(hosts, ex.map(_reach, hosts)))
    g = sum(ok[h] for h in PROBES["global"])
    c = sum(ok[h] for h in PROBES["cn"])
    guess = "cn" if c and g < len(PROBES["global"]) else "global"
    return {"guess": guess, "reachable": ok}


def has_key(api: dict) -> bool:
    if any(os.environ.get(e) for e in api["env"]):
        return True
    try:
        text = open(os.path.join(DATA_DIR, "keys.env"), encoding="utf-8").read()
    except OSError:
        return False
    return any(f"{e}=" in text for e in api["env"])


def load_config() -> dict:
    try:
        return json.load(open(CONFIG, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def scan() -> dict:
    found = []
    for a in AGENTS:
        b = find_bin(a)
        if b:
            found.append({**{k: a[k] for k in ("id", "name", "cost", "status", "note")}, "bin": b})
    apis = [{"id": x["id"], "name": x["name"], "configured": has_key(x)} for x in FREE_APIS]
    return {"agents": found, "apis": apis, "region": region()}


def default_chain(found: list) -> list:
    """默认委托顺序：找到的免费 agent，测过的排前面；自带 key 的放后面；付费订阅不默认接"""
    rank = {("free", "tested"): 0, ("free", "untested"): 1, ("byo", "tested"): 2, ("byo", "untested"): 3}
    ok = [a for a in found if (a["cost"], a["status"]) in rank]
    return [a["id"] for a in sorted(ok, key=lambda a: rank[(a["cost"], a["status"])])]


def chain() -> list:
    cfg = load_config()
    if cfg.get("chain"):
        return cfg["chain"]
    return default_chain([{"id": a["id"], "cost": a["cost"], "status": a["status"]} for a in AGENTS if find_bin(a)])


def recommend(info: dict) -> list:
    reg = info["region"]["guess"]
    have = {a["id"] for a in info["agents"]}
    recs = []
    for a in AGENTS:
        if a["id"] in have or a["cost"] == "sub" or a["region"] not in (reg, "both"):
            continue
        if a["cost"] == "free" or (a["cost"] == "byo" and not recs):
            recs.append(t(f"装 {a['name']}：{a['install']}（{a['note']}；命令以官网为准）",
                             f"Install {a['name']}: {a['install']} ({a['note']}; check the official site for the command)"))
    for x in FREE_APIS:
        if x["region"] in (reg, "both") and not has_key(x):
            recs.append(t(f"注册 {x['name']}：{x['how']}；然后在你自己的终端运行 iu api add {x['id']}（key 不要交给 agent）",
                             f"Register {x['name']}: {x['how']}; then run iu api add {x['id']} in your own terminal (do not give the key to an agent)"))
    return recs


def cmd_agents(args):
    info = scan()
    if args.json:
        info["chain"] = chain()
        info["recommend"] = recommend(info)
        print(json.dumps(info, ensure_ascii=False, indent=1))
        return
    reg = info["region"]
    print(t(f"网络：判断为 {'国内' if reg['guess'] == 'cn' else '海外'}（", f"Network: looks like {'mainland China' if reg['guess'] == 'cn' else 'outside mainland China'} (")
          + t("，", ", ").join(f"{h} {t('通', 'ok') if ok else t('不通', 'unreachable')}" for h, ok in reg["reachable"].items()) + t("）", ")"))
    label = {"free": t("免费", "free"), "byo": t("自带 key", "own key"), "sub": t("付费订阅", "paid subscription")}
    print(t("\n这台电脑上找到的 coding agent：", "\nCoding agents found on this machine:"))
    for a in info["agents"]:
        print(f"  · {a['name']}  [{label[a['cost']]}{t('，已实测', ', tested') if a['status'] == 'tested' else t('，未实测', ', untested')}]  {a['bin']}")
    if not info["agents"]:
        print(t("  （一个都没有）", "  (none)"))
    print(t("\n免费 API：", "\nFree APIs: ") + t("，", ", ").join(f"{x['name']} {'✓' if x['configured'] else '✗'}" for x in info["apis"]))
    ch = chain()
    src = "~/.infiniteusage/agents.json" if load_config().get("chain") else t("默认（免费的在前，付费订阅不接）", "default (free first, paid subscriptions excluded)")
    print(t("\n委托顺序：", "\nDelegation order: ") + (' → '.join(ch) or t('（空：iu delegate 会直接告诉调用方自己做）', '(empty: iu delegate tells the caller to do it itself)'))
          + t("  来源：", "  source: ") + src)
    recs = recommend(info)
    if recs:
        print(t("\n建议（装或注册之前先问用户）：", "\nSuggestions (ask the user before installing or registering anything):"))
        for r in recs:
            print("  · " + r)
    if args.set_chain is not None:
        ids = [x for x in args.set_chain.split(",") if x]
        known = {a["id"] for a in AGENTS}
        bad = [x for x in ids if x not in known]
        if bad:
            raise SystemExit(t(f"不认识的 agent：{bad}；可选：{sorted(known)}", f"Unknown agent: {bad}; choose from {sorted(known)}"))
        cfg = load_config()
        cfg["chain"] = ids
        os.makedirs(DATA_DIR, exist_ok=True)
        json.dump(cfg, open(CONFIG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(t("\n已保存委托顺序：", "\nSaved delegation order: ") + (' → '.join(ids) or t('（恢复默认）', '(back to default)')))


def agent_by_id(aid: str) -> dict:
    for a in AGENTS:
        if a["id"] == aid:
            return a
    raise KeyError(aid)


def build_cmd(agent: dict, binary: str, prompt: str, root: str) -> list:
    out = []
    for part in agent["cmd"]:
        out.append(part.replace("{bin}", binary).replace("{dir}", root) if part != "{prompt}" else prompt)
    return out

