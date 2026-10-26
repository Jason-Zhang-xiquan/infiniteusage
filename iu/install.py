"""iu install / iu uninstall：程序复制到 ~/.infiniteusage/app，skill 装进各家 coding agent（iu/universal.py）；
有 Claude Code 的话再改它的设置（下面三样都只在 Claude Code 上实测过）。

装这几样东西，都有证据（EVIDENCE.md）：
  1. 提前压缩：env CLAUDE_CODE_AUTO_COMPACT_WINDOW。实测触发点约为窗口的 67%，所以「约 300K 压缩」= 窗口 450K。
     压缩点第一次装时在本机日志上模拟选（iu/compactsim.py，300K~600K；会话很少长到这么长就不改）。
     你的真实日志模拟：Windows 省 43~50%（悲观假设下仍省 43%）
  2. 外包长输出：PreToolUse hook，只在配置了免费模型（FreeLLMAPI 统一 key）时才装。实测 Sonnet -20%（测试/构建多的任务）
  3. 子 agent 先试免费 agent 写代码：PreToolUse hook（Agent 工具），只在找得到 OpenCode 时才装。
     免费 agent 独立修 bug：单个 12/12、4 个一组 3/3（docs/recursion_free_agent.md）
  4. Codex 提前压缩：~/.codex/config.toml 顶层 model_auto_compact_token_limit = 150000（默认约在窗口的 90%）。
     你的真实 Codex 日志模拟：Windows 省 21%（悲观假设下 6%）；bench/codex_compact_sim.py
  5. 压缩后、新会话开场把项目 NOTES.md 塞进上下文：SessionStart hook（matcher 为 compact 和 startup 两条），
     总是装（--no-notes 不装）。「失忆鹈鹕」：频繁压缩后不记笔记 0/16、记笔记 + hook 16/16
     （docs/visual_compaction_test_plan.md）；跨会话、新会话不提上次：只有笔记 1/16、开场 hook 16/16
     （docs/code_map_feasibility.md）

做法：先显示要改什么；改之前备份 settings.json / config.toml；把原来的值记在 ~/.infiniteusage/install.json，卸载时精确还原。
hook 命令一律以 `|| true` 结尾（hook 坏了也不会拦住 Bash）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

from .store import DATA_DIR
from .i18n import t

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "run_iu.py").replace("\\", "/")
STATE = os.path.join(DATA_DIR, "install.json")
TRIGGER_RATIO = 0.67  # 实测：压缩在窗口的约 67% 处触发（bench/ab_sprint.py 的 W 组，100K 窗口在 64~71K 压缩）
ENV_KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
MARK = "run_iu.py"  # 配合 is_ours()：命令里同时有 run_iu.py 和 hook-pre / hook-agent / hook-compact 才算我们装的
OUR_EVENTS = ("PreToolUse", "SessionStart")


def is_ours(cmd: str) -> bool:
    cmd = cmd or ""
    return MARK in cmd and ("hook-pre" in cmd or "hook-agent" in cmd or "hook-compact" in cmd)


def strip_ours(settings: dict):
    """把我们装的 hook 从各事件里删掉；删空了的事件和 hooks 本身也去掉"""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for ev in OUR_EVENTS:
        if ev not in hooks:
            continue
        hooks[ev] = [e for e in hooks[ev] or [] if not any(is_ours(h.get("command")) for h in e.get("hooks", []))]
        if not hooks[ev]:
            hooks.pop(ev)
    if not hooks:
        settings.pop("hooks")


def claude_settings_path() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "settings.json")


def free_backend_ready() -> bool:
    """有 FreeLLMAPI 的 key，或者用 iu api add 加过、配好 key 的免费 API"""
    from .offload import setting
    from .providers import ready
    return bool(setting("FREELLMAPI_KEY") or ready())


def hook_entry(python: str) -> dict:
    return {"matcher": "Bash", "hooks": [{"type": "command", "command": f'"{python}" "{RUNNER}" hook-pre || true',
                                          "timeout": 10}]}


def agent_hook_entry(python: str) -> dict:
    return {"matcher": "Agent|Task", "hooks": [{"type": "command",
                                                "command": f'"{python}" "{RUNNER}" hook-agent || true', "timeout": 10}]}


def compact_hook_entry(python: str, matcher: str = "compact") -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command",
                                           "command": f'"{python}" "{RUNNER}" hook-compact || true', "timeout": 10}]}


def delegate_ready() -> bool:
    from .explore import opencode_bin
    try:
        opencode_bin()
        return True
    except SystemExit:
        return False


SIM_DAYS = 14


def pick_compact_at(arg, prev: dict):
    """--compact-at：数字就照用；auto 就在本机日志上模拟；不填时重装沿用上次的，第一次装就模拟。
    返回 (压缩点（0 = 不改）, 模拟结果或 None)"""
    if arg not in (None, "auto"):
        try:
            return int(arg), None
        except ValueError:
            raise SystemExit(t("--compact-at 要是数字或 auto", "--compact-at must be a number or auto"))
    if arg is None and prev.get("settings") and not prev.get("uninstalled_at") and "compact_at" in prev:
        at = prev["compact_at"]
        print(t(f"  · 压缩点沿用上次的：{f'约 {at // 1000}K' if at else '不改'}（重新模拟：iu install --compact-at auto）",
                f"  · keeping the previous compaction point: {f'~{at // 1000}K' if at else 'unchanged'} "
                f"(re-simulate: iu install --compact-at auto)"))
        return at, prev.get("compact_sim")
    from . import compactsim
    # 装过的话只看装之前的日志：装之后的会话已经被压缩过，看不出不压缩时会长多长
    until = (prev.get("compact_at") and prev.get("installed_at")) or time.time()
    since = until - SIM_DAYS * 86400
    print(t(f"  · 在你最近 {SIM_DAYS} 天的 Claude Code 日志上模拟不同压缩点（只读本机日志，不花额度）…",
            f"  · simulating compaction points on your last {SIM_DAYS} days of Claude Code logs (local, free)…"))
    r = compactsim.choose(compactsim.load_sessions(since), compactsim.iso(since), compactsim.iso(until))
    for row in r["rows"]:
        print(t(f"      {row['compact_at'] // 1000}K 压缩：省 {row['saving']:.0%}（悲观 {row['pessimistic']:.0%}），压缩 {row['compactions']} 次",
                f"      compact at {row['compact_at'] // 1000}K: saves {row['saving']:.0%} (pessimistic {row['pessimistic']:.0%}), "
                f"{row['compactions']} compactions"))
    msg = {
        "few": t(f"日志太少（{r['requests']} 次请求），先用默认约 {r['compact_at'] // 1000}K；过几天可以 iu install --compact-at auto 重新模拟",
                 f"too few logs ({r['requests']} requests); using the default ~{r['compact_at'] // 1000}K for now. "
                 f"Re-simulate in a few days: iu install --compact-at auto"),
        "short": t(f"最近没有会话长到 {compactsim.CANDIDATES[0] // 1000}K（最长 {r['max_ctx'] / 1000:.0f}K），提前压缩帮不上忙，不改压缩设置",
                   f"no session reached {compactsim.CANDIDATES[0] // 1000}K lately (longest {r['max_ctx'] / 1000:.0f}K); "
                   f"early compaction would not help, compaction settings left alone"),
        "no_gain": t(f"模拟省不到 {compactsim.MIN_SAVING:.0%}，不改压缩设置", f"simulated saving under {compactsim.MIN_SAVING:.0%}; compaction settings left alone"),
        "sim": t(f"选约 {r['compact_at'] // 1000}K（省得最多的几个里压缩最少的）",
                 f"picked ~{r['compact_at'] // 1000}K (the fewest compactions among the best savings)"),
    }[r["reason"]]
    print("  · " + msg)
    return r["compact_at"], {k: r[k] for k in ("reason", "rows", "requests", "max_ctx")}


def plan_changes(settings: dict, compact_at: int, offload: bool, python: str, delegate: bool = False,
                 notes_hook: bool = False):
    """返回 (新的 settings, 改动说明列表)"""
    new = json.loads(json.dumps(settings))
    strip_ours(new)  # 重装：先删掉上次装的，再按这次的选择加
    notes = []
    if compact_at:
        window = int(round(compact_at / TRIGGER_RATIO / 1000)) * 1000
        window = max(100_000, min(window, 1_000_000))
        env = new.setdefault("env", {})
        old = env.get(ENV_KEY)
        env[ENV_KEY] = str(window)
        k = int(window * TRIGGER_RATIO / 1000)
        notes.append(f"env.{ENV_KEY}: {old!r} → '{window}'" + t(f"（约在上下文 {k}K 时压缩；默认要到约 967K 才压缩）",
                                                                   f" (compacts at ~{k}K context; the default is ~967K)"))
    if offload:
        new.setdefault("hooks", {}).setdefault("PreToolUse", []).append(hook_entry(python))
        notes.append("hooks.PreToolUse += Bash → iu hook-pre" + t("（长输出的测试/构建/搜索/git 命令交给免费模型压缩，"
                                                                  "原始输出存文件；出错原样放行）",
                                                                  " (long test/build/search/git output is summarized by a free "
                                                                  "model, raw output saved to a file; errors pass through)"))
    if delegate:
        new.setdefault("hooks", {}).setdefault("PreToolUse", []).append(agent_hook_entry(python))
        notes.append("hooks.PreToolUse += Agent → iu hook-agent" + t("（通用子 agent 改代码前先试免费 agent，"
                                                                     "验证命令通过才保留，不过就还原；.iu-private 项目不外包）",
                                                                     " (general sub-agents try a free agent first for code "
                                                                     "changes; kept only if the check passes; .iu-private "
                                                                     "projects are never sent out)"))
    if notes_hook:
        new.setdefault("hooks", {}).setdefault("SessionStart", []).extend(
            [compact_hook_entry(python), compact_hook_entry(python, "startup")])
        notes.append("hooks.SessionStart += compact, startup → iu hook-compact" + t(
            "（压缩一结束、新会话一开始，把项目根目录的 NOTES.md 原文塞进上下文；没有这个文件就什么都不做）",
            " (right after compaction and at the start of a new session, puts the project's NOTES.md into context; "
            "does nothing if there is no such file)"))
    return new, notes


# Codex 提前压缩：~/.codex/config.toml 顶层的 model_auto_compact_token_limit。默认不设时约在上下文窗口的 90% 压缩
# （272K 窗口的模型约 232K，gpt-6.1-sol 约 427K）。150K 来自 bench/codex_compact_sim.py 在你的真实 Codex 日志上的模拟。
# config.toml 也归 Codex 桌面版管（会写 [projects] 等），所以只按行改这一行，别的字节一律不动。
CODEX_KEY = "model_auto_compact_token_limit"
CODEX_COMPACT_AT = 150_000


def codex_config_path():
    from . import universal
    d = os.environ.get("CODEX_HOME") or os.path.join(universal.home(), ".codex")
    return os.path.join(d, "config.toml") if os.path.isdir(d) else None


def _codex_lines(text: str):
    """返回 (行列表（带换行符）, 顶层区结束的行号, 顶层 CODEX_KEY 所在行号或 None)"""
    lines = text.splitlines(keepends=True)
    end = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), len(lines))
    at = next((i for i in range(end) if lines[i].split("=")[0].strip() == CODEX_KEY and "=" in lines[i]), None)
    return lines, end, at


def _toml_ok(text: str) -> bool:
    try:
        import tomllib
    except ImportError:  # Python 3.10 及以下没有 tomllib，不校验
        return True
    try:
        tomllib.loads(text)
        return True
    except ValueError:
        return False


def install_codex_compact(limit: int, dry_run: bool = False, prev: dict = None):
    """把 config.toml 顶层的 model_auto_compact_token_limit 设成 limit。返回装机记录，没有 Codex 或文件坏了返回 None"""
    path = codex_config_path()
    if not path:
        return None
    exists = os.path.exists(path)
    raw = open(path, "rb").read() if exists else b""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not _toml_ok(text):
        return None  # 用户的文件坏了：不碰
    lines, end, at = _codex_lines(text)
    nl = "\r\n" if "\r\n" in text else "\n"
    line = f"{CODEX_KEY} = {int(limit)}{nl}"
    entry = {"path": path, "value": int(limit), "created": not exists, "previous": None, "inserted_blank": False}
    if prev and prev.get("path") == path:  # 重装：原值沿用第一次的（不然卸载会「还原」成我们自己的值）
        entry.update(created=prev.get("created", False), previous=prev.get("previous"),
                     inserted_blank=prev.get("inserted_blank", False))
        if at is not None and lines[at] == line:
            entry["backup"] = prev.get("backup")
            return entry
    elif at is not None:
        entry["previous"] = lines[at]
    if at is not None:
        lines[at] = line
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += nl
        add = [line]
        if end < len(lines):  # 后面紧跟着 [表头]：空一行，和 Codex 自己写的格式一样
            add.append(nl)
            entry["inserted_blank"] = True
        lines[end:end] = add
    new = "".join(lines)
    if not _toml_ok(new):
        return None
    entry["backup"] = None
    if not dry_run:
        if exists:
            entry["backup"] = path + f".iu-backup-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(path, entry["backup"])
        open(path, "wb").write(new.encode("utf-8"))
    if prev and prev.get("path") == path and prev.get("backup"):
        entry["backup"] = prev["backup"]
    return entry


def uninstall_codex_compact(entry: dict) -> str:
    """还原成装之前的那一行（原来没有就删掉）。用户后来自己改了这个值就不动。返回说明"""
    path = (entry or {}).get("path")
    if not path or not os.path.exists(path):
        return ""
    text = open(path, "rb").read().decode("utf-8", errors="replace")
    lines, end, at = _codex_lines(text)
    if at is None:
        return ""
    if lines[at].split("=", 1)[1].split("#")[0].strip() != str(entry.get("value")):
        return t(f"Codex：{CODEX_KEY} 已被改成别的值，没动它（{path}）", f"Codex: {CODEX_KEY} was changed by someone else; left alone ({path})")
    if entry.get("previous"):
        lines[at] = entry["previous"]
    else:
        del lines[at]
        if entry.get("inserted_blank") and at < len(lines) and not lines[at].strip():
            del lines[at]
    new = "".join(lines)
    if entry.get("created") and not new.strip():
        os.remove(path)
    else:
        open(path, "wb").write(new.encode("utf-8"))
    return t(f"Codex：{CODEX_KEY} 已还原（{path}）", f"Codex: {CODEX_KEY} restored ({path})")


def load_state() -> dict:
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cmd_install(args):
    if args.agent not in ("all", "claude-code", "skills"):
        raise SystemExit(t("--agent 只能是 all（默认：skill 装进所有 coding agent，有 Claude Code 再改它的设置）、"
                           "claude-code、skills（只装 skill）",
                           "--agent must be all (default: skill for every coding agent, plus Claude Code settings if "
                           "present), claude-code, or skills (skill only)"))
    from . import universal
    global RUNNER
    RUNNER = universal.copy_app(dry_run=args.dry_run)  # hook 和 skill 都指向固定位置的程序，仓库挪了也不影响
    skills = universal.install_skills(RUNNER, dry_run=args.dry_run)
    print(t("程序：", "Program: ") + os.path.dirname(RUNNER))
    print(t("skill（装进这些目录，所有读它们的 coding agent 都能用）：", "Skill (installed in these folders; every coding agent that reads them can use it):"))
    for d in skills:
        print("  · " + d)
    prev = load_state()
    rules = [] if args.no_rules else universal.install_rules(
        RUNNER, dry_run=args.dry_run, backup_dir=os.path.join(DATA_DIR, "backups"),
        prev=None if prev.get("uninstalled_at") else prev.get("rules"))
    if rules:  # 只放 skill，别家宿主不会自己去用（bench/host_skill.py 实测 0/6），要在全局说明文件里加一条常驻规则
        print(t("常驻规则（带 infiniteusage 标记，iu uninstall 会精确删掉）：", "Standing rule (marked infiniteusage; iu uninstall removes exactly that block):"))
        for r in rules:
            print("  · " + r["path"] + (t("（新建）", " (new)") if r["created"] else ""))
    no_notes = getattr(args, "no_notes", False)
    codex_notes = None if no_notes else universal.install_codex_notes(
        RUNNER, dry_run=args.dry_run, prev=None if prev.get("uninstalled_at") else prev.get("codex_notes"))
    if codex_notes:
        print(f"Codex: {codex_notes['path']} += SessionStart (startup, compact) → iu hook-compact" + t(
            "（新会话开场、压缩之后把 NOTES.md 放进上下文）。Codex 要先信任这个 hook 才会运行：在 Codex 里输入 /hooks 看一遍再信任",
            " (puts NOTES.md into context at session start and after compaction). Codex runs it only after you trust "
            "it: type /hooks in Codex, review, then trust"))
    elif not no_notes and not universal.codex_hook_supported() and os.path.isdir(os.path.join(universal.home(), ".codex")):
        print(t("Codex：Windows 上先不装 NOTES.md hook（只在 Mac / Linux 上实测过）；常驻规则里已经让它新会话先读 NOTES.md", "Codex: the NOTES.md hook is not installed on Windows (tested on Mac / Linux only); the standing rule already tells it to read NOTES.md at session start"))
    prev_cp = None if prev.get("uninstalled_at") else prev.get("codex_pre")
    want_pre = getattr(args, "codex_offload", False)
    codex_pre = (universal.install_codex_pre(RUNNER, dry_run=args.dry_run, prev=prev_cp)
                 if want_pre and free_backend_ready() else None)
    if codex_pre:
        print(f"Codex: {codex_pre['path']} += PreToolUse (Bash) → iu hook-pre --codex" + t(
            "（试验：长输出的测试/构建/搜索/git 命令交给免费模型压缩；同样要先在 /hooks 里信任）",
            " (experimental: long test/build/search/git output summarized by a free model; trust it in /hooks too)"))
    elif want_pre and universal.codex_hooks_path():
        print(t("Codex：外包长输出没装：没有免费模型：没找到 FREELLMAPI_KEY，也没有用 iu api add 加过免费 API", "Codex: long-output offload not installed: no free model: no FREELLMAPI_KEY and no free API added with iu api add"))
    if not codex_pre and prev_cp and not args.dry_run:  # 重装时不要了：把上次装的删掉
        universal.uninstall_codex_pre(prev_cp)
    if prev.get("opencode_notes") and not prev.get("uninstalled_at") and not args.dry_run:  # 旧版装过的 OpenCode 插件
        universal.uninstall_opencode_notes(prev["opencode_notes"])
    codex_at = 0 if args.no_compact else getattr(args, "codex_compact_at", CODEX_COMPACT_AT)
    prev_cc = None if prev.get("uninstalled_at") else prev.get("codex_compact")
    codex_compact = install_codex_compact(codex_at, dry_run=args.dry_run, prev=prev_cc) if codex_at else None
    if codex_compact:
        old_line = (codex_compact.get("previous") or "").strip()
        print(t(f"Codex：{codex_compact['path']} 顶层 {CODEX_KEY} = {codex_at}（原来：{old_line or '没设，默认约在窗口的 90% 压缩'}；"
                f"约在上下文 {codex_at // 1000}K 时压缩）",
                f"Codex: {codex_compact['path']} top-level {CODEX_KEY} = {codex_at} (was: {old_line or 'unset, default ~90% of the window'}; "
                f"compacts at ~{codex_at // 1000}K context)"))
    elif prev_cc and not args.dry_run:  # 重装时不要了：把上次装的还原
        print(uninstall_codex_compact(prev_cc))
    extra_state = {"codex_notes": codex_notes, "opencode_notes": None, "codex_compact": codex_compact,
                   "codex_pre": codex_pre}
    claude_dir = os.path.dirname(claude_settings_path())
    if args.agent == "skills" or (args.agent == "all" and not os.path.isdir(claude_dir)):
        if args.dry_run:
            print(t("（预览，没有改任何东西。去掉 --dry-run 才会真的写入）", "(Preview only, nothing changed. Drop --dry-run to apply)"))
            return
        state = {**prev, "skills": skills, "app": RUNNER, "rules": rules, **extra_state}
        state.pop("uninstalled_at", None)
        state.setdefault("installed_at", time.time())
        os.makedirs(DATA_DIR, exist_ok=True)
        json.dump(state, open(STATE, "w", encoding="utf-8"), indent=1)
        print(t("已装好 skill。新开的会话生效", "Skill installed. Takes effect in new sessions"))
        return
    path = claude_settings_path()
    try:
        settings = json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        settings = {}
    offload = not args.no_offload and free_backend_ready()
    delegate = not args.no_delegate and delegate_ready()
    python = sys.executable.replace("\\", "/")
    notes_hook = not getattr(args, "no_notes", False)
    print(t("Claude Code 设置文件：", "Claude Code settings file: ") + path)
    compact_at, compact_sim = (0, None) if args.no_compact else pick_compact_at(args.compact_at, prev)
    new, notes = plan_changes(settings, compact_at, offload, python, delegate, notes_hook)
    if not compact_at and prev.get("settings") and not prev.get("uninstalled_at") and prev.get("compact_at"):
        env = new.get("env") or {}  # 上次装时改过压缩设置，这次不改了：还原成装之前的值
        if prev.get("previous_env") is None:
            env.pop(ENV_KEY, None)
        else:
            env[ENV_KEY] = prev["previous_env"]
        if not env:
            new.pop("env", None)
        notes.append(t(f"env.{ENV_KEY}：还原成装之前的值", f"env.{ENV_KEY}: restored to the value before install"))
    for n in notes:
        print("  · " + n)
    if not args.no_offload and not offload:
        print(t("  · 外包长输出没装：没有免费模型：没找到 FREELLMAPI_KEY，也没有用 iu api add 加过免费 API", "  · long-output offload not installed: no free model: no FREELLMAPI_KEY and no free API added with iu api add"))
    if not args.no_delegate and not delegate:
        print(t("  · 子 agent 先试免费 agent 没装：找不到 OpenCode（在 ~/iu_tools/opencode 里 npm i opencode-ai）", "  · sub-agent delegation not installed: OpenCode not found (npm i opencode-ai in ~/iu_tools/opencode)"))
    if args.dry_run:
        print(t("（预览，没有改任何东西。去掉 --dry-run 才会真的写入）", "(Preview only, nothing changed. Drop --dry-run to apply)"))
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = None
    if os.path.exists(path):
        backup = path + f".iu-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
    json.dump(new, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if offload or delegate:  # hook 对所有项目生效（标了 .iu-private 的除外）
        from .hook import PILOT_FILE
        os.makedirs(DATA_DIR, exist_ok=True)
        open(PILOT_FILE, "w", encoding="utf-8").write("*\n")
    os.makedirs(DATA_DIR, exist_ok=True)
    old = load_state()
    state = {"agent": "claude-code", "installed_at": time.time(), "settings": path, "backup": backup,
             "previous_env": (settings.get("env") or {}).get(ENV_KEY), "compact_at": compact_at, "compact_sim": compact_sim,
             "offload": offload, "delegate": delegate, "notes_hook": notes_hook, "skills": skills, "app": RUNNER, "rules": rules,
             **extra_state}
    if old.get("settings") and not old.get("uninstalled_at"):  # 重装：原值和装机时间沿用第一次的（不然卸载会「还原」成我们自己的值）
        state.update(previous_env=old.get("previous_env"), installed_at=old.get("installed_at", state["installed_at"]),
                     backup=old.get("backup") or backup, updated_at=time.time())
    json.dump(state, open(STATE, "w", encoding="utf-8"), indent=1)
    print(t(f"已写入（备份：{backup or '原来没有这个文件'}）。新开的 Claude Code 会话生效。之后用 `iu compare` 看装之前和装之后的对比。",
            f"Written (backup: {backup or 'there was no file before'}). Takes effect in new Claude Code sessions. "
            f"Later, run `iu compare` to see before vs after."))


def cmd_uninstall(args):
    from . import universal
    path = claude_settings_path()
    state = load_state()
    if not args.dry_run:
        universal.uninstall_skills(state.get("skills"))
        universal.uninstall_rules(state.get("rules"))
        universal.uninstall_codex_pre(state.get("codex_pre"))  # 先删 PreToolUse，NOTES.md hook 新建的文件才删得空
        universal.uninstall_codex_notes(state.get("codex_notes"))
        universal.uninstall_opencode_notes(state.get("opencode_notes"))
        msg = uninstall_codex_compact(state.get("codex_compact"))
        if msg:
            print(msg)
    try:
        settings = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        if state:
            state["uninstalled_at"] = time.time()
            json.dump(state, open(STATE, "w", encoding="utf-8"), indent=1)
        print(t("已卸载 skill 和常驻规则（没有 Claude Code 设置要还原）", "Removed the skill and standing rules (no Claude Code settings to restore)"))
        return
    env = settings.get("env") or {}
    if ENV_KEY in env:
        prev = state.get("previous_env")
        if prev is None:
            env.pop(ENV_KEY)
        else:
            env[ENV_KEY] = prev
        if not env:
            settings.pop("env", None)
    strip_ours(settings)
    if args.dry_run:
        print(json.dumps(settings, ensure_ascii=False, indent=2))
        return
    shutil.copy2(path, path + f".iu-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    json.dump(settings, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if state:
        state["uninstalled_at"] = time.time()
        json.dump(state, open(STATE, "w", encoding="utf-8"), indent=1)
    print(t("已卸载：skill 和常驻规则删除、压缩设置还原、InfiniteUsage 的 hook 全部移除（改之前也备份了一份）", "Uninstalled: skill and standing rules removed, compaction settings restored, all InfiniteUsage hooks removed (a backup was made first)"))
