"""通用安装：程序复制到固定位置 ~/.infiniteusage/app，skill 装进各家 coding agent 读的 skills 目录。

各家目录（docs/agents_landscape_2026-10.md，2026-10 调研）：
- ~/.agents/skills：Codex、Cursor、OpenCode、Kilo、Copilot、Amp、Goose、Windsurf/Devin 都读
- ~/.claude/skills：Claude Code 只读这里；OpenCode、Amp、Goose、VS Code 也读
- CodeBuddy、WorkBuddy、Trae、Trae CN、Qwen Code、Cline 用自己的目录：只有这台电脑上有那个 agent 时才装
SKILL.md 里的 {{IU}} 换成这台电脑上能直接运行的完整命令（python 路径 + run_iu.py 路径）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys

from .i18n import t

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_NAME = "infiniteusage"


def home() -> str:
    return os.path.expanduser("~")


def app_dir() -> str:
    from . import store
    return os.path.join(store.DATA_DIR, "app")


def targets() -> list:
    """[(说明, skills 目录)]；专属目录只在那个 agent 的配置目录存在时才装"""
    h = home()
    out = [(t("通用：", "shared: ") + "Codex / Cursor / OpenCode / Kilo / Copilot / Amp / Goose / Windsurf", os.path.join(h, ".agents", "skills")),
           ("Claude Code" + t("（OpenCode / Amp / Goose / VS Code 也读）", " (also read by OpenCode / Amp / Goose / VS Code)"), os.path.join(h, ".claude", "skills"))]
    for label, base in [("CodeBuddy", ".codebuddy"), ("WorkBuddy", ".workbuddy"), ("Trae", ".trae"),
                        ("Trae CN", ".trae-cn"), ("Qwen Code", ".qwen"), ("Cline", ".cline")]:
        if os.path.isdir(os.path.join(h, base)):
            out.append((label, os.path.join(h, base, "skills")))
    return out


def copy_app(dry_run: bool = False) -> str:
    """把 iu/、run_iu.py、skills/ 复制到 app_dir()；已经在 app_dir 里运行就不动。返回 run_iu.py 的路径"""
    dst = app_dir()
    runner = os.path.join(dst, "run_iu.py")
    if os.path.normcase(os.path.realpath(ROOT)) == os.path.normcase(os.path.realpath(dst)) or dry_run:
        return runner.replace("\\", "/")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    os.makedirs(dst, exist_ok=True)
    shutil.copytree(os.path.join(ROOT, "iu"), os.path.join(dst, "iu"), ignore=ignore, dirs_exist_ok=True)
    shutil.copytree(os.path.join(ROOT, "skills"), os.path.join(dst, "skills"), ignore=ignore, dirs_exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "run_iu.py"), runner)
    return runner.replace("\\", "/")


def render_skill(runner: str, python: str = None) -> str:
    text = open(os.path.join(ROOT, "skills", SKILL_NAME, "SKILL.md"), encoding="utf-8").read()
    cmd = f'"{(python or sys.executable).replace(chr(92), "/")}" "{runner}"'
    return text.replace("{{IU}}", f"`{cmd}`")


def install_skills(runner: str, dry_run: bool = False) -> list:
    """返回装好的 skill 目录列表（dry_run 时返回会装的目录）"""
    text = render_skill(runner)
    done = []
    for _label, base in targets():
        d = os.path.join(base, SKILL_NAME)
        done.append(d)
        if dry_run:
            continue
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8").write(text)
    return done


def uninstall_skills(dirs: list):
    for d in dirs or []:
        if os.path.basename(os.path.normpath(d)) == SKILL_NAME and os.path.isdir(d):  # 只删我们自己的 skill 目录
            shutil.rmtree(d, ignore_errors=True)


# ---------------- 常驻规则：写进各家 agent 的全局说明文件 ----------------
# 实测（docs/agents_landscape_2026-10.md 第 4 节）：只把 skill 放进目录，OpenCode 宿主 0/6 次去用；
# 全局说明文件里有一条规则后 2/2 次加载 skill 并照着委托。所以每家写一条带标记的规则，卸载时按标记精确删掉。
RULE_BEGIN = "<!-- infiniteusage:begin（iu install 自动加的，iu uninstall 会删掉这一段） -->"
RULE_END = "<!-- infiniteusage:end -->"


def rule_targets() -> list:
    """[(说明, 全局说明文件)]：已有这个 agent 的配置目录才写。宿主只有 Claude Code 和 Codex（花订阅额度的）；
    OpenCode、Qwen Code、Kilo 这些是 iu delegate 叫来干活的免费工人，不写规则：它们读到「先用 iu delegate」会绕回来"""
    h = home()
    out = []
    for label, base, name in [("Claude Code", ".claude", "CLAUDE.md"), ("Codex", ".codex", "AGENTS.md")]:
        if os.path.isdir(os.path.join(h, base)):
            out.append((label, os.path.join(h, base, name)))
    return out


def render_rule(runner: str, python: str = None) -> str:
    # 规则 A/B（docs/agents_landscape_2026-10.md 4.1）：「每条要求都记进 NOTES.md」让一次性小任务也先抄一遍任务说明，
    # 把省下的 15.6% 吃回到 1.5%；还有一次把 iu delegate 放到后台轮询。所以只记用户提的具体要求，委托写明要阻塞运行
    from .i18n import t
    cmd = f'"{(python or sys.executable).replace(chr(92), "/")}" "{runner}"'
    return (f"{RULE_BEGIN}\n" + t(
        "## InfiniteUsage：省付费用量（结果质量不变）\n\n"
        "- 开始写代码任务前，先加载并遵循 `infiniteusage` skill。\n"
        "- 能用命令验证的改代码子任务（修测试、按现有测试实现、构建要保持通过的小重构），先用 `iu delegate` "
        "交给这台电脑上的免费 coding agent：在前台阻塞运行（不要放到后台再轮询），验证通过才保留，失败会自动还原，然后自己做。\n"
        "- 输出很长的命令（测试、构建、git log/diff）用 `iu sum` 看摘要；需要细节就按行号读原始日志，不要重跑。\n"
        "- 用户在对话里提出具体要求时（颜色、名字、数值、文件名、做法上的约定），逐条原样记进项目根目录的 NOTES.md，"
        "改了当场更新；一次性的任务说明（比如「修好这些测试」）不用抄进去。上下文压缩后先读 NOTES.md 再继续。\n"
        "- 每个新会话开始时，如果项目根目录有 NOTES.md，先读它再动手：里面是之前的会话里用户定下的要求，"
        "就算用户这次没提「上次」也照着做；用户这次说的和笔记不一样时，以这次为准，并更新笔记。\n"
        f"- 这里的 `iu` 指：`{cmd}`\n",
        "## InfiniteUsage: fewer paid tokens, same results\n\n"
        "- Before starting a coding task, load and follow the `infiniteusage` skill.\n"
        "- For a code change a command can verify (fix tests, implement against existing tests, a small refactor that must "
        "keep the build green), first hand it to a free coding agent on this machine with `iu delegate`: run it in the "
        "foreground and wait (do not background it and poll). It is kept only if the check passes and rolled back "
        "otherwise; then do it yourself.\n"
        "- For commands with long output (tests, builds, git log/diff) read the `iu sum` summary; for details read the "
        "raw log by line range instead of re-running.\n"
        "- When the user states a concrete requirement (colors, names, numbers, file names, agreed conventions), write it "
        "verbatim, one per line, into NOTES.md in the project root and update it as soon as it changes; do not copy "
        "one-off task descriptions (such as \"fix these tests\") into it. After a context compaction, read NOTES.md "
        "before continuing.\n"
        "- At the start of every new session, if the project root has NOTES.md, read it before doing anything: it holds "
        "requirements the user set in earlier sessions; follow them even if the user does not mention earlier sessions. "
        "If the user now says something different, the user's latest words win; update the notes.\n"
        f"- `iu` here means: `{cmd}`\n") + f"{RULE_END}\n")


def _strip_rule(text: str, sep: str = "") -> str:
    """删掉标记之间的那一段，连同安装时在它前面补的分隔（sep，记在 install.json 里）"""
    while RULE_BEGIN in text and RULE_END in text:
        a = text.index(RULE_BEGIN)
        b = text.index(RULE_END, a) + len(RULE_END)
        if text[b:b + 1] == "\n":
            b += 1
        if sep and text[a - len(sep):a] == sep:
            a -= len(sep)
        text = text[:a] + text[b:]
    return text


def install_rules(runner: str, dry_run: bool = False, backup_dir: str = None, prev: list = None) -> list:
    """返回 [{"path", "created", "sep"}]；重装先按上次记下的分隔删掉旧的那段，再加新的，不重复"""
    block = render_rule(runner)
    prev_sep = {e.get("path"): e.get("sep", "") for e in prev or []}
    prev_created = {e.get("path"): e.get("created") for e in prev or []}
    done = []
    for _label, path in rule_targets():
        exists = os.path.exists(path)
        old = open(path, encoding="utf-8").read() if exists else ""
        base = _strip_rule(old, prev_sep.get(path, ""))
        sep = "" if not base else ("\n" if base.endswith("\n") else "\n\n")  # 和原文之间空一行
        new = base + sep + block
        created = prev_created.get(path, not exists)  # 重装时沿用第一次记下的：文件是不是我们新建的
        done.append({"path": path, "created": bool(created), "sep": sep})
        if dry_run or new == old:
            continue
        if exists and backup_dir:
            bak = os.path.join(backup_dir, "rule_" + path.replace(":", "").replace("\\", "_").replace("/", "_"))
            if not os.path.exists(bak):  # 只留第一次装之前的原样
                os.makedirs(backup_dir, exist_ok=True)
                shutil.copy2(path, bak)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(new)
    if not dry_run:  # 上次写过、这次不再写的（旧版写进过 OpenCode / Qwen Code）：按记录精确删掉
        now = {e["path"] for e in done}
        uninstall_rules([e for e in prev or [] if e.get("path") not in now])
    return done


def uninstall_rules(entries: list):
    for e in entries or []:
        path = e.get("path")
        if not path or not os.path.exists(path):
            continue
        text = _strip_rule(open(path, encoding="utf-8").read(), e.get("sep", ""))
        if e.get("created") and not text.strip():
            os.remove(path)
        else:
            open(path, "w", encoding="utf-8").write(text)


# ---------------- NOTES.md 自动放进上下文：Codex（hooks.json） ----------------
# Claude Code 上是 install.py 里的 SessionStart hook。Codex 的 hook 格式和 Claude Code 一样（SessionStart 的
# source 有 startup / compact，输出 additionalContext），所以直接用同一个 iu hook-compact；Codex 要求用户先在
# /hooks 里信任才会运行（实测：没信任时不触发）。
CODEX_HOOK_MARK = "hook-compact"
OPENCODE_PLUGIN = "infiniteusage-notes.js"  # 旧版给 OpenCode 装过的 NOTES 插件；OpenCode 现在只当免费工人，不再装


def codex_hook_supported() -> bool:
    """只在 Mac / Linux 上实测过；Windows 上 Codex 用什么 shell 跑 hook 没验证过，带引号的命令在 PowerShell 里会坏，先不装"""
    return os.name != "nt"


def codex_hooks_path():
    d = os.environ.get("CODEX_HOME") or os.path.join(home(), ".codex")
    return os.path.join(d, "hooks.json") if os.path.isdir(d) and codex_hook_supported() else None


def _strip_codex(cfg: dict):
    ev = (cfg.get("hooks") or {}).get("SessionStart")
    if not isinstance(ev, list):
        return
    keep = [e for e in ev if not any("run_iu.py" in str(h.get("command", "")) and CODEX_HOOK_MARK in str(h.get("command", ""))
                                     for h in (e.get("hooks") or []))]  # 只删我们装的那条
    if keep:
        cfg["hooks"]["SessionStart"] = keep
    else:
        cfg["hooks"].pop("SessionStart")
        if not cfg["hooks"]:
            cfg.pop("hooks")


def install_codex_notes(runner: str, python: str = None, dry_run: bool = False, prev: dict = None):
    """往 ~/.codex/hooks.json 加 SessionStart（startup|compact）→ iu hook-compact。返回 {"path", "created"}，没有 Codex 返回 None"""
    path = codex_hooks_path()
    if not path:
        return None
    exists = os.path.exists(path)
    try:
        cfg = json.load(open(path, encoding="utf-8")) if exists else {}
    except ValueError:
        return None  # 用户的文件坏了：不碰
    _strip_codex(cfg)
    cmd = f'"{(python or sys.executable).replace(chr(92), "/")}" "{runner}" hook-compact'
    cfg.setdefault("hooks", {}).setdefault("SessionStart", []).append(
        {"matcher": "startup|compact", "hooks": [{"type": "command", "command": cmd, "timeout": 10,
                                                   "statusMessage": "InfiniteUsage: loading NOTES.md",
                                                   "additionalContextLimit": 5000}]})
    created = (prev or {}).get("created", not exists) if (prev or {}).get("path") == path else not exists
    if not dry_run:
        open(path, "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return {"path": path, "created": bool(created)}


def uninstall_codex_notes(entry: dict):
    path = (entry or {}).get("path")
    if not path or not os.path.exists(path):
        return
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    except ValueError:
        return
    _strip_codex(cfg)
    if entry.get("created") and not cfg:
        os.remove(path)
    else:
        open(path, "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


# Codex 外包长输出（试验）：PreToolUse（Bash）→ iu hook-pre --codex。和上面的 NOTES.md hook 写同一个 hooks.json，
# 但只增删 PreToolUse 里我们那条，不碰 SessionStart。卸载时要先于 uninstall_codex_notes 调用（那边新建的文件删空才删）
CODEX_PRE_MARK = "hook-pre"


def _strip_codex_pre(cfg: dict):
    hooks = cfg.get("hooks")
    ev = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if not isinstance(ev, list):
        return
    keep = [e for e in ev if not any("run_iu.py" in str(h.get("command", "")) and CODEX_PRE_MARK in str(h.get("command", ""))
                                     for h in (e.get("hooks") or []))]
    if keep:
        hooks["PreToolUse"] = keep
    else:
        hooks.pop("PreToolUse")
        if not hooks:
            cfg.pop("hooks")


def install_codex_pre(runner: str, python: str = None, dry_run: bool = False, prev: dict = None):
    """往 ~/.codex/hooks.json 加 PreToolUse（Bash）→ iu hook-pre --codex。返回 {"path", "created"}，没有 Codex 返回 None"""
    path = codex_hooks_path()
    if not path:
        return None
    exists = os.path.exists(path)
    try:
        cfg = json.load(open(path, encoding="utf-8")) if exists else {}
    except ValueError:
        return None
    _strip_codex_pre(cfg)
    cmd = f'"{(python or sys.executable).replace(chr(92), "/")}" "{runner}" hook-pre --codex || true'  # 坏了也不拦命令
    cfg.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
    created = (prev or {}).get("created", not exists) if (prev or {}).get("path") == path else not exists
    if not dry_run:
        open(path, "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return {"path": path, "created": bool(created)}


def uninstall_codex_pre(entry: dict):
    path = (entry or {}).get("path")
    if not path or not os.path.exists(path):
        return
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    except ValueError:
        return
    _strip_codex_pre(cfg)
    if entry.get("created") and not cfg:
        os.remove(path)
    else:
        open(path, "w", encoding="utf-8").write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


def uninstall_opencode_notes(path: str):
    if path and os.path.basename(path) == OPENCODE_PLUGIN and os.path.isfile(path):  # 只删我们自己的插件
        os.remove(path)
