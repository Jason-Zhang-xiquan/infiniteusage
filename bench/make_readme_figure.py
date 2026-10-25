"""README 顶上那张对比图：Claude Code、Codex 两家 × 「不记笔记」/「装了 InfiniteUsage」，用跨会话失忆鹈鹕测试画出来的图拼成。

用法: python bench/make_readme_figure.py   →  assets/amnesia.png
数据：bench/results/pelican/*_cross_plain_{X,H}_1.png，分数来自 cross_runs.jsonl（docs/code_map_feasibility.md）
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "results", "pelican")
OUT = os.path.join(os.path.dirname(HERE), "assets", "amnesia.png")
ROWS = [("Claude Code", "claude", "1/16", "16/16"), ("Codex", "codex", "2/16", "16/16")]  # 宿主只有这两家
W, H = 480, 360  # 每张缩成这么大
LABEL_W, HEAD_H, GAP = 300, 90, 16
BG, FG, BAD, GOOD = (16, 18, 24), (235, 238, 245), (255, 107, 107), (61, 220, 151)


def font(size, bold=False):
    for name in (("arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("arial.ttf", "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main():
    width = LABEL_W + 2 * W + 3 * GAP
    height = HEAD_H + len(ROWS) * (H + GAP) + 80
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    d.text((LABEL_W + GAP + W // 2, 30), "New session, no notes", font=font(30, True), fill=BAD, anchor="mm")
    d.text((LABEL_W + 2 * GAP + W + W // 2, 30), "With InfiniteUsage", font=font(30, True), fill=GOOD, anchor="mm")
    d.text((LABEL_W + GAP + W, 68), "same 16 requirements agreed in the previous session  ·  score = requirements kept",
           font=font(20), fill=FG, anchor="mm")
    for i, (name, host, bad, good) in enumerate(ROWS):
        y = HEAD_H + i * (H + GAP)
        d.text((GAP, y + H // 2 - 40), name, font=font(32, True), fill=FG, anchor="lm")
        d.text((GAP, y + H // 2 + 10), bad, font=font(40, True), fill=BAD, anchor="lm")
        d.text((GAP + 105, y + H // 2 + 10), "→", font=font(40, True), fill=FG, anchor="lm")
        d.text((GAP + 160, y + H // 2 + 10), good, font=font(40, True), fill=GOOD, anchor="lm")
        for j, arm in enumerate(("X", "H")):
            p = Image.open(os.path.join(SRC, f"{host}_cross_plain_{arm}_1.png")).convert("RGB").resize((W, H))
            img.paste(p, (LABEL_W + GAP + j * (W + GAP), y))
    d.text((width // 2, height - 48), "\"Draw a pelican riding a bicycle\" in a fresh session.", font=font(20), fill=FG, anchor="mm")
    d.text((width // 2, height - 20), "Without notes every agent forgets the spec. With InfiniteUsage it is loaded back automatically.",
           font=font(20), fill=FG, anchor="mm")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT, optimize=True)
    print(OUT)


if __name__ == "__main__":
    main()
