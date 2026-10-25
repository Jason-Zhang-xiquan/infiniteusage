"""iu：跨平台额度账本（最小版）

用法:
  iu [report] [--days 7] [--mac <ssh主机>]   报告：官方额度 + 重置倒计时、按模型/项目/天的用量、浪费信号
  iu export [--days 8]                                  输出本机聚合数据（JSON），给另一台机器合并用
  iu statusline                                         当 Claude Code 的 statusline 命令：显示额度并记录官方快照
  iu install-statusline [--dry-run]                     把上面这条写进 ~/.claude/settings.json（会先备份）
  iu ask "问题" [文件...]                               免费模型读文件、回答问题（经 FreeLLMAPI）
  iu sum -- <命令...>                                    跑命令，长输出交给免费模型压成摘要，原始输出存文件
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time

from . import claude, codex, limits, offload, providers, report
from .i18n import t
from .store import DIMS, Store, iter_rows

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PKG_DIR)


def scan_local():
    host = socket.gethostname()
    st_c, st_x = Store("claude"), Store("codex")
    t0 = time.time()
    claude.scan(st_c, host)
    codex.scan(st_x, host)
    st_c.save()
    st_x.save()
    rows = list(iter_rows(st_c.rows)) + list(iter_rows(st_x.rows))
    return {"host": host, "rows": rows, "codex_limits": st_x.extra.get("codex_limits") or {},
            "claude_snap": limits.load_claude(), "seconds": round(time.time() - t0, 1)}


def cut(rows, days):
    since = time.strftime("%Y-%m-%dT%H", time.gmtime(time.time() - days * 86400))
    return [(d, b) for d, b in rows if d["hour"] >= since]


def cmd_export(args):
    data = scan_local()
    data["rows"] = [[d[k] for k in DIMS] + [b] for d, b in cut(data["rows"], args.days)]
    sys.stdout.write(json.dumps(data, separators=(",", ":")))


def fetch_remote(host: str, days: int) -> dict:
    """把 iu 包传到远端 ~/.infiniteusage/app 再运行 export（远端只需要系统自带的 python3）"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(PKG_DIR, arcname="iu", filter=lambda ti: None if "__pycache__" in ti.name else ti)
    cmd = ("mkdir -p ~/.infiniteusage/app && tar -xzf - -C ~/.infiniteusage/app && "
           f"cd ~/.infiniteusage/app && python3 -m iu export --days {days}")
    res = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, cmd], input=buf.getvalue(),
                         capture_output=True, timeout=900)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode("utf-8", "replace").strip()[-500:])
    data = json.loads(res.stdout)
    data["rows"] = [(dict(zip(DIMS, r[:-1])), r[-1]) for r in data["rows"]]
    return data


def newer(a: dict, b: dict, key) -> dict:
    return b if (b and (not a or key(b) > key(a))) else a


def cmd_report(args):
    sources = [scan_local()]
    for host in args.mac or []:
        try:
            sources.append(fetch_remote(host, args.days + 1))
        except Exception as e:  # noqa: BLE001
            print(f"[{host}] 读取失败：{e}", file=sys.stderr)
    rows, codex_lim, claude_snap = [], {}, {}
    for s in sources:
        rows += cut(s["rows"], args.days + 1)
        codex_lim = newer(codex_lim, s.get("codex_limits") or {}, lambda x: x.get("ts", ""))
        claude_snap = newer(claude_snap, s.get("claude_snap") or {}, lambda x: x.get("ts", 0))
    hosts = [f"{s['host']}（{s.get('seconds', '?')}秒）" for s in sources]
    print(report.build(rows, claude_snap, codex_lim, args.days, hosts))


def cmd_statusline(_args):
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    try:
        limits.record_statusline(payload)
    except OSError:
        pass  # 状态栏不能因为写文件失败而挂掉
    sys.stdout.write(limits.statusline_text(payload))


def cmd_install_statusline(args):
    settings_path = os.path.join(os.path.dirname(claude.ROOT), "settings.json")
    try:
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        settings = {}
    runner = os.path.join(ROOT, "run_iu.py").replace("\\", "/")
    python = sys.executable.replace("\\", "/")
    new = {"type": "command", "command": f'"{python}" "{runner}" statusline', "refreshInterval": 60}
    old = settings.get("statusLine")
    print(f"settings：{settings_path}\n现在的 statusLine：{json.dumps(old, ensure_ascii=False)}\n改成：{json.dumps(new, ensure_ascii=False)}")
    if old and "run_iu.py" not in json.dumps(old):
        print("已经有别的 statusLine，没有覆盖。要替换请手动改，或先删掉原来的再运行。")
        return
    if args.dry_run:
        return
    if os.path.exists(settings_path):
        shutil.copy2(settings_path, settings_path + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    settings["statusLine"] = new
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    print("已写入。下一次 Claude Code 刷新状态栏就开始记录官方额度。")


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    p = argparse.ArgumentParser(prog="iu", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("report")
    for q in (p, r):
        q.add_argument("--days", type=int, default=7)
        q.add_argument("--mac", action="append", help=t("也读这台 ssh 主机上的日志（可写多次）", "also read logs on this ssh host (repeatable)"))
    e = sub.add_parser("export")
    e.add_argument("--days", type=int, default=8)
    sub.add_parser("statusline")
    i = sub.add_parser("install-statusline")
    i.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("ask", help=t("免费模型读文件、回答问题或写草稿", "a free model reads files, answers questions or drafts text"))
    a.add_argument("question")
    a.add_argument("files", nargs="*")
    a.add_argument("--type", default="file_qa", choices=["file_qa", "explain"],
                   help=t("file_qa = 读文件回答问题；explain = 代码推理（函数对某个输入返回什么）", "file_qa = answer from files; explain = code reasoning (what a function returns for an input)"))
    s = sub.add_parser("sum", help=t("跑命令，长输出交给免费模型压成摘要", "run a command; a free model summarizes long output"))
    s.add_argument("--goal", help=t("想从输出里找什么（比如：哪些文件声明了 X）", "what you want from the output (e.g. which files declare X)"))
    s.add_argument("--budget", type=int, default=30, help=t("等免费模型最多几秒，超时改用规则过滤", "max seconds to wait for the free model before falling back to a rule filter"))
    s.add_argument("command", nargs=argparse.REMAINDER)
    for q in (a, s):
        q.add_argument("--model", default="route",
                       help=t("route = 按能力画像的路由表从强到弱试（默认）；也可以指定 FreeLLMAPI 的模型名或 auto", "route = try models from strongest down (default); or a FreeLLMAPI model name, or auto"))
        q.add_argument("--max-out", type=int, default=1200, help=t("最多输出多少 token", "max output tokens"))
        q.add_argument("--think", action="store_true", help=t("允许免费模型先思考（慢、费 token）", "let the free model think first (slower)"))
        q.add_argument("--force", action="store_true", help="项目标了 .iu-private 也外包")
    hp = sub.add_parser("hook-pre", help=t("Claude Code / Codex PreToolUse hook（从 stdin 读 JSON）", "Claude Code / Codex PreToolUse hook (JSON on stdin)"))
    hp.add_argument("--codex", action="store_true", help=t("按 Codex 的格式输出（带 permissionDecision: allow）", "output in Codex format (with permissionDecision: allow)"))
    sub.add_parser("hook-explore-gate", help=t("PreToolUse hook：试点项目里搜代码改走 iu explore（只拒绝不放行）", "PreToolUse hook: route code search to iu explore in pilot projects (deny only)"))
    ins = sub.add_parser("install", help=t("装进所有 coding agent：skill、常驻规则、NOTES.md hook，Claude Code 约 300K 压缩、Codex 约 150K（会备份；--dry-run 预览）", "install into every coding agent: skill, standing rule, NOTES.md hook; Claude Code compacts at ~300K, Codex at ~150K (backs up; --dry-run to preview)"))
    ins.add_argument("--agent", default="all", help=t("all（默认）/ claude-code / skills（只装 skill）", "all (default) / claude-code / skills (skill only)"))
    ins.add_argument("--compact-at", type=int, default=300_000, help=t("上下文到多少 token 时压缩（默认 300000）", "compact at this many context tokens (default 300000)"))
    ins.add_argument("--codex-compact-at", type=int, default=150_000,
                     help=t("Codex 上下文到多少 token 时压缩（默认 150000，0 = 不改 Codex）", "Codex: compact at this many context tokens (default 150000, 0 = leave Codex alone)"))
    ins.add_argument("--no-compact", action="store_true", help=t("不改压缩设置（Claude Code 和 Codex 都不改）", "do not change compaction settings (Claude Code or Codex)"))
    ins.add_argument("--no-offload", action="store_true", help=t("不装外包长输出的 hook", "do not install the long-output offload hook"))
    ins.add_argument("--codex-offload", action="store_true",
                     help=t("试验：Codex 也装外包长输出的 hook（PreToolUse → iu hook-pre --codex；只在 Mac / Linux 上装）", "experimental: also install the offload hook for Codex (PreToolUse → iu hook-pre --codex; Mac / Linux only)"))
    ins.add_argument("--no-delegate", action="store_true", help=t("不装「子 agent 先试免费 agent 写代码」的 hook", "do not install the sub-agent delegation hook"))
    ins.add_argument("--no-notes", action="store_true", help=t("不装「压缩后、新会话开场把项目 NOTES.md 塞进上下文」的 hook", "do not install the NOTES.md hook (after compaction / at session start)"))
    ins.add_argument("--no-rules", action="store_true", help=t("不往各家 agent 的全局说明文件（CLAUDE.md / AGENTS.md）里加常驻规则", "do not add the standing rule to global instruction files (CLAUDE.md / AGENTS.md)"))
    ins.add_argument("--dry-run", action="store_true", help=t("只显示要改什么", "only show what would change"))
    uni = sub.add_parser("uninstall", help=t("卸载：删掉 skill、规则和 hook，还原原来的设置", "uninstall: remove skill, rules and hooks, restore previous settings"))
    uni.add_argument("--agent", default="claude-code")
    uni.add_argument("--dry-run", action="store_true")
    cmp_ = sub.add_parser("compare", help=t("装前 / 装后对比账本", "before / after usage comparison"))
    cmp_.add_argument("--days", type=int, default=7, help=t("装之前取多少天做对照", "days before install to compare against"))
    x = sub.add_parser("explore", help=t("查代码的问题交给免费 agent（OpenCode），返回带 文件:行 的结论", "ask a free agent (OpenCode) a code question; answer cites file:line"))
    x.add_argument("question")
    x.add_argument("--model", help=t("指定 OpenCode 免费模型；不填就按 deepseek → space-bunny → longcat 依次试", "OpenCode free model; default tries deepseek → space-bunny → longcat"))
    x.add_argument("--budget", type=int, default=240, help=t("最多等几秒", "max seconds to wait"))
    x.add_argument("--force", action="store_true", help="项目标了 .iu-private 也外包")
    dl = sub.add_parser("delegate", help=t("改代码子任务先交给免费 agent，验证命令通过才保留，否则全部还原", "hand a code change to a free agent; kept only if the check passes, otherwise fully rolled back"))
    dl.add_argument("brief", nargs="?", help=t("任务说明；也可以用 --brief-file 或标准输入", "the brief; or use --brief-file or stdin"))
    dl.add_argument("--brief-file")
    dl.add_argument("--verify", required=True, help=t("证明做对了的命令（测试/构建/脚本），退出码 0 才算成功；none = 不验证", "command that proves it works (tests/build/script), exit 0 = success; none = no check"))
    dl.add_argument("--verify-timeout", type=int, default=150)
    dl.add_argument("--budget", type=int, default=420, help=t("免费 agent 最多花几秒（所有模型合计）", "max seconds for the free agents (all models together)"))
    dl.add_argument("--model", help=t("指定 OpenCode 免费模型", "OpenCode free model"))
    dl.add_argument("--via", help=t("只用这个 agent（opencode / kilo / codebuddy / qwen / codex …），不按委托顺序", "use only this agent (opencode / kilo / codebuddy / qwen / codex ...), ignore the delegation order"))
    dl.add_argument("--max-models", type=int, default=3, help=t("最多试几次（每个模型或 agent 算一次；总时间还受 --budget 限制）", "max attempts (one per model or agent; total time also capped by --budget)"))
    ag = sub.add_parser("agents", help=t("找这台电脑上的 coding agent 和免费 API，给安装 / 注册建议，设置委托顺序", "find coding agents and free APIs on this machine, suggest what to install / register, set the delegation order"))
    ag.add_argument("--json", action="store_true")
    ag.add_argument("--set-chain", help=t("委托顺序，逗号分隔，比如 opencode,kilo；传空字符串恢复默认", "delegation order, comma-separated, e.g. opencode,kilo; empty string = default"))
    sub.add_parser("hook-agent", help=t("PreToolUse hook：通用子 agent 的改代码任务，先让它试 iu delegate", "PreToolUse hook: general sub-agents try iu delegate first for code changes"))
    api = sub.add_parser("api", help=t("管理你自己加的免费 API（Gemini、Groq 等，自己注册、自己输入 key）",
                                       "Manage free APIs you add yourself (Gemini, Groq, ...; you register and enter the key)"))
    api.add_argument("action", choices=["list", "add", "test", "qualify", "remove"], nargs="?", default="list")
    api.add_argument("--no-qualify", action="store_true",
                     help=t("add 之后不跑资格测试（之后可以 iu api qualify <名字>）", "skip the qualification test after add (run iu api qualify <name> later)"))
    api.add_argument("name", nargs="?", help=t("预设名（gemini、groq、openrouter…）或自己起的名字", "preset (gemini, groq, openrouter, ...) or your own name"))
    api.add_argument("--base-url", help=t("OpenAI 兼容接口地址（非预设时必填）", "OpenAI-compatible base URL (required if not a preset)"))
    api.add_argument("--model", help=t("用哪个模型（默认查 /models 自动挑）", "model to use (default: picked from /models)"))
    api.add_argument("--key-env", help=t("key 在 keys.env 里的变量名", "variable name of the key in keys.env"))
    sp = sub.add_parser("speed", help=t("免费模型测速：看记录，或 --probe 并行测一遍", "free model speed: show the record, or --probe to test all now"))
    sp.add_argument("--probe", action="store_true", help=t("现在并行测一遍", "probe all now, in parallel"))
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--json", action="store_true")
    sub.add_parser("hook-compact", help=t("SessionStart hook（compact / startup）：压缩一结束、新会话一开始，把项目的 NOTES.md 塞进上下文", "SessionStart hook (compact / startup): puts the project NOTES.md into context"))
    args = p.parse_args(argv)
    from . import agents, compare, delegate, explore, hook, install, speed
    {"export": cmd_export, "statusline": cmd_statusline, "install-statusline": cmd_install_statusline,
     "ask": offload.cmd_ask, "sum": offload.cmd_sum, "hook-pre": hook.cmd_hook_pre,
     "explore": explore.cmd_explore, "hook-explore-gate": hook.cmd_hook_explore_gate,
     "install": install.cmd_install, "uninstall": install.cmd_uninstall,
     "compare": compare.cmd_compare, "delegate": delegate.cmd_delegate, "agents": agents.cmd_agents,
     "hook-agent": hook.cmd_hook_agent, "hook-compact": hook.cmd_hook_compact, "api": providers.cmd_api,
     "speed": speed.cmd_speed}.get(args.cmd, cmd_report)(args)
