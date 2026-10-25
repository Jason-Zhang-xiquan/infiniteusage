"""基准 2 语料：在 Mac 上 FreeLLMAPI 源码的临时副本（/tmp/flbench，APFS 克隆，不碰正在运行的服务）里
注入已知错误，跑真实的测试、类型检查、lint、构建、搜索、git，收集原始输出和标准答案。

FreeLLMAPI 是公开的 MIT 开源项目，所以这些输出可以发给会训练的免费模型，不涉及用户私有代码。

每条语料：corpus/<id>/raw.txt（命令输出）、gold.json：
  kind      sum（压缩命令输出）/ ask（读文件回答问题）
  command   跑的命令；goal（可选）agent 想从输出里找什么
  required  必须出现的内容：[[任选其一的写法...], ...]，大小写和空白不敏感
  locations 必须出现的位置（文件:行）
用法（Mac）: python3 make_corpus.py /tmp/flbench /tmp/iu_corpus
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

REPO, OUT = sys.argv[1], sys.argv[2]
SERVER = os.path.join(REPO, "server")
ENV = {**os.environ, "PATH": "/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:" + os.environ["PATH"],
       "NO_COLOR": "1", "FORCE_COLOR": "0", "CI": "1"}


def run(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, env=ENV)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def save(cid, kind, command, raw, exit_code, required, locations, goal="", question="", files=None):
    d = os.path.join(OUT, cid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "raw.txt"), "w", encoding="utf-8") as f:
        f.write(raw)
    with open(os.path.join(d, "gold.json"), "w", encoding="utf-8") as f:
        json.dump({"id": cid, "kind": kind, "command": command, "exit": exit_code, "goal": goal, "question": question,
                   "files": files or [], "required": required, "locations": locations}, f, ensure_ascii=False, indent=1)
    print(f"{cid}: exit {exit_code}, {len(raw)} chars, required {len(required)}, locations {len(locations)}")


def patch(path, old, new, count=1):
    s = open(path, encoding="utf-8").read()
    assert old in s, (path, old)
    open(path, "w", encoding="utf-8").write(s.replace(old, new, count))


def line_of(path, needle):
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        if needle in line:
            return i
    raise ValueError(needle)


os.makedirs(OUT, exist_ok=True)

# 1) vitest：整个 providers 测试目录（大量通过的噪音）里注入 2 个失败断言
test = os.path.join(SERVER, "src/__tests__/providers/aihorde.test.ts")
patch(test, "expect(provider.name).toBe('AI Horde');", "expect(provider.name).toBe('AI Hordes');")
patch(test, "expect(cap.body.max_tokens).toBe(16);", "expect(cap.body.max_tokens).toBe(17);")
l1, l2 = line_of(test, "toBe('AI Hordes')"), line_of(test, "toBe(17)")
cmd = "npx vitest run src/__tests__/providers --reporter=verbose"
code, raw = run(cmd, SERVER)
save("vitest_fail", "sum", cmd, raw, code,
     required=[["AI Hordes"], ["17"], ["2 failed"]],
     locations=[f"aihorde.test.ts:{l1}", f"aihorde.test.ts:{l2}"])
subprocess.run(["git", "checkout", "--", test], cwd=REPO)

# 2) vitest 全部通过（只该报告「全过」和数量，不该编造失败）
code, raw = run(cmd, SERVER)
m = re.search(r"Tests\s+(\d+) passed", raw)
save("vitest_pass", "sum", cmd, raw, code, required=[[m.group(1) if m else "passed"], ["pass"]], locations=[])

# 3) tsc：注入两个类型错误；--listFiles 让输出又长又杂（真实的冗长构建输出）
src = os.path.join(SERVER, "src/providers/aihorde.ts")
patch(src, "  keyless = true;", "  keyless = true;\n  badCount: number = 'three';")
with open(src, "a", encoding="utf-8") as f:
    f.write("\nexport const zz = undefinedHelperZz(1);\n")
la, lb = line_of(src, "badCount: number"), line_of(src, "undefinedHelperZz(1)")
cmd = "npx tsc --noEmit -p . --listFiles"
code, raw = run(cmd, SERVER)
save("tsc_errors", "sum", cmd, raw, code, required=[["TS2322"], ["TS2304"], ["undefinedHelperZz"]],
     locations=[f"aihorde.ts({la},", f"aihorde.ts({lb},"])

# 4) eslint：同一份带错的源码，再加一个没用到的变量
patch(src, "  badCount: number = 'three';", "  badCount: number = 'three';\n  private unusedThingQq = 42;")
cmd = "npx eslint src/providers src/lib --max-warnings=-1"
code, raw = run(cmd, SERVER)
lines = [ln for ln in raw.splitlines() if "error" in ln and re.match(r"\s+\d+:\d+", ln)]
save("eslint_errors", "sum", cmd, raw, code,
     required=[[re.sub(r"\s+", " ", ln.strip()).split(" ", 2)[2][:40]] for ln in lines[:4]] or [["error"]],
     locations=[])
subprocess.run(["git", "checkout", "--", src], cwd=REPO)

# 5) vite 构建成功但有警告（不该编造错误，要保住 500 kB 警告）
cmd = "npm run build -w client"
code, raw = run(cmd, REPO)
save("vite_build_ok", "sum", cmd, raw, code, required=[["500 kB", "500kb", "larger than 500"], ["built in"]], locations=[])

# 6) npm 安装一个不存在的包（verbose 日志很长）
tmp = "/tmp/iu_npmtest"
shutil.rmtree(tmp, ignore_errors=True)
os.makedirs(tmp)
run("npm init -y", tmp)
cmd = "npm install iu-definitely-not-real-pkg-zz9 --no-audit --no-fund --loglevel verbose"
code, raw = run(cmd, tmp)
save("npm_404", "sum", cmd, raw, code, required=[["404"], ["iu-definitely-not-real-pkg-zz9"]], locations=[])

# 7) Node 运行时崩溃：前面 60 行普通日志，嵌套调用里抛错
crash = "/tmp/iu_crash.js"
with open(crash, "w") as f:
    f.write("for (let i = 0; i < 60; i++) console.log(`[worker] processed batch ${i} ok, 128 items`);\n"
            "function parseConfig(cfg) { return cfg.limits.perMinute; }\n"
            "function startServer() { const cfg = { name: 'x' }; return parseConfig(cfg); }\n"
            "startServer();\n")
cmd = f"node {crash}"
code, raw = run(cmd, "/tmp")
save("node_crash", "sum", cmd, raw, code, required=[["perMinute"], ["TypeError"]],
     locations=["iu_crash.js:2", "iu_crash.js:3"])

# 8) 大搜索：rg 出几百行，agent 想知道哪些 provider 声明了免 key
cmd = "rg -n keyless src"
code, raw = run(cmd, SERVER)
_, truth = run("rg -n 'keyless = true|keyless: true' src --glob '!**/__tests__/**'", SERVER)
locs = []
for ln in truth.splitlines():
    m = re.match(r"src/(.+?):(\d+):", ln)
    if m:
        locs.append(f"{os.path.basename(m.group(1))}:{m.group(2)}")
save("rg_keyless", "sum", cmd, raw, code, required=[], locations=locs,
     goal="Which provider source files (not tests) declare a keyless provider (keyless = true or keyless: true), with line numbers?")

# 9) git log：最近 40 个提交，agent 想知道哪些提交改过 aihorde.ts
run("git fetch --depth 60 origin main", REPO)
cmd = "git log --stat -40 origin/main"
code, raw = run(cmd, REPO)
_, hashes = run("git log --format=%h -40 origin/main -- server/src/providers/aihorde.ts", REPO)
save("git_log", "sum", cmd, raw, code, required=[[h] for h in hashes.split()[:5]] or [["no commit"]], locations=[],
     goal="Which of these commits touched server/src/providers/aihorde.ts? Give their short hashes and subjects.")

# 10) 读大文件回答问题（ask）
idx = os.path.join(SERVER, "src/providers/index.ts")
text = open(idx, encoding="utf-8").read()
kilo_line = line_of(idx, "platform: 'kilo'")
ovh_line = line_of(idx, "platform: 'ovh'")
save("ask_index_kilo", "ask", "", text, 0, required=[["200"], ["kilo"]], locations=[f"index.ts:{kilo_line}"],
     question="What is the documented anonymous rate limit of the Kilo gateway, what is its platform id, and on which line is that platform declared?",
     files=["server/src/providers/index.ts"])
save("ask_index_ovh", "ask", "", text, 0, required=[["ovh"], ["2 req/min", "2 requests per minute", "2 req per min"]],
     locations=[f"index.ts:{ovh_line}"],
     question="Which platform id does OVHcloud AI Endpoints use, what anonymous rate limit is documented for it, and on which line is the platform declared?",
     files=["server/src/providers/index.ts"])
print("done")
