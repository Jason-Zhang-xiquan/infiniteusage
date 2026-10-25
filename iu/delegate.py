"""iu delegate：把一个自成一体、能用命令验证的改代码子任务，先交给免费 agent（OpenCode build agent + Zen 免费模型）做。

流程：
1. 记下工作区现状（git stash create 得到一个快照提交，不改动工作区；没被 git 跟踪的文件另外备份）
2. 免费 agent 在项目里直接改代码；模型链：失败或超时就换下一个
3. 跑调用方给的验证命令（测试、构建、脚本）。退出码 0 才算成功
4. 成功：打印改动文件和 diff，交给调用方审查。失败：把免费 agent 的改动全部还原，告诉调用方自己做

护栏：
- 项目标了 .iu-private 就不外包（免费线路可能留存数据）
- 必须是 git 仓库（要靠 git 还原）
- 验证命令由调用方给，不信免费 agent 自己说「好了」

依据：bench/free_coder.py（12 个修 bug 任务，单元测试判对错）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .det import det_filter
from .i18n import t
from .explore import ANSI, opencode_bin
from .offload import LOG, private_marker, redact
from .store import DATA_DIR

# 实测（2026-10-19 端到端）：在 Claude Code 的 Bash 工具里，stdin 是一直开着的管道，OpenCode 会等它读完而卡死 →
# 子进程一律 stdin=DEVNULL。Bash 工具最多等 600 秒，所以免费 agent 默认预算 420 秒，留时间给验证。
DIFF_SHOW_LIMIT = 12_000
# 免费额度用完：OpenCode 不退出、一直重试（实测会把 420 秒预算全卡满），看到这句就马上放弃，换下一家
QUOTA = re.compile(r"free usage limit reached|usage limit reached|insufficient_quota|exceeded your current quota|"
                   r"quota exceeded", re.I)
QUOTA_ERR = "free quota used up"
LOG_LINE = re.compile(r"^(timestamp=|INFO |WARN |ERROR |DEBUG )")  # --print-logs 的日志行，不算 agent 的回复
# 写代码用的免费模型顺序（bench/free_coder.py 实测，2026-10-19）：先准后快。用户只看用量和效果，不在乎数据是否被拿去训练，
# 所以测过的都用上；不想让代码进可能训练的模型，设 IU_NO_TRAINING_MODELS=1（只留 OpenCode Zen 写明零保留的）。
# nemotron-3.5-lightning-free 也全对，但有题要 7 分钟，等不起，不放进来
CODER_MODELS = ["deepseek-v4.1-flash-free", "glm-5.3-flash-free", "space-bunny-free", "nemotron-3-ultra-free",
                "mimo-v2.6-flash-free", "beam-free"]
ZERO_RETENTION = {"deepseek-v4.1-flash-free", "glm-5.3-flash-free", "space-bunny-free", "longcat-2.5-preview-free"}


GUIDE = ("\n\nRules: make the smallest change that fully solves the task; follow the existing code style; do not modify "
         "or delete tests unless the task asks for it; do not commit. When done, reply with a short summary of what "
         "you changed and why.")


def coder_models() -> list:
    if os.environ.get("IU_NO_TRAINING_MODELS"):
        return [m for m in CODER_MODELS if m in ZERO_RETENTION]
    return list(CODER_MODELS)


def sh(cmd: str, cwd: str, timeout: int = 600):
    shell = [shutil.which("bash") or "bash", "-c", cmd] if os.name == "nt" else ["/bin/sh", "-c", cmd]
    try:
        p = subprocess.run(shell, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, t(f"验证命令超时（{timeout} 秒）", f"check command timed out ({timeout} s)")


def git(args: list, cwd: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace").stdout


def snapshot(root: str) -> dict:
    stash = git(["stash", "create"], root).strip() or git(["rev-parse", "HEAD"], root).strip()
    untracked = [x for x in git(["ls-files", "--others", "--exclude-standard", "-z"], root).split("\0") if x]
    backup = tempfile.mkdtemp(prefix="iu_delegate_")
    for rel in untracked:
        dst = os.path.join(backup, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(root, rel), dst)
    return {"commit": stash, "untracked": set(untracked), "backup": backup}


def changes(root: str, snap: dict):
    tracked = [x for x in git(["diff", "--name-only", "-z", snap["commit"]], root).split("\0") if x]
    now = {x for x in git(["ls-files", "--others", "--exclude-standard", "-z"], root).split("\0") if x}
    new = sorted(now - snap["untracked"])
    edited_untracked = []
    for rel in snap["untracked"] & now:
        a, b = os.path.join(root, rel), os.path.join(snap["backup"], rel)
        if open(a, "rb").read() != open(b, "rb").read():
            edited_untracked.append(rel)
    return tracked, new, sorted(edited_untracked)


def restore(root: str, snap: dict):
    tracked, new, edited = changes(root, snap)
    # 快照里没有的文件（免费 agent 新建后又 git add 的）不能交给 checkout：路径不存在会让整条 checkout 失败
    in_snap = set(x for x in git(["ls-tree", "-r", "--name-only", "-z", snap["commit"], "--", *tracked],
                                 root).split("\0") if x) if tracked else set()
    git(["reset", "-q"], root)  # 免费 agent 若 git add 过，先取消暂存
    if in_snap:
        git(["checkout", snap["commit"], "--", *sorted(in_snap)], root)
        git(["reset", "-q"], root)  # checkout <commit> -- 会顺带暂存，恢复成调用前「改动未暂存」的样子
    for rel in tracked:
        p = os.path.join(root, rel)
        if rel not in in_snap and os.path.isfile(p):
            os.remove(p)
    for rel in new:
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            os.remove(p)
    for rel in edited:
        shutil.copy2(os.path.join(snap["backup"], rel), os.path.join(root, rel))


def host_agents() -> set:
    """当前是在哪个 coding agent 里被调用的（看它们给 shell 设的环境变量）"""
    return {"opencode"} if os.environ.get("OPENCODE") else set()


def attempts(via: str, model: str, brief: str, root: str) -> list:
    """按委托顺序展开成 [(名字, 命令)]：OpenCode 每个免费模型算一次尝试，其他 agent 各一次"""
    from . import agents
    out, prompt = [], brief + GUIDE
    for aid in ([via] if via else agents.chain()):
        if aid in host_agents():  # 宿主自己就是这个 agent：两个实例改同一个项目会互相覆盖（实测 OpenCode 套 OpenCode 丢了修复）
            continue
        if aid == "opencode":
            try:
                ob = opencode_bin()
            except SystemExit:
                continue
            for m in ([model] if model else coder_models()):
                # --print-logs：额度用完的报错只出现在日志里
                out.append((f"opencode/{m}", [ob, "run", "--print-logs", "--agent", "build", "-m", f"opencode/{m}",
                                              "--dir", root, prompt]))
            for b in ([] if model else api_backends()):  # Zen 免费额度用完时的后备：OpenCode 换用户自己的免费 key
                out.append((f"opencode+{b['id']}/{b['model']}", api_cmd(ob, b, root, prompt)))
            continue
        try:
            a = agents.agent_by_id(aid)
        except KeyError:
            continue
        b = agents.find_bin(a)
        if not b:
            continue
        p = prompt
        if os.name == "nt":  # Windows 的 .cmd 包装会改坏多行、引号、%：任务说明写进文件，只传一句短提示
            f = os.path.join(tempfile.gettempdir(), f"iu_brief_{os.getpid()}.md")
            open(f, "w", encoding="utf-8").write(prompt)
            p = f"Your task is written in the file {f}. Read that file and complete the task in the current directory."
        out.append((aid, agents.build_cmd(a, b, p, root)))
    return out


def family(name: str) -> str:
    """共用一份额度的那一组：opencode（Zen 免费模型）、opencode+gemini（用户的 Gemini key）、kilo ……"""
    return name.split("/")[0]


def api_backends() -> list:
    """用户用 iu api add 加的、配好 key 的免费 API，给 OpenCode 当后备模型（Zen 免费额度用完时接着干活）。
    设了 IU_NO_TRAINING_MODELS 就不用：这些免费层大多会拿数据训练。资格测试里改代码没过的不用"""
    if os.environ.get("IU_NO_TRAINING_MODELS"):
        return []
    from .providers import allowed, ready
    return [b for b in ready() if allowed(b, "delegate")]


def api_cmd(ob: str, b: dict, root: str, prompt: str) -> list:
    """OpenCode 用用户自己的那家 API 跑（provider 名 iu-<id>，配置由 opencode_api_env 给）"""
    return [ob, "run", "--print-logs", "--agent", "build", "-m", f"iu-{b['id']}/{b['model']}", "--dir", root, prompt]


def opencode_api_env(backends: list) -> dict:
    """给 OpenCode 的临时配置（OPENCODE_CONFIG_CONTENT，叠在用户自己的配置上，不改任何文件）：每个 API 一个
    OpenAI 兼容 provider，key 写成 {env:变量名}，值只放进子进程的环境变量，不上命令行"""
    from .offload import setting
    cfg, env = {"$schema": "https://opencode.ai/config.json", "provider": {}}, {}
    for b in backends:
        cfg["provider"]["iu-" + b["id"]] = {
            "npm": "@ai-sdk/openai-compatible", "name": f"{b['id']} (iu api)",
            "options": {"baseURL": b["base_url"], "apiKey": "{env:" + b["key_env"] + "}"},
            "models": {b["model"]: {"name": b["model"]}}}
        env[b["key_env"]] = setting(b["key_env"]) or ""
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
    return env


SECRET_ENV = re.compile(r"API_?KEY|TOKEN|SECRET|PASSW|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY", re.I)


def agent_env(cmd: list, extra_env: dict = None) -> dict:
    """OpenCode 用的是 Zen 免费模型或我们给的那一个 key：环境变量里别的 key / token 不交给它（它会跑 shell，
    输出会发给免费模型）。别的 agent 可能靠环境变量登录，原样保留"""
    env = dict(os.environ)
    if os.path.basename(cmd[0]).lower().startswith("opencode"):
        env = {k: v for k, v in env.items() if k.upper().startswith("OPENCODE") or not SECRET_ENV.search(k)}
    return {**env, "NO_COLOR": "1", "NODE_OPTIONS": "--max-old-space-size=2048", **(extra_env or {})}


def run_agent(cmd: list, root: str, timeout: int, extra_env: dict = None):
    env = agent_env(cmd, extra_env)
    p = subprocess.Popen(cmd, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                         start_new_session=(os.name != "nt"))
    buf, quota = [], threading.Event()

    def pump():  # 边读边看：额度用完的报错一出现就停，不等到超时
        for line in iter(p.stdout.readline, b""):
            buf.append(line)
            if QUOTA.search(line.decode("utf-8", "replace")):
                quota.set()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.time() + timeout
    while p.poll() is None and not quota.is_set() and time.time() < deadline:
        time.sleep(0.3)
    running = p.poll() is None
    if running or os.name != "nt":  # 超时、额度用完要停；正常退出也按进程组清掉 agent 留下的子进程（vitest 等）
        try:
            if os.name != "nt":
                os.killpg(p.pid, signal.SIGKILL)
            else:
                p.kill()
        except (ProcessLookupError, PermissionError):
            pass
    p.wait()
    reader.join(5)
    if quota.is_set():
        return None, QUOTA_ERR
    if running:
        return None, t("超时", "timed out")
    out = b"".join(buf)
    text = "\n".join(x for x in ANSI.sub("", out.decode("utf-8", "replace")).splitlines()
                     if not LOG_LINE.match(x)).strip()
    if p.returncode != 0:
        return None, text[-200:] or t(f"退出码 {p.returncode}", f"exit code {p.returncode}")
    return text, None


def cmd_delegate(args):
    brief = args.brief or (open(args.brief_file, encoding="utf-8").read() if args.brief_file else sys.stdin.read())
    brief = redact(brief.strip())  # 任务说明会发给免费模型：误贴进来的 key 先遮掉
    if not brief:
        raise SystemExit(t("没有任务说明：用 --brief-file 文件、命令行参数或标准输入给", "No brief: pass it with --brief-file, as an argument, or on stdin"))
    cwd = os.getcwd()
    marker = private_marker(cwd)
    if marker:
        print("[iu delegate 委托失败 / delegation failed: " + t(f"这个项目标了 {marker}，不外包给免费模型。请自己做]", f"this project is marked {marker}, not sent to free models. Do it yourself]"))
        sys.exit(1)
    root = git(["rev-parse", "--show-toplevel"], cwd).strip()
    if not root:
        print("[iu delegate 委托失败 / delegation failed: " + t("不是 git 仓库，没法安全还原。请自己做]", "not a git repository, cannot roll back safely. Do it yourself]"))
        sys.exit(1)
    snap = snapshot(root)
    t0, tried, last_fail = time.time(), [], ""
    try:
        from . import speed
        plan = speed.rank(attempts(args.via, args.model, brief, root), key_of=lambda a: "agent:" + a[0])
        if not plan:
            tried.append(t("这台电脑上没有可用的 coding agent（运行 iu agents 看建议）", "no usable coding agent on this machine (run iu agents for suggestions)"))
        api_env = opencode_api_env(api_backends()) if any(m.startswith("opencode+") for m, _ in plan) else {}
        no_quota, runs = set(), 0  # 报过额度用完的 agent（同一家的免费模型共用额度，一个用完就都别试了）；真正跑过几次
        for i, (model, cmd) in enumerate(plan):
            left = args.budget - (time.time() - t0)
            if left < 30 or runs >= args.max_models:  # --max-models 只数真正跑了的，跳过的不占名额
                break
            if family(model) in no_quota:
                continue
            # 后面还有别的可试：这一个最多用 60%（至少 150 秒），卡住的模型不会把整个预算吃光
            more = runs + 1 < args.max_models and any(family(m) not in no_quota for m, _ in plan[i + 1:])
            cap = max(150, left * 0.6) if more else left
            runs += 1
            a0 = time.time()
            reply, err = run_agent(cmd, root, int(min(left, cap)), api_env if model.startswith("opencode+") else None)
            if err == QUOTA_ERR:
                no_quota.add(family(model))
                for m, _ in plan:  # 同一家都记成失败：下次调用直接排到最后，先用别家
                    if family(m) == family(model) and m != model:
                        speed.record("agent:" + m, False)
            if err:  # 超时、报错退出、额度用完才算这个 agent 不健康；没改文件、验证不过是任务本身的问题
                speed.record("agent:" + model, False)
            elif reply is not None:
                speed.record("agent:" + model, True, time.time() - a0)
            tracked, new, edited = changes(root, snap)
            files = tracked + new + edited
            if err or not files:
                tried.append(f"{model}: {err or t('没有改任何文件', 'changed no files')}"[:120])
                restore(root, snap)
                continue
            code, vout = (0, "") if args.verify == "none" else sh(args.verify, root, args.verify_timeout)
            if code != 0:
                tried.append(t(f"{model}：验证没通过（退出码 {code}）", f"{model}: check failed (exit code {code})"))
                last_fail = det_filter(vout, "error failed", False, cap=2500)
                restore(root, snap)
                continue
            diff = git(["diff", snap["commit"], "--", *tracked], root) if tracked else ""
            for rel in new:
                diff += f"\n+++ {t('新文件', 'new file')} {rel}\n" + open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
            raw = os.path.join(DATA_DIR, "raw", time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}-delegate.diff")
            os.makedirs(os.path.dirname(raw), exist_ok=True)
            open(raw, "w", encoding="utf-8").write(diff)
            secs = round(time.time() - t0)
            verified = t("没有验证（--verify none）", "not verified (--verify none)") if args.verify == "none" else t(f"`{args.verify}` 通过", f"`{args.verify}` passed")
            print(f"[iu delegate 委托成功 / delegated OK · {model} · {secs} s · {verified}]")
            print(t("改动文件：", "Changed files: ") + ", ".join(files))
            print(diff[:DIFF_SHOW_LIMIT] + (t("\n…（diff 太长已截断）", "\n… (diff truncated)") if len(diff) > DIFF_SHOW_LIMIT else ""))
            print(t("接手的 agent 的说明（未核实）：", "What the agent says it did (unverified): ") + reply[-800:])
            print(t(f"[完整 diff：{raw}。改动还在工作区里、没有提交：请审查，不对就改或用 git 还原]", f"[Full diff: {raw}. The change is in the working tree, not committed: review it, fix it or revert it with git]"))
            log(cwd, model, True, secs, len(files))
            return
        msg = "; ".join(tried) or t("时间不够", "out of time")
        print(f"[iu delegate 委托失败 / delegation failed: {msg}. " + t("免费 agent 的改动已全部还原，工作区和调用前一样。请自己做]", "All changes by the free agent were rolled back; the working tree is as before. Do it yourself]"))
        if last_fail:
            print(t("最后一次验证的关键输出：\n", "Key output of the last check:\n") + last_fail)
        log(cwd, None, False, round(time.time() - t0), 0)
        sys.exit(1)
    finally:
        shutil.rmtree(snap["backup"], ignore_errors=True)


def log(cwd, model, ok, secs, nfiles):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "kind": "delegate", "cwd": cwd, "routed": model and f"opencode/{model}",
                            "ok": ok, "seconds": secs, "files": nfiles}, ensure_ascii=False) + "\n")
