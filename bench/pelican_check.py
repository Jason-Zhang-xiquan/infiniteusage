"""「失忆鹈鹕」检查器：读 pelican.svg，按 12 条细节打分（只用正则和计数，不调用模型），可选渲染成 PNG、拼四宫格。
规格见 docs/visual_compaction_test_plan.md。

用法：
  python3 bench/pelican_check.py <pelican.svg>                 打分
  python3 bench/pelican_check.py --render <pelican.svg> <out.png>
  python3 bench/pelican_check.py --grid <runs.json> <out.png>  四宫格（Chrome 截图）
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import tempfile

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ITEMS = [
    ("canvas", "画布 800×600"),
    ("scarf", "围巾 #E4572E"),
    ("frame", "车架 #8338EC"),
    ("sun", "太阳 #FFB703 在左上"),
    ("sky", "天空渐变 #FFD166→#06D6A0"),
    ("sign", "有路牌文字"),
    ("fish", "车筐里的鱼 #3A86FF"),
    ("birds", "5 只鸟 #2B2D42"),
    ("spokes", "12 根辐条 #5C677D"),
    ("dash", "地面虚线 12 8"),
    ("sign_new", "路牌是改过的 IU-43"),
    ("no_helmet", "没有头盔（已撤销）"),
]
TAG = re.compile(r"<(/?)([a-zA-Z]+)\b([^>]*?)(/?)>", re.S)
ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"|([\w:-]+)\s*=\s*\'([^\']*)\'')


def attrs(s: str) -> dict:
    out = {}
    for m in ATTR.finditer(s):
        k, v = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        out[k.lower()] = v
    for decl in out.get("style", "").split(";"):  # style="fill:#..;stroke:#.."
        if ":" in decl:
            k, v = decl.split(":", 1)
            out.setdefault(k.strip().lower(), v.strip())
    return out


def num(v, default=None):
    m = re.match(r"\s*(-?[\d.]+)", v or "")
    return float(m.group(1)) if m else default


def has_color(svg: str, hexcode: str) -> bool:
    return hexcode.lower() in svg.lower()


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)  # SVG 的 matrix(a b c d e f)：x' = a·x + c·y + e，y' = b·x + d·y + f


def mul(m, n):
    """先做 n 再做 m"""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2, a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def parse_transform(s: str):
    """translate / scale / rotate / matrix，从左到右复合；skew 之类不认识的当没有"""
    import math
    m = IDENTITY
    for name, body in re.findall(r"(\w+)\s*\(([^)]*)\)", s or ""):
        v = [float(x) for x in re.findall(r"-?\d*\.?\d+(?:e-?\d+)?", body)]
        name = name.lower()
        if name == "translate" and v:
            n = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale" and v:
            n = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate" and v:
            r = math.radians(v[0])
            n = (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0)
            if len(v) >= 3:  # rotate(a, cx, cy) = translate(cx,cy) rotate(a) translate(-cx,-cy)
                n = mul(mul((1, 0, 0, 1, v[1], v[2]), n), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "matrix" and len(v) == 6:
            n = tuple(v)
        else:
            continue
        m = mul(m, n)
    return m


def apply(m, p):
    return (m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5])


def center(a):
    """圆 / 椭圆的圆心，换算成画布坐标"""
    return apply(a.get("_m", IDENTITY), (num(a.get("cx"), 0), num(a.get("cy"), 0)))


def elements(svg: str) -> list:
    """[(标签, 属性)]；没写 stroke 的元素继承外层 <g> 的 stroke；外层 <g> 和自己的 transform 复合后放在 a["_m"]"""
    out, stack = [], []  # stack: [(stroke, 累计的 transform)]
    for m in TAG.finditer(svg):
        close, t, body, selfclose = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if close:
            if t == "g" and stack:
                stack.pop()
            continue
        a = attrs(body)
        outer_stroke, outer_m = stack[-1] if stack else (None, IDENTITY)
        if "stroke" not in a and outer_stroke:
            a["stroke"] = outer_stroke
        a["_m"] = mul(outer_m, parse_transform(a.get("transform"))) if a.get("transform") else outer_m
        if t == "g" and not selfclose:
            stack.append((a.get("stroke") or outer_stroke, a["_m"]))
        out.append((t, a))
    return out


def stroked(elems, color):
    return [(t, a) for t, a in elems if t in ("path", "line", "polyline")
            and (a.get("stroke") or "").lower() == color.lower()]


def segments(t, a) -> list:
    """每一笔的 (起点, 下一个点)，画布坐标（换算过 transform）"""
    m = a.get("_m", IDENTITY)
    return [(apply(m, s), apply(m, e)) for s, e in local_segments(t, a)]


def local_segments(t, a) -> list:
    """每一笔的 (起点, 下一个点)：line 一笔；path 每个 M 开始的子路径算一笔（相对坐标换算成绝对）"""
    if t == "line":
        return [((num(a.get("x1"), 0), num(a.get("y1"), 0)), (num(a.get("x2"), 0), num(a.get("y2"), 0)))]
    if t == "polyline":
        v = [float(x) for x in re.findall(r"-?[\d.]+", a.get("points", ""))]
        return [((v[0], v[1]), (v[2], v[3]))] if len(v) >= 4 else []
    out, cur = [], (0.0, 0.0)
    for cmd, body in re.findall(r"([MmLlHhVvCcSsQqTtAaZz])([^MmLlHhVvCcSsQqTtAaZz]*)", a.get("d", "")):
        v = [float(x) for x in re.findall(r"-?\d*\.?\d+(?:e-?\d+)?", body)]
        if cmd in "Mm" and len(v) >= 2:
            start = (v[0], v[1]) if cmd == "M" or not out else (cur[0] + v[0], cur[1] + v[1])
            cur = start
            out.append([start, None])
            if len(v) >= 4:  # M x y x2 y2：后面的坐标是隐式 L
                out[-1][1] = (v[2], v[3]) if cmd == "M" else (start[0] + v[2], start[1] + v[3])
        elif out and out[-1][1] is None and len(v) >= 2:
            if cmd in "LTCSQA":
                out[-1][1] = (v[-2], v[-1]) if cmd != "A" else (v[5], v[6])
            elif cmd in "ltcsqa":
                d = (v[-2], v[-1]) if cmd != "a" else (v[5], v[6])
                out[-1][1] = (cur[0] + d[0], cur[1] + d[1])
        elif out and out[-1][1] is None and cmd in "HhVv" and v:
            out[-1][1] = ((v[0] if cmd == "H" else cur[0] + v[0]), cur[1]) if cmd in "Hh" else \
                (cur[0], (v[0] if cmd == "V" else cur[1] + v[0]))
    return [(s, e or s) for s, e in out]


def subpaths(t, a):
    if t == "path":
        return max(1, len(re.findall(r"[Mm]", a.get("d", ""))))
    if t in ("line", "polyline"):
        return 1
    return 0


def check(svg: str, level: str = "easy") -> dict:
    low = svg.lower()
    elems = elements(svg)
    res = {}
    root = next((a for t, a in elems if t == "svg"), {})
    vb = re.sub(r"[\s,]+", " ", root.get("viewbox", "")).strip()
    res["canvas"] = vb == "0 0 800 600" or (num(root.get("width")) == 800 and num(root.get("height")) == 600)
    res["scarf"] = has_color(svg, "#E4572E")
    res["frame"] = has_color(svg, "#8338EC")
    suns = [a for t, a in elems if t in ("circle", "ellipse") and a.get("fill", "").lower() == "#ffb703"]
    res["sun"] = any(center(a)[0] < 267 and center(a)[1] < 200 for a in suns)
    grads = re.findall(r"<(?:linear|radial)Gradient\b.*?</(?:linear|radial)Gradient>", svg, re.S | re.I)
    res["sky"] = any("#ffd166" in g.lower() and "#06d6a0" in g.lower() for g in grads)
    texts = " ".join(re.findall(r"<text\b[^>]*>(.*?)</text>", svg, re.S | re.I))
    texts = re.sub(r"<[^>]+>", "", texts)  # <tspan> 里的字
    res["sign"] = bool(re.search(r"IU-4\d", texts))
    res["fish"] = has_color(svg, "#3A86FF")
    # 模型常把规格里的颜色顺手用在别处（鹈鹕描边、地面线），所以按位置数：天上（y≤220、不闭合）的才算鸟，从车轮中心出发的才算辐条
    birds = [s for t, a in stroked(elems, "#2B2D42") if "z" not in a.get("d", "").lower()  # 闭合形状是鹈鹕的嘴、身体
             for s in segments(t, a)[:1] if s[0][1] <= 220]
    res["birds"] = len(birds) in (5, 10)  # 一只鸟画成一笔或左右两笔都算
    wheels = [(*center(a), num(a.get("r") or a.get("rx"), 0)) for t, a in elems
              if t in ("circle", "ellipse") and num(a.get("r") or a.get("rx"), 0) >= 25 and center(a)[1] > 250]
    segs = [s for t, a in stroked(elems, "#5C677D") for s in segments(t, a)]
    if wheels:
        near = lambda p, w: (p[0] - w[0]) ** 2 + (p[1] - w[1]) ** 2 <= 64
        spokes = 0
        for a, b in segs:
            for w in wheels:
                if near(a, w) or near(b, w):
                    spokes += 1
                    break
                if near(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), w):  # 穿过轮心的一条直径算两根
                    spokes += 2
                    break
        res["spokes"] = spokes == 12
    else:
        res["spokes"] = len(segs) == 12
    res["dash"] = any(re.sub(r"[\s,]+", " ", a.get("stroke-dasharray", "")).strip() == "12 8" for _, a in elems)
    res["sign_new"] = "IU-43" in texts and "IU-42" not in texts
    res["no_helmet"] = "#2e86ab" not in low
    if level == "easy":
        return res
    # 加难版：同一细节改过几次只认最终值，旧值不能出现；外加几条琐碎细节
    signs = set(re.findall(r"IU-4\d", texts))
    clouds = any(a.get("fill", "").lower() == "#f1faee" and 0.8 in (num(a.get("opacity")), num(a.get("fill-opacity")))
                 for _, a in elems) or bool(re.search(r'<g[^>]*opacity="0?\.8"[^>]*>(?:(?!</g>).)*#f1faee', low, re.S))
    hard = {"canvas": res["canvas"], "scarf": has_color(svg, "#F95738") and not has_color(svg, "#E4572E"),
            "frame": res["frame"], "sun": res["sun"],
            "rays": sum(len(segments(t, a)) for t, a in stroked(elems, "#FB8500")) == 8, "sky": res["sky"],
            "sign": signs == {"IU-46"}, "fish": has_color(svg, "#00B4D8") and not has_color(svg, "#3A86FF"),
            "birds": len(birds) in (7, 14), "spokes": res["spokes"], "dash": res["dash"], "no_helmet": res["no_helmet"],
            "eye": has_color(svg, "#0B132B"), "ladybug": has_color(svg, "#D7263D"), "cloud": clouds,
            "comment": bool(re.search(r"<!--\s*iu-pelican-v3\s*-->", svg))}
    return hard


ITEMS_HARD = [
    ("canvas", "画布 800×600"), ("scarf", "围巾改成 #F95738"), ("frame", "车架 #8338EC"), ("sun", "太阳在左上"),
    ("rays", "太阳 8 道光芒 #FB8500"), ("sky", "天空渐变"), ("sign", "路牌最终是 IU-46"), ("fish", "鱼改成 #00B4D8"),
    ("birds", "鸟改成 7 只"), ("spokes", "12 根辐条"), ("dash", "地面虚线 12 8"), ("no_helmet", "没有头盔"),
    ("eye", "眼睛 #0B132B"), ("ladybug", "路牌上的瓢虫 #D7263D"), ("cloud", "云 #F1FAEE 透明度 0.8"),
    ("comment", "注释 iu-pelican-v3"),
]


def score(path: str, level: str = "easy") -> dict:
    items = ITEMS if level == "easy" else ITEMS_HARD
    if not path or not os.path.exists(path):
        return {"ok": False, "score": 0, "total": len(items), "missing": [x[1] for x in items], "detail": {},
                "error": "没有生成 pelican.svg"}
    svg = open(path, encoding="utf-8", errors="replace").read()
    if "<svg" not in svg.lower():
        return {"ok": False, "score": 0, "total": len(items), "missing": [x[1] for x in items], "detail": {},
                "error": "不是 SVG"}
    r = check(svg, level)
    return {"ok": True, "score": sum(r.values()), "total": len(items), "missing": [lab for k, lab in items if not r[k]],
            "detail": r}


def chrome_shot(url: str, out: str, w: int, h: int):
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={w},{h}",
                    f"--screenshot={os.path.abspath(out)}", url], stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=120)


def render(svg_path: str, out: str):
    chrome_shot("file://" + os.path.abspath(svg_path), out, 800, 600)


def grid(runs: list, out: str):
    """runs: [{"label", "svg", "score", "missing", "cost"}]，拼成一排卡片：图、分数、用量柱"""
    top = max([r.get("cost") or 0 for r in runs] + [1])
    cards = []
    for r in runs:
        img = f'<img src="file://{html.escape(os.path.abspath(r["svg"]))}">' if os.path.exists(r["svg"]) else \
            '<div class="none">没有画出来</div>'
        bar = (r.get("cost") or 0) / top * 100
        miss = "".join(f"<li>{html.escape(m)}</li>" for m in r.get("missing", []))
        cards.append(f'<div class="card"><h2>{html.escape(r["label"])}</h2><div class="pic">{img}</div>'
                     f'<div class="score">{r["score"]}/{r.get("total", 12)}</div>'
                     f'<div class="bar"><span style="width:{bar:.0f}%"></span></div>'
                     f'<div class="cost">{html.escape(r.get("cost_label", ""))}</div><ul>{miss}</ul></div>')
    page = """<html><head><meta charset="utf-8"><style>
body{margin:0;padding:24px;background:#111;color:#eee;font-family:-apple-system,"PingFang SC",sans-serif;display:flex;gap:18px}
.card{flex:1;background:#1c1c1c;border-radius:14px;padding:14px}
h2{margin:0 0 10px;font-size:22px}.pic{background:#fff;border-radius:8px;aspect-ratio:4/3;overflow:hidden}
.pic img{width:100%;height:100%;object-fit:contain}.none{color:#900;padding:40px;text-align:center}
.score{font-size:46px;font-weight:700;margin-top:10px}.bar{height:12px;background:#333;border-radius:6px}
.bar span{display:block;height:100%;background:#f7b733;border-radius:6px}.cost{font-size:15px;color:#aaa;margin:6px 0}
ul{margin:4px 0 0 18px;padding:0;font-size:14px;color:#f88}</style></head><body>""" + "".join(cards) + "</body></html>"
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(page)
    f.close()
    chrome_shot("file://" + f.name, out, 420 * len(runs) + 60, 760)
    os.remove(f.name)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--render":
        render(a[1], a[2])
    elif a and a[0] == "--grid":
        grid(json.load(open(a[1], encoding="utf-8")), a[2])
    else:
        print(json.dumps(score(a[0]), ensure_ascii=False, indent=1))
