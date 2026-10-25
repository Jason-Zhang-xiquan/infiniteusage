"""把体力活外包给免费模型（经 FreeLLMAPI 统一入口），只把结论交回给付费 agent。

- iu ask "问题" [文件...]：免费模型读文件、回答问题或写草稿
- iu sum -- <命令...>：跑命令；输出短就原样打印，长就交给免费模型压成摘要，原始输出存成文件备查

护栏：
- 项目里（当前目录往上找）有 .iu-private 标记时拒绝外包：免费线路可能拿提示词训练
- 跳过 .env、私钥一类文件，正文里像密钥的内容替换成 [REDACTED]
- 默认 reasoning_effort=none：免费模型的思考过程既慢又占输出，摘要类任务用不上
- 每次调用记一行到 ~/.infiniteusage/offload.jsonl（免费 token、路由到哪个模型、耗时），账本据此统计外包了多少
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .det import det_filter
from .i18n import t
from .store import DATA_DIR

DEFAULT_URL = "http://127.0.0.1:3001"
LOG = os.path.join(DATA_DIR, "offload.jsonl")
MAX_INPUT_CHARS = 300_000  # 免费模型上下文多在 128K~262K token，留余量
RAW_SHOW_LIMIT = 2_000  # 命令输出短于这个就原样给，不值得调模型

SKIP_FILES = re.compile(r"(^|[\\/])(\.env(\..*)?|id_rsa.*|id_ed25519.*|.*\.pem|.*\.key|credentials.*\.json|keys\.env)$", re.I)
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
               r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|xox[abpr]-[A-Za-z0-9\-]{10,}|gsk_[A-Za-z0-9]{20,}"
               r"|freellmapi-[A-Za-z0-9_\-]{10,})\b"),
    re.compile(r"(?im)^(\s*[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|APIKEY|PRIVATE_KEY)[A-Z0-9_]*\s*[=:]\s*)(\S.*)$"),
]


def redact(text: str) -> str:
    for pat in SECRET_PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


def private_marker(start: str = None):
    d = os.path.abspath(start or os.getcwd())
    while True:
        p = os.path.join(d, ".iu-private")
        if os.path.exists(p):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def setting(name: str):
    """先看环境变量，再看 ~/.infiniteusage/keys.env 里的 NAME=... 那一行；都没有返回 None"""
    if os.environ.get(name):
        return os.environ[name]
    try:
        with open(os.path.join(DATA_DIR, "keys.env"), encoding="utf-8") as f:
            for line in f:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def load_key() -> str:
    key = setting("FREELLMAPI_KEY")
    if key:
        return key
    path = os.path.join(DATA_DIR, "keys.env")
    raise SystemExit(t(f"没有 FreeLLMAPI 统一 key：在 {path} 写一行 FREELLMAPI_KEY=...（scripts/save_freellmapi_key.sh）", f"No FreeLLMAPI key: add a line FREELLMAPI_KEY=... to {path} (scripts/save_freellmapi_key.sh)"))


def base_url() -> str:
    return os.environ.get("FREELLMAPI_URL", DEFAULT_URL).rstrip("/")


def _port_open(url: str) -> bool:
    m = re.match(r"https?://([^:/]+):(\d+)", url)
    if not m:
        return True
    try:
        with socket.create_connection((m.group(1), int(m.group(2))), timeout=2):
            return True
    except OSError:
        return False


def ensure_tunnel(url: str):
    """FreeLLMAPI 跑在另一台机器上时（keys.env 里写 FREELLMAPI_SSH_HOST=那台的 ssh 主机名），本机 3001 没开就自动起一条
    SSH 隧道接到那台的 127.0.0.1:3001（后台常驻）。没写这一项就不开隧道"""
    host = setting("FREELLMAPI_SSH_HOST")
    if _port_open(url) or not url.startswith(DEFAULT_URL) or not host:
        return
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["ssh", "-N", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes",
                      "-L", "3001:127.0.0.1:3001", host], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=(os.name != "nt"))
    for _ in range(20):
        time.sleep(0.5)
        if _port_open(url):
            return
    raise SystemExit(t(f"连不上 FreeLLMAPI（{url}），SSH 隧道也没起来：检查那台机器上的服务（docs/freellmapi_setup.md）", f"Cannot reach FreeLLMAPI ({url}) and the SSH tunnel did not come up: check the service on that machine (docs/freellmapi_setup.md)"))


def chat(system: str, user: str, model: str = "auto", max_tokens: int = 1200, think: bool = False,
         timeout: int = 180, attempts: int = 3, backend: dict = None) -> dict:
    """backend=None 走 FreeLLMAPI；给了就是用户用 iu api add 加的那一家（iu/providers.py），用它自己的模型和 key"""
    if backend:
        url, endpoint, key = backend["base_url"].rstrip("/"), "/chat/completions", setting(backend["key_env"])
        model = backend["model"]
        if not key:
            raise SystemExit(f"{backend['id']}: no key ({backend['key_env']})")
    else:
        url = base_url()
        ensure_tunnel(url)
        endpoint, key = "/v1/chat/completions", load_key()
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if not think and not backend:  # 别家不一定认这个参数，不认会直接 400
        body["reasoning_effort"] = "none"
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(15 * attempt)  # 免费池全限流时等一会儿再试
        req = urllib.request.Request(url + endpoint, data=data, method="POST",
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
                out["_routed"] = f"{backend['id']}/{model}" if backend else resp.headers.get("X-Routed-Via", "")
                out["_seconds"] = round(time.time() - t0, 1)
                return out
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            if e.code not in (429, 502, 503, 504):
                break
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            last = str(e)
    raise SystemExit(t(f"免费模型调用失败：{last}", f"Free model call failed: {last}"))


def log_call(kind: str, res: dict, chars_in: int, extra: dict = None):
    u = res.get("usage") or {}
    rec = {"ts": time.time(), "kind": kind, "cwd": os.getcwd(), "routed": res.get("_routed"), "model": res.get("model"),
           "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
           "chars_in": chars_in, "seconds": res.get("_seconds"), **(extra or {})}
    try:  # 沙箱里写不了账本也不能影响命令的退出码
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def answer_text(res: dict) -> str:
    msg = ((res.get("choices") or [{}])[0].get("message") or {})
    text = (msg.get("content") or "").strip()
    # 有的免费模型把思考写在正文的 <think> 里
    return re.sub(r"<think>[\s\S]*?</think>\s*", "", text).strip()


def footer(res: dict) -> str:
    u = res.get("usage") or {}
    return (f"[iu 免费模型 free model {res.get('_routed') or res.get('model')} · in {u.get('prompt_tokens', '?')} / "
            f"out {u.get('completion_tokens', '?')} tokens · {res.get('_seconds')} s]")


def guard_private(force: bool):
    marker = private_marker()
    if marker and not force:
        raise SystemExit(t(f"这个项目标了 {marker}：不外包给免费模型（它们可能拿提示词训练）。确实要外包加 --force", f"This project is marked {marker}: not sent to free models (they may train on prompts). Add --force if you really want to"))


def read_files(paths: list) -> str:
    parts, total = [], 0
    for p in paths:
        if SKIP_FILES.search(p.replace("\\", "/")):
            parts.append(f"=== {p} ===\n[skipped: may contain secrets]\n")
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            parts.append(f"=== {p} ===\n[unreadable: {e}]\n")
            continue
        room = MAX_INPUT_CHARS - total
        if room <= 0:
            parts.append(f"=== {p} ===\n[over the {MAX_INPUT_CHARS}-char limit, not sent]\n")
            continue
        if len(text) > room:
            text = text[:room] + f"\n[... {len(text) - room} more chars truncated]"
        total += len(text)
        numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(text.splitlines(), 1))
        parts.append(f"=== {p} ===\n{numbered}\n")
    return redact("\n".join(parts))


ASK_SYSTEM = ("You are a careful assistant helping a senior coding agent. Answer the question using ONLY the provided "
              "files. Be concise and concrete: cite file paths with line numbers (path:line) for every claim, quote "
              "identifiers and error messages exactly, and say 'not found in the files' instead of guessing. "
              "No preamble. Reply in the language of the question.")

SUM_SYSTEM = ("You compress command output for a senior coding agent that cannot afford to read it all. Keep ONLY what "
              "it needs to act: the overall result (passed/failed, counts), every distinct error or failure with its "
              "exact message, and for each one BOTH the outermost frame in project code (e.g. the test) AND the "
              "deepest frame in project code (where it actually broke), each as file:line, and key numbers. Quote "
              "error text verbatim; never invent or paraphrase file names or line numbers. Collapse repeated lines "
              "into one with a count. Drop progress bars, download logs, and passing items. Do NOT restate the "
              "command, exit code or duration (they are shown separately) and never state a number that is not in "
              "the output. If the output is already compact, keep only the parts that matter. At most 30 lines, "
              "plain text, no preamble.")


ROUTES = os.path.join(DATA_DIR, "routes.json")


def task_type_of(command: str) -> str:
    """命令属于能力画像里的哪类任务"""
    if re.search(r"(^|[;&|]\s*|\s)git\s", " " + command):
        return "git"
    if re.search(r"(^|[;&|]\s*|\s)(rg|grep|find|findstr|Select-String)\b", " " + command):
        return "search"
    return "build"


def chain_for(task_type: str) -> list:
    """能力画像生成的路由表（bench/profile_report.py --routes）。没有表就用 auto"""
    try:
        with open(ROUTES, encoding="utf-8") as f:
            routes = json.load(f).get("routes", {})
    except (OSError, ValueError):
        return ["auto"]
    return routes.get(task_type, ["auto"])


def speed_key(attempt) -> str:
    m, b = attempt
    return f"api:{b['id']}:{b['model']}" if b else f"freellmapi:{m}"


def chat_routed(system: str, user: str, model: str, task_type: str, max_tokens: int, think: bool, timeout: int,
                attempts: int, backend: dict = None) -> dict:
    """model="route" 时试路由表里的模型和用户用 iu api add 加的那几家（资格测试没过这类任务的不用），所有模型共用 timeout 秒。
    顺序按 iu/speed.py 的测速：最近失败的放最后，其余快的先试（没测过就保持路由表从强到弱的顺序）。
    指定了具体模型就只在 FreeLLMAPI 上试它；给了 backend 就只用那一家（资格测试用）"""
    if backend:
        return chat(system, user, max_tokens=max_tokens, think=think, timeout=timeout, attempts=attempts, backend=backend)
    if model != "route":
        return chat(system, user, model=model, max_tokens=max_tokens, think=think, timeout=timeout, attempts=attempts)
    from . import speed
    from .providers import allowed, ready
    tries = [(m, None) for m in (chain_for(task_type) if setting("FREELLMAPI_KEY") else [])]
    tries += [(b["model"], b) for b in ready() if allowed(b, task_type)]
    tries = speed.rank(tries, key_of=speed_key)
    speed.kick_if_stale()  # 超过一小时没测过：后台并行测一遍，这次调用不等它
    if not tries:
        raise SystemExit(t("没有可用的免费模型：用 iu api add 加一个（比如 iu api add gemini），或者配好 FreeLLMAPI",
                           "No free model configured: add one with iu api add (e.g. iu api add gemini), or set up FreeLLMAPI"))
    deadline, last = time.time() + timeout, None
    for m, b in tries:
        left = deadline - time.time()
        if left < 3:
            break
        try:
            res = chat(system, user, model=m, max_tokens=max_tokens, think=think, timeout=int(left), attempts=1, backend=b)
        except SystemExit as e:
            last = e
            if left >= 10:  # 只剩几秒还超时，不算这个模型慢
                speed.record(speed_key((m, b)), False)
            continue
        speed.record(speed_key((m, b)), True, res.get("_seconds"))
        res["_chain"] = [x for x, _ in tries]
        return res
    raise SystemExit(t(f"免费模型都没成功（{[x for x, _ in tries]}）：{str(last)[:150] if last else '超时'}",
                       f"No free model succeeded ({[x for x, _ in tries]}): {str(last)[:150] if last else 'timed out'}"))


def ask_text(question: str, files_text: str, model="route", max_tokens=1200, think=False, timeout: int = 180,
             attempts: int = 3, task_type: str = "file_qa", backend: dict = None):
    """files_text 是 read_files 的格式（带行号）。返回 (回答, 原始响应, 发送字符数)"""
    user = redact(f"Files:\n{files_text}\n\nQuestion: {question}" if files_text else question)
    res = chat_routed(ASK_SYSTEM, user, model, task_type, max_tokens, think, timeout, attempts, backend)
    return answer_text(res), res, len(user)


def summarize_text(command: str, exit_code: int, output: str, goal: str = "", model="route", max_tokens=1200,
                   think=False, timeout: int = 180, attempts: int = 3, task_type: str = None, backend: dict = None):
    """把一段命令输出交给免费模型压缩。goal = agent 想从输出里找什么（可选）。返回 (摘要, 原始响应, 发送字符数)"""
    body = output
    if len(body) > MAX_INPUT_CHARS:  # 太长就保留开头和结尾，错误通常在这两处
        half = MAX_INPUT_CHARS // 2
        body = body[:half] + f"\n[... {len(output) - 2 * half} chars omitted ...]\n" + body[-half:]
    body = redact(body)
    goal_line = f"What the agent is looking for: {goal}\n" if goal else ""
    res = chat_routed(SUM_SYSTEM, f"Command: {command}\nExit code: {exit_code}\n{goal_line}\nOutput:\n{body}",
                      model, task_type or task_type_of(command), max_tokens, think, timeout, attempts, backend)
    return answer_text(res), res, len(body)


REF = re.compile(r"([\w\-.]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|cs|cpp|c|h|rb|php|swift|json|yaml|yml|toml))"
                 r"[\"']?\s*(?::|\(|,?\s*line\s+)(\d+)")
CRITICAL = re.compile(r"(\w*Error\b:?|\w*Exception\b|AssertionError|\bFAIL\b|error TS\d{4}|ERR!|panic:|Traceback)")


def guard_summary(summary: str, raw: str):
    """免费模型偶尔会路由到弱模型：编造位置、改写错误原文、漏掉失败。交回 agent 之前检查一遍。
    返回 (最终文字, 问题说明或 None)。说明不为空时调用方改用规则过滤。"""
    fake = sorted({f"{n}:{l}" for n, l in REF.findall(summary)
                   if not re.search(re.escape(n) + r"[\"']?\s*(?::|\(|,?\s*line\s+)" + l + r"\b", raw)})
    if fake:
        return summary, t("摘要里有原始输出中不存在的位置：", "summary cites locations not in the output: ") + ", ".join(fake[:5])
    norm_sum = re.sub(r"\s+", " ", summary.lower())
    missing, seen = [], set()
    for line in raw.splitlines():
        s = line.strip()
        if not s or not CRITICAL.search(s) or len(s) > 300 or s.startswith("Traceback (most recent call last)"):
            continue
        key = re.sub(r"\s+", " ", s.lower())[:120]
        if key in seen:
            continue
        seen.add(key)
        if key[:80] not in norm_sum:
            missing.append(s)
        if len(missing) >= 12:
            break
    if missing:
        summary += "\n原文关键行 / key lines from the raw output (added back verbatim):\n" + "\n".join(missing)
    return summary, None


def run_shell(command: list):
    """单个字符串当 shell 命令跑（支持管道）。Windows 上 Claude Code 的 Bash 是 Git Bash 语法，所以优先用 bash，不用 cmd"""
    if len(command) > 1:
        return subprocess.run(command, capture_output=True)
    bash = shutil.which("bash")
    if bash:
        return subprocess.run([bash, "-c", command[0]], capture_output=True)
    return subprocess.run(command[0], shell=True, capture_output=True)


def cmd_ask(args):
    guard_private(args.force)
    files = read_files(args.files) if args.files else ""
    text, res, sent = ask_text(args.question, files, model=args.model, max_tokens=args.max_out, think=args.think,
                               task_type=args.type)
    print(text)
    print(footer(res), file=sys.stderr)
    log_call("ask", res, sent, {"files": len(args.files or [])})


def save_raw(text: str):
    """原始输出存到 ~/.infiniteusage/raw；写不了（比如在 Codex 的 workspace-write 沙箱里）就退到临时目录，再不行返回 None"""
    import tempfile
    name = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}.log"
    for d in (os.path.join(DATA_DIR, "raw"), os.path.join(tempfile.gettempdir(), "iu_raw")):
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return path
        except OSError:
            continue
    return None


def cmd_sum(args):
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        raise SystemExit(t("用法：iu sum -- <命令...>", "Usage: iu sum -- <command...>"))
    t0 = time.time()
    proc = run_shell(command)
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    took = round(time.time() - t0, 1)
    head = f"$ {' '.join(command)}  →  exit {proc.returncode}, {took} s, {len(out)} chars"
    if len(out) <= RAW_SHOW_LIMIT:
        print(head)
        print(out.rstrip())
        sys.exit(proc.returncode)
    raw = save_raw(head + "\n" + out)
    tail = (f"[完整输出 / full output: {raw} — read it by line range for details, do not re-run the command]" if raw else
            "[完整输出没能存下来 / full output could not be saved here; for details re-run only the failing part]")
    reason = None
    if private_marker() and not args.force:
        reason = "private project, not sent out"
    else:
        try:
            text, res, sent = summarize_text(" ".join(command), proc.returncode, out, goal=args.goal or "",
                                             model=args.model, max_tokens=args.max_out, think=args.think,
                                             timeout=args.budget, attempts=1)
        except SystemExit as e:  # 免费模型失败、限流、超时：兜底用规则过滤，绝不卡住
            reason = f"free model unavailable ({str(e)[:120]})"
        else:
            text, problem = guard_summary(text, out)
            if problem:
                reason = f"{problem}（{res.get('_routed')}）"
    if reason:
        print(head + f"\n[规则过滤 / rule filter: {reason}]")
        print(det_filter(out, args.goal or ""))
        print(tail)
        log_call("sum-det", {"_routed": "det"}, len(out), {"exit": proc.returncode, "raw_chars": len(out),
                                                           "reason": reason})
        sys.exit(proc.returncode)
    print(head)
    print(text)
    print(tail)
    print(footer(res), file=sys.stderr)
    log_call("sum", res, sent, {"exit": proc.returncode, "raw_chars": len(out)})
    sys.exit(proc.returncode)
