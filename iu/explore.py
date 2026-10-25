"""iu explore：把「X 在哪、怎么工作」这类查代码的问题整块交给免费 agent（OpenCode + Zen 免费模型，只读的 plan agent），
付费 agent 只拿到带 文件:行 的结论，再按行号核对。

依据：bench/explore_bench.py，10 个查代码问题 × 3 个免费模型，30/30 答对（docs/levers_small_results.md）。
护栏：
- 项目标了 .iu-private 就不外包（免费线路可能留存数据）
- 答案里的每个 文件:行 都核对一遍：文件在仓库里找不到、或行号超出文件长度的，标成「未核实」
- 模型链：失败或超时就换下一个；都不行就明确告诉 agent 自己查
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

from .offload import LOG, private_marker
from .i18n import t
from .store import DATA_DIR

MODELS = ["deepseek-v4.1-flash-free", "space-bunny-free", "longcat-2.5-preview-free"]
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
REF = re.compile(r"([\w\-./]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|cs|cpp|c|h|rb|php|swift|json|ya?ml|toml|md))"
                 r"[:(](\d+)")


def opencode_bin() -> str:
    # Windows 上直接用 exe：经过 .cmd 包装，多行任务说明里的引号、换行、% 会被 cmd.exe 改坏
    for cand in (os.environ.get("IU_OPENCODE"),
                 os.path.expanduser("~/iu_tools/opencode/node_modules/opencode-windows-x64/bin/opencode.exe"),
                 os.path.expanduser("~/iu_tools/opencode/node_modules/.bin/opencode"), shutil.which("opencode")):
        if cand and os.path.exists(cand):
            return cand
    raise SystemExit(t("找不到 OpenCode：设置 IU_OPENCODE，或在 ~/iu_tools/opencode 里 npm i opencode-ai", "OpenCode not found: set IU_OPENCODE, or run npm i opencode-ai in ~/iu_tools/opencode"))


def repo_root(start: str) -> str:
    try:
        out = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except OSError:
        pass
    return start


def verify_refs(text: str, root: str):
    """返回 (核实通过的引用, 未核实的引用)。文件名只给了 basename 时在仓库里找同名文件"""
    ok, bad, cache = [], [], {}
    for path, line in sorted(set(REF.findall(text))):
        n = int(line)
        cands = cache.get(path)
        if cands is None:
            full = os.path.join(root, path)
            if os.path.isfile(full):
                cands = [full]
            else:
                base = os.path.basename(path)
                cands = []
                for dirpath, dirnames, files in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git", "dist", "build")]
                    if base in files:
                        cands.append(os.path.join(dirpath, base))
            cache[path] = cands
        good = False
        for c in cands:
            try:
                with open(c, encoding="utf-8", errors="replace") as f:
                    if sum(1 for _ in f) >= n:
                        good = True
                        break
            except OSError:
                continue
        (ok if good else bad).append(f"{path}:{line}")
    return ok, bad


def run_once(model: str, question: str, root: str, timeout: int):
    prompt = (f"{question}\nLook it up in this repository (read-only, do not modify anything). Answer concisely and "
              f"cite every claim as path:line.")
    env = {**os.environ, "NO_COLOR": "1", "NODE_OPTIONS": "--max-old-space-size=2048"}
    p = subprocess.Popen([opencode_bin(), "run", "--agent", "plan", "-m", f"opencode/{model}", "--dir", root, prompt],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        return None, t("超时", "timed out")
    text = ANSI.sub("", out.decode("utf-8", "replace")).strip()
    if p.returncode != 0 or "Error:" in text[:400] or len(text) < 20:
        return None, text[-200:] or t(f"退出码 {p.returncode}", f"exit code {p.returncode}")
    text = re.sub(r"^>\s*\S+\s*·\s*\S+\s*\n", "", text)  # 去掉 OpenCode 的「> plan · 模型」抬头
    return text, None


def cmd_explore(args):
    cwd = os.getcwd()
    marker = private_marker(cwd)
    if marker and not args.force:
        raise SystemExit(t(f"这个项目标了 {marker}：不外包给免费模型。请自己查代码", f"This project is marked {marker}: not sent to free models. Search the code yourself"))
    root = repo_root(cwd)
    t0, errors = time.time(), []
    for model in ([args.model] if args.model else MODELS):
        left = args.budget - (time.time() - t0)
        if left < 10:
            break
        text, err = run_once(model, args.question, root, int(left))
        if text:
            ok, bad = verify_refs(text, root)
            print(text)
            note = f"[iu explore · {model} · {round(time.time() - t0)} s · " + t(f"核实了 {len(ok)} 处引用", f"{len(ok)} references verified")
            if bad:
                note += t(f"，{len(bad)} 处未核实（找不到文件或行号越界）：", f", {len(bad)} unverified (file missing or line out of range): ") + ", ".join(bad[:6])
            print(note + t("。改代码前用 Read 按行号核对]", ". Check the lines with Read before editing]"))
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "kind": "explore", "cwd": cwd, "routed": f"opencode/{model}",
                                    "chars_out": len(text), "refs_ok": len(ok), "refs_bad": len(bad),
                                    "seconds": round(time.time() - t0, 1)}, ensure_ascii=False) + "\n")
            return
        errors.append(f"{model}: {err[:80]}")
    print(t("[iu explore 没拿到答案：", "[iu explore got no answer: ") + "; ".join(errors) + t("。请自己查代码]", ". Search the code yourself]"))
    sys.exit(1)
