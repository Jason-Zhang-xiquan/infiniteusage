"""iu api：用户自己加的免费 API（OpenAI 兼容接口），给 iu sum / iu ask 用；OpenCode 免费额度用完时也给 iu delegate 当后备模型。

原则（docs/free_providers.md）：每家一个账号、用官方免费层、用户自己注册、自己输入 key。
- key 只存在 ~/.infiniteusage/keys.env（权限 600），这里和 providers.json 都只记环境变量名，不记 key 的值
- iu api add 要在用户自己的终端里运行：key 用 getpass 输入，不显示、不进日志；没有终端（agent 在跑）就拒绝
- 免费模型名经常变，所以不写死：add 时查一次这家的 /models，按 PREFER 挑一个，用户可以 --model 指定
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .i18n import t
from .store import DATA_DIR

PROVIDERS = os.path.join(DATA_DIR, "providers.json")
KEYS = os.path.join(DATA_DIR, "keys.env")

# 正规免费层（2026-10，额度会变，以官网为准）。prefer：挑模型时按顺序找第一个匹配的 id
PRESETS = {
    "gemini": {"name": "Google AI Studio (Gemini)", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key_env": "GEMINI_API_KEY", "site": "https://aistudio.google.com/apikey",
               "prefer": [r"gemini-[\d.]+-flash$", r"gemini-.*flash(?!.*(lite|image|tts|live|audio))", r"gemini"],
               "note": t("免费层的数据会被用来改进产品", "free-tier data may be used to improve Google products")},
    "groq": {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "site": "https://console.groq.com/keys", "prefer": [r"llama-3\.3-70b", r"70b", r"llama"], "note": ""},
    "openrouter": {"name": "OpenRouter (:free models)", "base_url": "https://openrouter.ai/api/v1",
                   "key_env": "OPENROUTER_API_KEY", "site": "https://openrouter.ai/settings/keys",
                   "prefer": [r"deepseek.*:free$", r"qwen.*:free$", r":free$"],
                   "note": t("没充值过每天只有 50 次；免费模型的服务商可能留存数据",
                             "50 requests/day until you have topped up once; free-model hosts may keep data")},
    "cerebras": {"name": "Cerebras", "base_url": "https://api.cerebras.ai/v1", "key_env": "CEREBRAS_API_KEY",
                 "site": "https://cloud.cerebras.ai", "prefer": [r"qwen", r"llama", r"gpt-oss"], "note": ""},
    "mistral": {"name": "Mistral", "base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY",
                "site": "https://console.mistral.ai/api-keys", "prefer": [r"mistral-small-latest", r"small", r"latest"],
                "note": t("免费层可能拿数据训练，可以在后台退出", "free tier may train on data; you can opt out")},
    "nvidia": {"name": "NVIDIA build", "base_url": "https://integrate.api.nvidia.com/v1", "key_env": "NVIDIA_API_KEY",
               "site": "https://build.nvidia.com", "prefer": [r"llama-3\.3-70b", r"70b", r"instruct"], "note": ""},
    "zhipu": {"name": t("智谱 GLM（免费 Flash 模型）", "Zhipu GLM (free Flash models)"),
              "base_url": "https://open.bigmodel.cn/api/paas/v4", "key_env": "ZHIPUAI_API_KEY",
              "site": "https://open.bigmodel.cn", "prefer": [r"glm-[\d.]+-flash$", r"flash"], "note": ""},
    "siliconflow": {"name": t("硅基流动（免费模型）", "SiliconFlow (free models)"), "base_url": "https://api.siliconflow.cn/v1",
                    "key_env": "SILICONFLOW_API_KEY", "site": "https://cloud.siliconflow.cn",
                    "prefer": [r"Qwen.*7B", r"Qwen", r"GLM"], "note": ""},
    "dashscope": {"name": t("阿里云百炼", "Alibaba Model Studio"),
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key_env": "DASHSCOPE_API_KEY",
                  "site": "https://bailian.console.aliyun.com", "prefer": [r"qwen-flash", r"qwen-turbo", r"qwen-plus"],
                  "note": t("新人免费额度，用完收费", "free quota for new accounts, paid after that")},
}


def load() -> list:
    try:
        with open(PROVIDERS, encoding="utf-8") as f:
            return json.load(f).get("providers", [])
    except (OSError, ValueError):
        return []


def save(items: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PROVIDERS, "w", encoding="utf-8") as f:
        json.dump({"providers": items}, f, ensure_ascii=False, indent=1)


def key_of(key_env: str):
    from .offload import setting
    return setting(key_env)


def ready() -> list:
    """配好了 key 的那些（按用户加的顺序）"""
    return [p for p in load() if key_of(p.get("key_env", ""))]


def allowed(p: dict, task: str) -> bool:
    """资格测试（iu/qualify.py）没过这类任务就不用；没测过的不拦"""
    r = (p.get("qualify") or {}).get("types", {}).get(task)
    return r is None or r.get("ok") is not False  # None = 没测成（限流等），不拦


def write_key(key_env: str, value: str):
    """把 KEY_ENV=value 写进 keys.env：别的行原样保留，同名的那行替换掉；文件权限 600"""
    os.makedirs(DATA_DIR, exist_ok=True)
    lines = []
    if os.path.exists(KEYS):
        with open(KEYS, encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if not x.startswith(key_env + "=")]
    lines.append(f"{key_env}={value}")
    fd = os.open(KEYS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(KEYS, 0o600)
    except OSError:
        pass


def list_models(base_url: str, key: str, timeout: int = 20) -> list:
    req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    ids = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    return [i.split("/", 1)[1] if i.startswith("models/") else i for i in ids if i]  # Gemini 的 id 带 models/ 前缀


def pick_model(ids: list, prefer: list):
    for pat in prefer:
        hits = sorted((i for i in ids if re.search(pat, i, re.I)), reverse=True)  # 版本号大的排前面
        if hits:
            return hits[0]
    return ids[0] if ids else None


def cmd_api(args):
    action = args.action
    items = load()
    if action == "list":
        if not items:
            print(t("还没有加免费 API。", "No free APIs added yet."))
        for p in items:
            ok = "✓" if key_of(p["key_env"]) else t("✗ 缺 key", "✗ no key")
            print(f"  {p['id']:<12} {ok}  {p['model']:<40} {p['base_url']}")
            q = (p.get("qualify") or {}).get("types")
            if q:
                print(t("     资格测试：", "     qualification: ") + "  ".join(
                    mark_of(r) + " " + qualify_label(k) for k, r in q.items()))
            else:
                print(t(f"     还没做资格测试：iu api qualify {p['id']}", f"     not qualified yet: iu api qualify {p['id']}"))
        print(t("\n可以加的（自己去官网注册，一家一个账号）：", "\nAvailable presets (register yourself, one account per provider):"))
        for k, v in PRESETS.items():
            print(f"  {k:<12} {v['name']:<36} {v['site']}" + (f"  · {v['note']}" if v["note"] else ""))
        print(t("\n加一个：在你自己的终端运行  iu api add gemini", "\nAdd one in your own terminal:  iu api add gemini"))
        return
    name = args.name
    if not name:
        raise SystemExit(t("要给名字，比如 iu api add gemini", "Give a name, e.g. iu api add gemini"))
    if action == "remove":
        save([p for p in items if p["id"] != name])
        print(t(f"已移除 {name}（keys.env 里的 key 没动，要删请自己编辑）",
                f"Removed {name} (the key in keys.env is left alone; edit it yourself to delete it)"))
        return
    if action == "qualify":
        from .qualify import qualify_and_save
        qualify_and_save(name)
        return
    if action == "test":
        targets = [p for p in items if p["id"] == name] or []
        if not targets:
            raise SystemExit(t(f"没有叫 {name} 的 API，先 iu api add {name}", f"No API named {name}; run iu api add {name} first"))
        from .offload import chat
        res = chat("Reply with exactly: OK", "Say OK.", max_tokens=20, timeout=60, attempts=1, backend=targets[0])
        text = ((res.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        print(t(f"{name} 能用：{res.get('model')} 回复 {text.strip()[:40]!r}，{res.get('_seconds')} 秒",
                f"{name} works: {res.get('model')} replied {text.strip()[:40]!r} in {res.get('_seconds')} s"))
        return
    # add
    preset = PRESETS.get(name, {})
    base_url = args.base_url or preset.get("base_url")
    key_env = args.key_env or preset.get("key_env") or re.sub(r"\W", "_", name).upper() + "_API_KEY"
    if not base_url:
        raise SystemExit(t(f"{name} 不是内置的，要给 --base-url（OpenAI 兼容接口，比如 https://example.com/v1）",
                           f"{name} is not a preset; pass --base-url (an OpenAI-compatible endpoint, e.g. https://example.com/v1)"))
    host = urllib.parse.urlparse(base_url).hostname or ""
    if not base_url.startswith("https://") and host not in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit(t(f"{base_url} 不是 https：key 会明文传出去。只有本机地址（localhost）可以用 http",
                           f"{base_url} is not https, so the key would travel in clear text. Only local addresses may use http"))
    key = key_of(key_env)
    if not key:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):  # Windows 上 stdin 接 NUL 也算 tty，所以连 stdout 一起看
            raise SystemExit(t(f"要在你自己的终端里运行这条命令来输入 key（agent 不能替你输入）。"
                               f"先去 {preset.get('site', base_url)} 注册拿 key",
                               f"Run this command in your own terminal to type the key (an agent must not enter it for you). "
                               f"Get a key at {preset.get('site', base_url)} first"))
        if preset.get("site"):
            print(t(f"去 {preset['site']} 注册并创建 key（一家一个账号）", f"Create a key at {preset['site']} (one account per provider)"))
        key = getpass.getpass(t(f"粘贴 {key_env}（输入不显示）: ", f"Paste {key_env} (input hidden): ")).strip()
        if not key:
            raise SystemExit(t("没输入 key，没保存", "No key entered; nothing saved"))
        write_key(key_env, key)
        print(t(f"key 已存进 {KEYS}（只有你能读）", f"Key saved to {KEYS} (readable only by you)"))
    model = args.model
    if not model:
        try:
            ids = list_models(base_url, key)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SystemExit(t(f"查不到 {base_url}/models（{str(e)[:120]}）：用 --model 直接指定模型",
                               f"Could not list {base_url}/models ({str(e)[:120]}); pass --model"))
        model = pick_model(ids, preset.get("prefer", []))
        if not model:
            raise SystemExit(t("这家没有返回任何模型：用 --model 指定", "No models returned; pass --model"))
    items = [p for p in items if p["id"] != name] + [{"id": name, "base_url": base_url, "key_env": key_env, "model": model,
                                                       "added_at": time.time()}]
    save(items)
    print(t(f"已加 {name}：模型 {model}。iu sum 会在 FreeLLMAPI 之后（没有的话直接）按加的顺序用它；OpenCode 免费额度用完时，iu delegate 也用它",
            f"Added {name}: model {model}. iu sum uses it after FreeLLMAPI (or first, if you have none), in the order "
            f"added; iu delegate also uses it once OpenCode's free quota runs out"))
    if getattr(args, "no_qualify", False):
        print(t(f"跳过了资格测试：之后可以 iu api qualify {name}", f"Qualification skipped; run iu api qualify {name} later"))
        return
    from .qualify import qualify_and_save
    qualify_and_save(name)


def qualify_label(task: str) -> str:
    from .qualify import label
    return label(task)


def mark_of(r: dict) -> str:
    from . import qualify
    return qualify.mark_of(r)
