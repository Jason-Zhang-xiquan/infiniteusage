"""能力画像语料第二批：在 FreeLLMAPI（公开 MIT 项目）干净副本的临时克隆里生成，标准答案都由程序算出来。
新增任务类型：
  search   搜索结果里按目标找位置（sum + goal）
  ask      读单个文件回答问题并给行号
  git      git 历史里按目标找提交
  explain  代码推理：函数对某个输入返回什么（标准答案 = 真的用 tsx 跑一遍的结果）
  testgen  写单元测试：在干净代码上要通过、在注入了 bug 的代码上要失败
  build    更多测试日志
用法（Mac）: python3 make_corpus2.py /tmp/flbench_clean /tmp/iu_corpus2
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

CLEAN, OUT = sys.argv[1], sys.argv[2]
REPO = "/tmp/flbench_c2"
SERVER = os.path.join(REPO, "server")
ENV = {**os.environ, "PATH": "/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:" + os.environ["PATH"],
       "NO_COLOR": "1", "FORCE_COLOR": "0", "CI": "1"}


def run(cmd, cwd=SERVER):
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, env=ENV)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def save(cid, kind, task_type, raw, required, locations, command="", exit_code=0, goal="", question="", files=None,
         extra=None):
    d = os.path.join(OUT, cid)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "raw.txt"), "w", encoding="utf-8").write(raw)
    g = {"id": cid, "kind": kind, "task_type": task_type, "command": command, "exit": exit_code, "goal": goal,
         "question": question, "files": files or [], "required": required, "locations": locations, **(extra or {})}
    json.dump(g, open(os.path.join(d, "gold.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{cid}: {task_type}, {len(raw)} chars, required {required}, locations {locations}", flush=True)


def line_of(path, needle):
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        if needle in line:
            return i
    raise ValueError(needle)


def tsx_eval(expr_import, expr):
    code, out = run(f"npx tsx -e \"{expr_import}; console.log(JSON.stringify({expr}))\"")
    if code != 0:
        raise RuntimeError(out[-500:])
    return out.strip().splitlines()[-1]


shutil.rmtree(REPO, ignore_errors=True)
subprocess.run(["cp", "-Rc", CLEAN, REPO], check=True)
os.makedirs(OUT, exist_ok=True)

# ---------- search ----------
cmd = "rg -n process.env src -g '!**/__tests__/**'"
code, raw = run(cmd)
_, truth = run("rg -n process.env src/lib/provider-timeout.ts")
locs = [f"provider-timeout.ts:{m}" for m in re.findall(r"^(\d+):", truth, re.M)]
save("search_env_timeout", "sum", "search", raw, [], locs[:4], cmd, code,
     goal="Which lines read environment variables that configure provider timeouts (file:line)?")

cmd = "rg -n '^export function' src/lib"
code, raw = run(cmd)
save("search_def_parsebudget", "sum", "search", raw, [], [f"budget.ts:{line_of(SERVER + '/src/lib/budget.ts', 'export function parseBudget')}"],
     cmd, code, goal="Which file and line defines the function parseBudget?")

cmd = "rg -n 'keyless' src -g '!**/__tests__/**' -C 2"
code, raw = run(cmd)
save("search_keyless_ctx", "sum", "search", raw, [["kilo"], ["ovh"]], [], cmd, code,
     goal="Which provider platform ids are registered with keyless: true (give the platform ids)?")

# ---------- ask（读文件答问题） ----------
for cid, rel, q, req, needle in [
    ("ask_config_port", "src/lib/config.ts",
     "What are the default port and default bind host when PORT and HOST are not set, and on which lines?",
     [["3001"], ["::"]], "PORT ?? 3001"),
    ("ask_catalog_interval", "src/services/catalog-sync.ts",
     "How often does the catalog sync run, what is the constant called, and on which line is it defined?",
     [["12"], ["SYNC_INTERVAL_MS"]], "const SYNC_INTERVAL_MS"),
    ("ask_setup_code", "src/lib/setup-code.ts",
     "Which exported function mints the first-run setup code, and on which line is it defined?",
     [["generateSetupCode"]], "export function generateSetupCode"),
]:
    path = os.path.join(SERVER, rel)
    save(cid, "ask", "file_qa", open(path, encoding="utf-8").read(), req,
         [f"{os.path.basename(rel)}:{line_of(path, needle)}"], question=q, files=["server/" + rel])

# ---------- git ----------
run("git fetch -q --depth 80 origin main", REPO)
cmd = "git log --oneline -60 origin/main"
code, raw = run(cmd, REPO)
hs = [ln.split()[0] for ln in raw.splitlines() if "retention" in ln.lower()][:6]
save("git_oneline_retention", "sum", "git", raw, [[h] for h in hs] or [["none"]], [], cmd, code,
     goal="Which of these commits are about retention? Give their short hashes.")

_, target = run("git log --format=%h -1 origin/main -- server/src/lib", REPO)
target = target.strip().split()[0]
cmd = f"git show --stat {target}"
code, raw = run(cmd, REPO)
files_changed = [os.path.basename(f) for f in re.findall(r"^\s(server/src/lib/\S+?)\s+\|", raw, re.M)
                 if "__tests__" not in f][:5]
save("git_show_stat", "sum", "git", raw, [[f] for f in files_changed] or [["lib"]], [], cmd, code,
     goal="Which non-test source files under server/src/lib did this commit change?")

# ---------- explain（代码推理，答案由真实运行得出） ----------
for cid, rel, imp, expr, q in [
    ("explain_parsebudget", "src/lib/budget.ts", "import {parseBudget} from './src/lib/budget.ts'", "parseBudget('1.5M')",
     "What exactly does parseBudget('1.5M') return? Give the exact value."),
    ("explain_routedvia", "src/lib/header-value.ts", "import {routedViaValue} from './src/lib/header-value.ts'",
     "routedViaValue('kilo', 'nvidia/nemotron:free')",
     "What exactly does routedViaValue('kilo', 'nvidia/nemotron:free') return? Give the exact string."),
    ("explain_maxtokens", "src/lib/output-cap.ts", "import {parseMaxTokensCeiling} from './src/lib/output-cap.ts'",
     "parseMaxTokensCeiling('max_tokens: 9000 > 8192, which is the maximum allowed')",
     "What exactly does parseMaxTokensCeiling('max_tokens: 9000 > 8192, which is the maximum allowed') return?"),
]:
    try:
        value = tsx_eval(imp, expr)
    except RuntimeError as e:
        print(f"skip {cid}: {e}")
        continue
    v = value.strip('"')
    save(cid, "ask", "explain", open(os.path.join(SERVER, rel), encoding="utf-8").read(), [[v]], [], question=q,
         files=["server/" + rel], extra={"truth": value})

# ---------- testgen（写测试：干净代码要过，注入 bug 后要失败） ----------
tasks = {t["id"]: t for t in json.load(open("/tmp/iu_tasks.json", encoding="utf-8"))}
for cid, rel, fn, mutant in [
    ("testgen_outputcap", "src/lib/output-cap.ts", "parseMaxTokensCeiling", None),
    ("testgen_headervalue", "src/lib/header-value.ts", "safeHeaderValue", "t11_header-value"),
]:
    path = os.path.join(SERVER, rel)
    if mutant:
        t = tasks[mutant]
        mut = {"line": t["line"], "old": t["old"], "new": t["new"]}
    else:  # 在函数体里找一个让现有测试失败的比较符变异
        lines = open(path, encoding="utf-8").read().split("\n")
        start = line_of(path, f"export function {fn}")
        mut = None
        for i in range(start, min(start + 15, len(lines))):
            for old, new in ((" >= ", " > "), (" > ", " >= "), (" <= ", " < "), (" < ", " <= "), (" === ", " !== ")):
                if old in lines[i]:
                    mut = {"line": i + 1, "old": lines[i], "new": lines[i].replace(old, new, 1)}
                    break
            if mut:
                break
    if not mut:
        print(f"skip {cid}: no mutant")
        continue
    save(cid, "testgen", "testgen", open(path, encoding="utf-8").read(), [], [],
         question=(f"Write a Vitest test file (TypeScript) for the exported function {fn} in {rel}. "
                   f"It will be saved as server/src/__tests__/lib/_gen.test.ts, so import it with "
                   f"`import {{ {fn} }} from '../../lib/{os.path.basename(rel)[:-3]}.js';`. Cover normal inputs and "
                   f"edge cases with exact expected values. Reply with only one ```ts code block."),
         files=["server/" + rel], extra={"fn": fn, "rel": rel, "mutant": mut})

# ---------- build（更多测试日志） ----------
t = tasks["t06_url-guard"]
p = os.path.join(REPO, t["file"])
src = open(p, encoding="utf-8").read().split("\n")
src[t["line"] - 1] = t["new"]
open(p, "w", encoding="utf-8").write("\n".join(src))
cmd = "npx vitest run src/__tests__/lib --reporter=verbose"
code, raw = run(cmd)
fails = re.search(r"Tests\s+(\d+) failed", raw)
save("vitest_fail_many", "sum", "build", raw, [[f"{fails.group(1)} failed"]] if fails else [["failed"]],
     [f"url-guard.ts:{t['line']}"] if f"url-guard.ts:{t['line']}" in raw else [], cmd, code)
subprocess.run(["git", "checkout", "--", t["file"]], cwd=REPO)
print("done")
