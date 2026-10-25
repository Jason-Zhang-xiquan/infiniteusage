"""Claude Code / Codex 的 PreToolUse hook（Codex 用 `hook-pre --codex`）：把白名单里「输出又长又杂」的 Bash 命令改写成 `iu sum -- <原命令>`。

只有同时满足才改写，否则什么都不输出（= 原样放行）：
  1. 工具是 Bash，且不是后台命令
  2. 命令命中白名单：测试、类型检查、lint、构建、git log/diff/show/blame/grep、rg/grep/find 搜索
     （不含 install 一类会改环境的命令；已经自己用 | head / | tail / | grep 截过的不管）
  3. 当前目录在试点名单 ~/.infiniteusage/pilot_projects.txt 里（每行一个目录）
  4. 当前目录往上没有 .iu-private 标记
不设置 permissionDecision：原来要确认的命令改写后照样要确认，hook 不替用户放行任何东西。
任何异常都原样放行。每次改写记一行到 ~/.infiniteusage/hook.jsonl。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time

from .offload import private_marker
from .store import DATA_DIR

PILOT_FILE = os.path.join(DATA_DIR, "pilot_projects.txt")
LOG = os.path.join(DATA_DIR, "hook.jsonl")
RUNNER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_iu.py").replace("\\", "/")

WHITELIST = re.compile(
    r"(^|[;&|]\s*|\s)("
    r"pytest|python3? -m (pytest|unittest)|npm (run )?(test|build|lint|typecheck)|pnpm (run )?(test|build|lint)|"
    r"yarn (test|build|lint)|npx (vitest|jest|tsc|eslint)|vitest|jest|tsc|eslint|ruff (check)?|mypy|flake8|"
    r"cargo (test|build|check|clippy)|go (test|build|vet)|dotnet (test|build)|make|gradle|mvn|"
    r"git (log|diff|show|blame|grep)|rg|grep -[a-zA-Z]*r[a-zA-Z]*|find \S+ .*-name"
    r")\b")
SELF_LIMITED = re.compile(r"\|\s*(head|tail|grep|wc|less|more)\b|--quiet|\s-q\b")
# v2：结尾是 | tail -N / | head -N 的命令也接管——agent 只是想看头尾几行，换成「完整输出的摘要」更短也不会把错误截掉
TRAILING = re.compile(r"^(?P<base>.+?)\s*\|\s*(?P<tool>tail|head)\s+(?:-n\s*)?-?(?P<n>\d+)\s*$")


def _canon(path: str) -> str:
    """真实路径（macOS 的 /tmp 其实是 /private/tmp），Windows 上不分大小写"""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def pilot_dirs(path: str = PILOT_FILE) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.strip() if ln.strip() == "*" else _canon(ln.strip())
                    for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        return []


def in_pilot(cwd: str, dirs: list) -> bool:
    if "*" in dirs:  # iu install 写的：所有项目（标了 .iu-private 的照样排除）
        return True
    c = _canon(cwd)
    return any(c == d or c.startswith(d + os.sep) for d in dirs)


def decide(payload: dict, dirs: list, is_private=lambda cwd: bool(private_marker(cwd)), python: str = None):
    """返回改写后的 tool_input，或 None（不改写）。纯函数，方便单元测试"""
    if payload.get("hook_event_name", "PreToolUse") != "PreToolUse" or payload.get("tool_name") != "Bash":
        return None
    inp = dict(payload.get("tool_input") or {})
    cmd = (inp.get("command") or "").strip()
    cwd = payload.get("cwd") or os.getcwd()
    if not cmd or inp.get("run_in_background") or "run_iu.py" in cmd or re.match(r"iu\s+sum\b", cmd):
        return None
    hint = ""
    m = TRAILING.match(cmd)
    if m:
        cmd = m.group("base").strip()
        which = "last" if m.group("tool") == "tail" else "first"
        hint = (f" (The agent piped this to `{m.group('tool')} -{m.group('n')}`, i.e. it mainly wanted the {which} "
                f"{m.group('n')} lines: make sure what those lines say, such as the final summary, is in your summary.)")
    if not WHITELIST.search(cmd) or SELF_LIMITED.search(cmd) or re.search(r"\b(install|add|publish|push)\b", cmd):
        return None
    if not in_pilot(cwd, dirs) or is_private(cwd):
        return None
    py = (python or sys.executable).replace("\\", "/")
    goal = ((inp.get("description") or "").strip()[:300] + hint).strip()
    parts = [shlex.quote(py), shlex.quote(RUNNER), "sum"]
    if goal:
        parts += ["--goal", shlex.quote(goal)]
    inp["command"] = " ".join(parts + ["--", shlex.quote(cmd)])
    if inp.get("timeout"):  # 摘要最多多花 30 秒，给原命令的超时留余量
        inp["timeout"] = min(int(inp["timeout"]) + 45000, 600000)
    return inp


def cmd_hook_pre(_args):
    if os.environ.get("IU_NO_OFFLOAD"):  # 临时关掉（A/B 实验、排查问题）
        return
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        new = decide(payload, pilot_dirs())
        if not new:
            return
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "cwd": payload.get("cwd"), "session": payload.get("session_id"),
                                    "original": (payload.get("tool_input") or {}).get("command")},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass
        sys.stdout.write(json.dumps(pre_output(new, getattr(_args, "codex", False))))
    except Exception:  # noqa: BLE001  hook 出任何错都原样放行
        return


def pre_output(new: dict, codex: bool = False) -> dict:
    """Claude Code：只给 updatedInput（不替用户放行）。
    Codex：updatedInput 必须配 permissionDecision "allow"，否则算 hook 失败、按原命令执行（codex-rs/hooks output_parser.rs，
    0.157.1 起）。Codex 的 allow 只表示「不拦」：改写发生在执行之前，审批和沙箱照样按改写后的命令走（审批是另一个
    PermissionRequest 事件）。Codex 只认 tool_input.command，别的字段原样带回也会被忽略"""
    out = {"hookEventName": "PreToolUse", "updatedInput": new}
    if codex:
        out["permissionDecision"] = "allow"
        out["updatedInput"] = {"command": new["command"]}
    return {"hookSpecificOutput": out}


# ---------------- 探索闸门：搜代码改走免费 agent ----------------
# 实测（2026-10-18）：只在系统提示里写「先用 iu explore」，Sonnet 会直接无视。所以用 hook 强制：
# 试点项目里，agent 自己搜代码（Grep / Glob / rg / grep -r / find -name / git grep）会被拒绝，并告诉它改用 iu explore。
# 只拒绝、不放行任何东西；Read 照常（改代码前要按行号核对原文）。同一会话被拒 GATE_ESCAPE 次以后放行，避免卡死。
SEARCH_BASH = re.compile(r"(^|[;&|]\s*|\s)(rg|ag|ack|git\s+grep|grep\s+-[a-zA-Z]*[rR][a-zA-Z]*|find\s+\S+.*-i?name)\b")
GATE_ESCAPE = 4


def gate_decide(payload: dict, dirs: list, is_private=lambda cwd: bool(private_marker(cwd)), python: str = None,
                denials: int = 0):
    """返回 PreToolUse 的输出 dict（拒绝），或 None（放行）"""
    tool = payload.get("tool_name")
    inp = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    cmd = inp.get("command") or ""
    is_search = tool in ("Grep", "Glob") or (tool == "Bash" and SEARCH_BASH.search(cmd) and "run_iu.py" not in cmd)
    if not is_search or denials >= GATE_ESCAPE or not in_pilot(cwd, dirs) or is_private(cwd):
        return None
    py = (python or sys.executable).replace("\\", "/")
    reason = (f"Code search is routed to the free exploration helper to save cost. Instead run: {py} {RUNNER} explore "
              f"\"<your specific question, e.g. where is X defined / how does Y work>\". It returns path:line citations; "
              f"then Read only those lines. If it cannot answer, try again with a more specific question.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def cmd_hook_explore_gate(_args):
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        sid = re.sub(r"[^\w-]", "", str(payload.get("session_id") or "nosession"))[:64]
        state = os.path.join(DATA_DIR, f"gate_{sid}.count")
        try:
            denials = int(open(state).read().strip() or 0)
        except (OSError, ValueError):
            denials = 0
        out = gate_decide(payload, pilot_dirs(), denials=denials)
        if not out:
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        open(state, "w").write(str(denials + 1))
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "cwd": payload.get("cwd"), "session": payload.get("session_id"),
                                "gate_denied": payload.get("tool_name"),
                                "original": (payload.get("tool_input") or {}).get("command")
                                or (payload.get("tool_input") or {}).get("pattern")}, ensure_ascii=False) + "\n")
        sys.stdout.write(json.dumps(out))
    except Exception:  # noqa: BLE001  出错就放行
        return


# ---------------- 子 agent 先试免费 agent：Opus 已经决定委派的改代码任务，让子 agent 先跑 iu delegate ----------------
# 真实日志（2026-10-19，Windows 7 天）：通用子 agent 里改文件的那些占子 agent 花费的 91%，平均每个 2.15M 等效 token。
# 免费 agent 做成了，子 agent 只需审查 diff（几次小上下文请求）；没做成，改动已还原，子 agent 照常自己做。
AGENT_TOOLS = ("Agent", "Task")
DELEGATE_TYPES = ("", "general-purpose", "claude")
AGENT_MARK = "[InfiniteUsage]"


def agent_prefix(python: str) -> str:
    run = f'"{python}" "{RUNNER}" delegate'
    return (f"{AGENT_MARK} If this task is a self-contained code change that a command can check (tests, a build, "
            "a lint run or a script), first hand it to the free coding agent once:\n"
            "1. Write the complete task below (plus any file paths and constraints you know) to a brief file in a "
            "temp/scratch directory.\n"
            f"2. Run with Bash (timeout 600000, not in the background): {run} --verify \"<command that proves it works>\" "
            "--brief-file <that file>\n"
            "3. If it prints 委托成功 / delegated OK: the change is in the working tree. Review the printed diff, read the changed "
            "files if needed, fix anything wrong or missing, run the check yourself once, then report.\n"
            "4. If it prints 委托失败 / delegation failed, or the task is research/design/non-code or cannot be checked by a command: "
            "do the task yourself as usual (any free-agent changes were already rolled back).\n"
            "--- task ---\n")


def agent_decide(payload: dict, dirs: list, is_private=lambda cwd: bool(private_marker(cwd)), python: str = None):
    """返回改写后的 Agent 调用参数，或 None（不改写）"""
    if payload.get("tool_name") not in AGENT_TOOLS:
        return None
    inp = dict(payload.get("tool_input") or {})
    prompt = inp.get("prompt") or ""
    cwd = payload.get("cwd") or os.getcwd()
    if (inp.get("subagent_type") or "") not in DELEGATE_TYPES or AGENT_MARK in prompt or not prompt:
        return None
    if not in_pilot(cwd, dirs) or is_private(cwd):
        return None
    inp["prompt"] = agent_prefix((python or sys.executable).replace("\\", "/")) + prompt
    return inp


def cmd_hook_agent(_args):
    if os.environ.get("IU_NO_DELEGATE"):  # 临时关掉（A/B 实验、排查问题）
        return
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        new = agent_decide(payload, pilot_dirs())
        if new:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": new}}))
    except Exception:  # noqa: BLE001  hook 出任何错都原样放行
        return


# ---------------- 压缩后把项目笔记塞回上下文：SessionStart hook，matcher 为 compact ----------------
# 实测（2026-10-24，docs/visual_compaction_test_plan.md）：Sonnet 压缩 7 次后把用户定的 16 条规格全忘了（0/16）；
# 让它记进 NOTES.md 的那组救回 12/16。笔记在文件里不会被压缩掉，但压缩后模型不一定想起来去读，所以压缩一结束就塞回去。
# rep 3 又发现：压缩常常正好发生在「用户刚给新要求、还没写进笔记」的那一轮，摘要把那条消息整个丢了。
# 所以再从会话记录里取出笔记最后保存之后用户发过的原话，一起塞回去，让它先补进笔记。
# Claude Code 把超过 10000 字的 additionalContext 换成「文件路径 + 前 2000 字预览」，所以自己截在 CONTEXT_MAX 以内。
NOTE_FILES = ("NOTES.md",)
CONTEXT_MAX = 9000
RECENT_MAX = 3000  # 笔记之后的用户原话最多占多少字
RECENT_EACH = 1500
RECENT_COUNT = 6


def project_root(cwd: str) -> str:
    """往上找 .git 所在的目录；找不到就是 cwd 本身"""
    d = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return os.path.abspath(cwd)
        d = up


def notes_file(cwd: str):
    """当前目录或项目根目录下的笔记文件，没有就 None"""
    for d in dict.fromkeys([os.path.abspath(cwd), project_root(cwd)]):
        for name in NOTE_FILES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


def _ts(value) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def user_messages_since(transcript: str, since: float) -> list:
    """会话记录里 since（时间戳）之后用户亲手发的消息原文，按时间顺序；不含工具结果、压缩摘要、斜杠命令"""
    out = []
    try:
        f = open(transcript, encoding="utf-8", errors="replace")
    except (OSError, TypeError):
        return out
    with f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") != "user" or d.get("isCompactSummary") or d.get("isMeta") or _ts(d.get("timestamp")) <= since:
                continue
            c = (d.get("message") or {}).get("content")
            if isinstance(c, list):
                c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            c = (c or "").strip()
            if c and not c.startswith(("<command-", "<local-command", "Caveat:")):
                out.append(c)
    return out


def compact_context(payload: dict):
    """返回要塞进上下文的文字，或 None（不是压缩后或新会话开场 / 没有笔记 / 笔记是空的）

    新会话开场（startup）也塞：「跨会话失忆鹈鹕」里新会话不提上次的规格时，只有 NOTES.md 1/16，开场塞进去 16/16
    （bench/pelican_cross.py --final plain，docs/code_map_feasibility.md）。resume / clear 不塞"""
    source = payload.get("source")
    if payload.get("hook_event_name", "SessionStart") != "SessionStart" or source not in ("compact", "startup"):
        return None
    path = notes_file(payload.get("cwd") or os.getcwd())
    if not path:
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not text.strip():
        return None
    if source == "startup":  # 新会话：记录是空的，没有「笔记之后的用户原话」可补
        head = (f"Loaded at session start by the InfiniteUsage SessionStart hook the user installed. This is the project "
                f"notes file {path}, where requirements and decisions from earlier sessions in this project are recorded. "
                "Follow the ones that apply to what the user asks; if the user now says something different, the user's "
                "latest words win. Keep the file up to date.\n\n--- NOTES.md ---\n")
        cut = f"\n…(truncated here: Read {path} for the rest)"
        if len(text) > CONTEXT_MAX - len(head):
            text = text[:max(CONTEXT_MAX - len(head) - len(cut), 0)] + cut
        return head + text
    transcript = payload.get("transcript_path")
    recent, used = [], 0
    for m in reversed(user_messages_since(transcript, os.path.getmtime(path))[-RECENT_COUNT:]):  # 新的优先
        m = m if len(m) <= RECENT_EACH else m[:RECENT_EACH] + " …"
        if used + len(m) > RECENT_MAX:
            break
        recent.insert(0, m)
        used += len(m)
    tail = ""
    if recent:
        tail = ("\n\nThe user sent these messages after NOTES.md was last saved (verbatim, oldest first, from this "
                f"session's transcript {transcript}). Requirements in them may be missing from both NOTES.md and the "
                "compaction summary: before continuing, add every requirement or change they contain to NOTES.md.\n"
                + "\n".join(f"--- user message {i + 1} ---\n{m}" for i, m in enumerate(recent)))
    head = (f"Restored after context compaction by the InfiniteUsage SessionStart hook the user installed. This is "
            f"the current content of the project notes file {path}, where requirements and decisions from this session "
            "are recorded. Keep following them and keep the file up to date.\n\n--- NOTES.md ---\n")
    room = CONTEXT_MAX - len(head) - len(tail)
    cut = f"\n…(truncated here: Read {path} for the rest)"
    if len(text) > room:
        text = text[:max(room - len(cut), 0)] + cut
    return head + text + tail


def cmd_hook_compact(_args):
    if os.environ.get("IU_NO_NOTES"):  # 临时关掉（A/B 实验、排查问题）
        return
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        if payload.get("source") == "startup":  # 新会话开场：免费模型超过一小时没测速就在后台测一遍，不等它
            from . import speed
            from .install import free_backend_ready
            if free_backend_ready():
                speed.kick_if_stale()
        ctx = compact_context(payload)
        if ctx:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                                 "additionalContext": ctx}}))
    except Exception:  # noqa: BLE001  hook 出任何错都不影响会话
        return
