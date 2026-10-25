"""基准 2：外包压缩的质量。对 bench/corpus 里每条语料跑几种「交给 agent 的内容」，自动打分。

组（arm）：
  raw_full   原样全部（信息最全，最贵）
  raw_30k    Claude Code 实际看到的样子：Bash 输出超过 3 万字符会被截断（假设保留前后各 1.5 万字符，未核实）
  det        确定性过滤，不用模型：错误/失败/警告行及其后 2 行 + 目标关键词所在行 + 最后 20 行，最多 6000 字符
  llm:<模型> 经 FreeLLMAPI 交给免费模型（iu 的 summarize_text / ask_text），每个模型重复 N 次

分数：
  recall     标准答案里的必需内容和位置（文件:行）命中了多少
  幻觉位置   输出里出现、但原始输出里根本不存在的「文件:行」
  字符 / 压缩比 / 估算 token（2.5 字符/token，按用户日志拟合的 ASCII 系数）
用法: python bench/run_bench.py [--repeats 3] [--models auto,qwen3.5-397b,...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from iu import offload  # noqa: E402

CORPUS = os.path.join(ROOT, "bench", "corpus")
RESULTS = os.path.join(ROOT, "bench", "results")
CHARS_PER_TOKEN = 2.5
from iu.det import det_filter  # noqa: E402


def truncate_30k(raw: str) -> str:
    if len(raw) <= 30000:
        return raw
    return raw[:15000] + f"\n[... {len(raw) - 30000} characters truncated ...]\n" + raw[-15000:]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower())


def loc_hit(loc: str, out: str, kind: str = "sum") -> bool:
    m = re.match(r"(.+?)[:(](\d+)", loc)
    name, num = m.group(1), m.group(2)
    if kind == "ask":  # 读单个文件回答问题：只看行号（输出里可能写 line 280 或 280:）
        return re.search(r"\b" + num + r"\b", out) is not None
    return re.search(re.escape(name) + r"[^\n]{0,40}?\b" + num + r"\b", out) is not None


def alt_hit(alt: str, n_out: str) -> bool:
    """「N failed」这类数量：数字和 fail 靠在一起就算（模型常写 "11 tests failed"、"Failed: 11"）"""
    m = re.fullmatch(r"(\d+) failed", alt.strip())
    if m:
        n = m.group(1)
        return re.search(rf"\b{n}\b[^\n]{{0,25}}fail|fail[^\n]{{0,25}}\b{n}\b", n_out) is not None
    return norm(alt) in n_out


def score(gold: dict, out: str, raw: str):
    n_out = norm(out)
    req_hits = [any(alt_hit(alt, n_out) for alt in group) for group in gold["required"]]
    loc_hits = [loc_hit(loc, out, gold["kind"]) for loc in gold["locations"]]
    total = len(req_hits) + len(loc_hits)
    recall = (sum(req_hits) + sum(loc_hits)) / total if total else 1.0
    halluc = 0
    if gold["kind"] == "sum":
        for name, num in re.findall(r"([\w\-.]+\.(?:ts|tsx|js|mjs|py|json))\s*(?::|\(|,?\s*line\s+)(\d+)", out):
            if not re.search(re.escape(name) + r"\s*[:(]\s*" + num + r"\b", raw):
                halluc += 1
    return recall, halluc, [g[0] for g, h in zip(gold["required"], req_hits) if not h] + \
        [loc for loc, h in zip(gold["locations"], loc_hits) if not h]


def numbered_file(gold: dict, raw: str) -> str:
    name = gold["files"][0] if gold["files"] else "file"
    return f"=== {name} ===\n" + "\n".join(f"{i}: {line}" for i, line in enumerate(raw.splitlines(), 1))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--models", default="auto,qwen3.5-397b,nemotron-3-ultra-550b,kilo-auto")
    ap.add_argument("--only", help="只跑这些语料 id，逗号分隔")
    ap.add_argument("--guard", action="store_true", help="免费模型的摘要再过一遍 iu 的护栏（编造就改用规则过滤，漏掉的关键原文行补上）")
    ap.add_argument("--skip-baselines", action="store_true", help="不跑 raw/det 这些不用模型的组")
    args = ap.parse_args()
    os.makedirs(RESULTS, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(RESULTS, f"quality_{stamp}.jsonl")
    items = sorted(os.listdir(CORPUS))
    if args.only:
        items = [i for i in items if i in args.only.split(",")]
    golds = {i: json.load(open(os.path.join(CORPUS, i, "gold.json"), encoding="utf-8")) for i in items}
    raws = {i: open(os.path.join(CORPUS, i, "raw.txt"), encoding="utf-8").read() for i in items}
    models = [m for m in args.models.split(",") if m]
    last_call = {}

    def record(rec):
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"{rec['item']:<15} {rec['arm']:<26} rep{rec['rep']} recall {rec['recall']:.2f} halluc {rec['halluc']} "
              f"{rec['chars']:>7} chars {rec.get('seconds', 0):>5}s {rec.get('routed', '')} {rec.get('error', '')[:80]}",
              flush=True)

    # 不用模型的组：各跑一次
    for i in ([] if args.skip_baselines else items):
        g, raw = golds[i], raws[i]
        given = numbered_file(g, raw) if g["kind"] == "ask" else raw
        arms = {"raw_full": given}
        if g["kind"] == "sum":
            arms["raw_30k"] = truncate_30k(raw)
        arms["det"] = det_filter(raw, g.get("goal") or g.get("question") or "", numbered=(g["kind"] == "ask"))
        for arm, text in arms.items():
            r, h, miss = score(g, text, raw)
            record({"item": i, "kind": g["kind"], "arm": arm, "rep": 0, "recall": r, "halluc": h, "missing": miss,
                    "chars": len(text), "raw_chars": len(raw)})

    # 免费模型组
    for rep in range(1, args.repeats + 1):
        for i in items:
            g, raw = golds[i], raws[i]
            for m in models:
                if m != "auto":  # 指定单个模型时，同一模型两次调用至少隔 25 秒（OVH 匿名线路每模型每分钟 2 次）
                    wait = 25 - (time.time() - last_call.get(m, 0))
                    if wait > 0:
                        time.sleep(wait)
                last_call[m] = time.time()
                t0 = time.time()
                arm = f"llm:{m}+guard" if args.guard else f"llm:{m}"
                rec = {"item": i, "kind": g["kind"], "arm": arm, "rep": rep, "raw_chars": len(raw)}
                try:
                    if g["kind"] == "ask":
                        text, res, sent = offload.ask_text(g["question"], numbered_file(g, raw), model=m)
                    else:
                        text, res, sent = offload.summarize_text(g["command"], g["exit"], raw, goal=g.get("goal", ""),
                                                                 model=m)
                    if args.guard and g["kind"] == "sum":
                        text, problem = offload.guard_summary(text, raw)
                        if problem:
                            text = det_filter(raw, g.get("goal", ""))
                            rec["fallback"] = problem
                    u = res.get("usage") or {}
                    r, h, miss = score(g, text, raw)
                    rec.update(recall=r, halluc=h, missing=miss, chars=len(text), seconds=round(time.time() - t0, 1),
                               routed=res.get("_routed"), prompt_tokens=u.get("prompt_tokens"),
                               completion_tokens=u.get("completion_tokens"), output=text)
                except SystemExit as e:
                    rec.update(recall=0.0, halluc=0, missing=["(调用失败)"], chars=0, seconds=round(time.time() - t0, 1),
                               error=str(e))
                record(rec)
    print("结果:", out_path)


if __name__ == "__main__":
    main()
