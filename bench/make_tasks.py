"""阶段 B 任务集：在 FreeLLMAPI 源码（公开 MIT 项目）的干净副本里，对 server/src/lib 下有单元测试的源文件做小变异，
挑出「让 1~10 个测试失败」的变异，每个文件最多一个，凑够 N 个任务。只改临时副本，不碰正在运行的服务。

用法（Mac）: python3 make_tasks.py /tmp/flbench_clean /tmp/iu_tasks.json [--n 12]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

REPO, OUT = sys.argv[1], sys.argv[2]
N = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 12
SERVER = os.path.join(REPO, "server")
ENV = {**os.environ, "PATH": "/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:" + os.environ["PATH"],
       "NO_COLOR": "1", "CI": "1"}

FILES = ["key-parser", "think-tags", "tool-args", "tool-call-rescue", "tool-validate", "url-guard", "sampling-params",
         "output-cap", "provider-size-parser", "served-model", "header-value", "log-redaction", "error-redaction",
         "task-type", "budget", "structured-output", "system-prompt", "guardrails", "provider-timeout", "content",
         "endpoint-scope", "client-classifier", "wake-detect", "ttfb-budget"]

OPS = [(" <= ", " < "), (" >= ", " > "), (" < ", " <= "), (" > ", " >= "), (" === ", " !== "), (" !== ", " === "),
       (" && ", " || "), (".trim()", ""), (".toLowerCase()", "")]
NUM = re.compile(r"(?<![\w.])([2-9]|[1-9]\d{1,3})(?![\w.])")


def vitest(files):
    p = subprocess.run(["npx", "vitest", "run", *files], cwd=SERVER, capture_output=True, env=ENV, timeout=300)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    m = re.search(r"Tests\s+(?:(\d+) failed \| )?(\d+) passed", out)
    failed = int(m.group(1) or 0) if m else -1
    return p.returncode, failed, out


def candidates(lines):
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith(("//", "*", "/*", "import ", "export type", "type ")) or "console." in s:
            continue
        for old, new in OPS:
            if old in line and not (old in (" < ", " > ") and ("<" in line and ">" in line and "=>" not in line)):
                yield i, line.replace(old, new, 1), f"'{old.strip()}' → '{new.strip() or '(删掉)'}'"
        m = NUM.search(line.split("//")[0])
        if m and "'" not in line[: m.start()] and '"' not in line[: m.start()]:
            n = int(m.group(1))
            yield i, line[: m.start()] + str(n + 1) + line[m.end():], f"常量 {n} → {n + 1}"


def main():
    code, failed, out = vitest(["src/__tests__/lib"])
    print(f"干净代码：lib 测试 exit={code} failed={failed}", flush=True)
    if code != 0:
        sys.exit("干净代码的 lib 测试就不过，换个版本")
    tasks = []
    for name in FILES:
        if len(tasks) >= N:
            break
        src = os.path.join(SERVER, "src", "lib", name + ".ts")
        tests = sorted(f"src/__tests__/lib/{t}" for t in os.listdir(os.path.join(SERVER, "src/__tests__/lib"))
                       if t.startswith(name) and t.endswith(".test.ts"))
        if not os.path.exists(src) or not tests:
            continue
        original = open(src, encoding="utf-8").read()
        lines = original.split("\n")
        tried = 0
        for i, mutated, desc in candidates(lines):
            if tried >= 12:
                break
            tried += 1
            new_lines = list(lines)
            new_lines[i] = mutated
            open(src, "w", encoding="utf-8").write("\n".join(new_lines))
            try:
                code, failed, _ = vitest(tests)
            except subprocess.TimeoutExpired:
                code, failed = -1, -1
            finally:
                open(src, "w", encoding="utf-8").write(original)
            if code != 0 and 1 <= failed <= 10:
                tasks.append({"id": f"t{len(tasks) + 1:02d}_{name}", "file": f"server/src/lib/{name}.ts", "line": i + 1,
                              "old": lines[i], "new": mutated, "mutation": desc, "tests": tests, "failed": failed})
                print(f"✓ {name}.ts:{i + 1} {desc} → {failed} 个测试失败", flush=True)
                break
        else:
            print(f"· {name}.ts 没找到合适的变异", flush=True)
    json.dump(tasks, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"共 {len(tasks)} 个任务 → {OUT}")


if __name__ == "__main__":
    main()
