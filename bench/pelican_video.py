"""「失忆鹈鹕」视频素材：从 runs.jsonl 里挑 Claude 加难版同一次（--rep）的各组，生成
  timeline.png  每次请求的上下文曲线（压缩处标出来）
  story.html    竖屏 1080×1920 动画，直接录屏：各组上下文条一起涨、压缩时掉；最后出图、16 条细节逐条打勾、用量对比
  grid.png      各组的鹈鹕并排（没画出来的显示它最后说的话）
  summary.json  数字

用法（Mac）: python3 bench/pelican_video.py [--rep 2] [--out bench/results/pelican/video]
数据只读本机文件，不调用任何模型。
"""

from __future__ import annotations

import base64
import glob
import html
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pelican_check  # noqa: E402
import pelican_compact as pc  # noqa: E402

W = {"in": 1.0, "cw1h": 2.0, "cw5m": 1.25, "cr": 0.1, "out": 5.0}
ARMS = [("A", "不压缩", "#ff6b6b"), ("B", "压缩 7 次", "#ffb347"), ("C", "压缩 + 记笔记", "#3ddc97")]


def eq(u):
    cc = u.get("cache_creation") or {}
    c1h, c5 = cc.get("ephemeral_1h_input_tokens"), cc.get("ephemeral_5m_input_tokens")
    cw = (c1h or 0) * W["cw1h"] + (c5 or 0) * W["cw5m"] if (c1h is not None or c5 is not None) \
        else (u.get("cache_creation_input_tokens") or 0) * W["cw1h"]
    return ((u.get("input_tokens") or 0) * W["in"] + cw + (u.get("cache_read_input_tokens") or 0) * W["cr"]
            + (u.get("output_tokens") or 0) * W["out"])


def session_trace(sids: list) -> dict:
    """按请求顺序：上下文、等效 token、属于第几轮（用户消息），压缩发生在第几次请求之前"""
    ctx, cost, turn_of, compact_at, seen, turn = [], [], [], [], set(), -1
    for sid in sids:
        for p in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")):
            for line in open(p, encoding="utf-8", errors="replace"):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("subtype") == "compact_boundary":
                    compact_at.append(len(ctx))
                    continue
                m = d.get("message") or {}
                if d.get("type") == "user" and not d.get("isCompactSummary") and not d.get("isMeta"):
                    c = m.get("content")
                    if isinstance(c, str) or (isinstance(c, list) and not any(
                            isinstance(x, dict) and x.get("type") == "tool_result" for x in c)):
                        turn += 1
                    continue
                u = m.get("usage")
                if d.get("type") != "assistant" or not u or m.get("id") in seen:
                    continue
                seen.add(m.get("id"))
                ctx.append((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                           + (u.get("cache_creation_input_tokens") or 0))
                cost.append(round(eq(u)))
                turn_of.append(max(turn, 0))
    return {"ctx": ctx, "cost": cost, "turn": turn_of, "compact_at": compact_at}


def turn_labels(arm: str) -> list:
    out, k = [], 0
    for s in pc.schedule("hard", arm, 12):
        if s[0] == "say":
            out.append(["📝 定规格", "📝 加规格", "✏️ 改需求", "🔍 加小细节", "✏️ 再改", "✏️ 最后一改"][k])
            k += 1
        elif s[0] == "filler":
            out.append("📂 干别的活")
        elif s[0] == "final":
            out.append("🎨 画鹈鹕！")
    return out


def last_reply(run: dict) -> str:
    """最后一轮它说了什么（没画出图时放进视频）"""
    log = os.path.join(pc.WORK, f"claude_hard_{run['arm']}_{run['rep']}", "events.jsonl")
    text = ""
    if os.path.exists(log):
        for line in open(log, encoding="utf-8", errors="replace"):
            try:
                text = json.loads(line).get("result") or text
            except ValueError:
                pass
    return text


def pick(rep: int) -> dict:
    out = {}
    for x in open(os.path.join(pc.OUT_DIR, "runs.jsonl"), encoding="utf-8"):
        r = json.loads(x)
        if r.get("host") == "claude" and r.get("level") == "hard" and r.get("rep") == rep:
            out[r["arm"]] = r  # 同一组多次就取最后一次
    return out


def timeline_svg(tr: dict, w=1000, h=560) -> str:
    top = max(max(t["ctx"] or [1]) for t in tr.values()) * 1.1
    pad_l, pad_b, pad_t = 80, 50, 30
    color = {k: c for k, _, c in ARMS}
    name = {k: n for k, n, _ in ARMS}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">',
             f'<rect width="{w}" height="{h}" fill="#111"/>']
    for k in range(0, int(top) + 1, 25000):
        y = h - pad_b - (k / top) * (h - pad_b - pad_t)
        parts.append(f'<line x1="{pad_l}" x2="{w - 20}" y1="{y:.1f}" y2="{y:.1f}" stroke="#2a2a2a"/>'
                     f'<text x="{pad_l - 10}" y="{y + 5:.1f}" fill="#888" font-size="16" text-anchor="end">{k // 1000}K</text>')
    for arm, t in tr.items():
        n = max(len(t["ctx"]) - 1, 1)
        pts = [(pad_l + i / n * (w - pad_l - 30), h - pad_b - c / top * (h - pad_b - pad_t)) for i, c in enumerate(t["ctx"])]
        parts.append(f'<polyline fill="none" stroke="{color[arm]}" stroke-width="4" stroke-linejoin="round" '
                     f'points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}"/>')
        for ci in t["compact_at"]:
            if 0 < ci < len(pts):
                parts.append(f'<circle cx="{pts[ci][0]:.1f}" cy="{pts[ci][1]:.1f}" r="7" fill="{color[arm]}"/>')
    ly = pad_t + 8
    for arm in tr:
        ly += 28
        parts.append(f'<rect x="{w - 330}" y="{ly - 14}" width="22" height="6" fill="{color[arm]}"/>'
                     f'<text x="{w - 300}" y="{ly - 6}" fill="#eee" font-size="18">{name[arm]}'
                     f'{"（圆点 = 压缩）" if tr[arm]["compact_at"] else ""}</text>')
    parts.append(f'<text x="{w / 2}" y="{h - 12}" fill="#888" font-size="16" text-anchor="middle">'
                 f'每一次请求要重读的上下文（从第一句规格到画出鹈鹕）</text></svg>')
    return "".join(parts)


STORY = r"""<!doctype html><html><head><meta charset="utf-8"><title>失忆鹈鹕</title><style>
html,body{margin:0;background:#0b0b0f;color:#f2f2f2;font-family:-apple-system,"PingFang SC","Helvetica Neue",sans-serif}
#stage{width:1080px;height:1920px;margin:0 auto;position:relative;overflow:hidden}
h1{font-size:96px;margin:90px 56px 8px}.sub{font-size:40px;color:#aaa;margin:0 56px;line-height:1.4}
.cols{display:flex;gap:26px;margin:60px 56px 0}.col{flex:1;min-width:0}
.name{font-size:34px;font-weight:800;margin-bottom:16px;height:46px}
.meter{height:860px;background:#1a1a22;border-radius:24px;position:relative;overflow:hidden}
.fill{position:absolute;left:0;right:0;bottom:0;height:0;transition:height .16s linear;opacity:.92}
.ctx{position:absolute;top:22px;left:0;right:0;text-align:center;font-size:44px;font-weight:800;text-shadow:0 2px 8px #000}
.flash{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column;
 font-size:60px;font-weight:900;opacity:0;transition:opacity .2s;background:rgba(255,255,255,.18)}
.flash.on{opacity:1}.flash small{font-size:34px;font-weight:600}
.step{font-size:28px;color:#ccc;margin:18px 0 4px;height:40px;white-space:nowrap;overflow:hidden}
.cost{font-size:26px;color:#999}.cost b{display:block;font-size:44px;color:#fff}
#final{position:absolute;inset:0;background:#0b0b0f;opacity:0;transition:opacity .8s;pointer-events:none}
#final.on{opacity:1}#final h2{font-size:64px;margin:90px 56px 30px}
.pics{display:flex;flex-direction:column;gap:26px;margin:0 56px}.pic{display:flex;gap:26px;align-items:center}
.frame{width:470px;flex:none;background:#fff;border-radius:20px;overflow:hidden;aspect-ratio:4/3;display:flex;
 align-items:center;justify-content:center}.frame img{width:100%;height:100%;object-fit:contain}
.frame.empty{background:#1d1d24;color:#ffb347;font-size:23px;line-height:1.45;padding:22px;box-sizing:border-box;text-align:left}
.meta .t{font-size:38px;font-weight:800}.meta .s{font-size:96px;font-weight:900;line-height:1.05}
.meta .c{font-size:30px;color:#bbb}
.big{position:absolute;left:0;right:0;bottom:90px;text-align:center;opacity:0;transition:opacity .8s}
.big.on{opacity:1}.big .n{font-size:84px;font-weight:900;line-height:1.3}.big .l{white-space:pre-line;line-height:1.5;padding:0 40px;font-size:32px;color:#aaa;margin-top:10px}
.foot{position:absolute;bottom:34px;left:0;right:0;text-align:center;color:#555;font-size:24px}
.pic{opacity:0;transform:translateY(30px);transition:all .6s}.pic.on{opacity:1;transform:none}
</style></head><body><div id="stage">
<h1>失忆鹈鹕 🦩</h1><p class="sub">同一个 AI、同一份图纸要求。<br>中途让它「压缩记忆」好几次，最后还画得出来吗？</p>
<div class="cols">__COLS__</div>
<div class="foot">柱子 = 每次请求要重读的上下文　·　用量 = 等效 token（缓存读 0.1 倍、输出 5 倍）</div>
<div id="final"><h2>最后一句：画鹈鹕骑自行车</h2><div class="pics">__PICS__</div>
 <div class="big"><div class="n">__HEAD__</div><div class="l">__HEAD2__</div></div></div>
</div><script>
const D=__DATA__;const speed=+(new URLSearchParams(location.search).get("speed")||1);
const arms=Object.keys(D.runs);const top_=Math.max(...arms.flatMap(k=>D.runs[k].ctx))*1.08;
const fmt=n=>n>=1e6?(n/1e6).toFixed(2)+"M":n>=1e3?Math.round(n/1e3)+"K":String(n);
const N=Math.max(...arms.map(k=>D.runs[k].ctx.length));
const C={};for(const k of arms){const c=document.querySelector(".col."+k);
 C[k]={fill:c.querySelector(".fill"),ctx:c.querySelector(".ctx"),step:c.querySelector(".step"),cost:c.querySelector(".cost b"),
 flash:c.querySelector(".flash"),n:0,last:-1}}
function tick(i){for(const k of arms){const t=D.runs[k];const j=Math.min(Math.floor(i*t.ctx.length/N),t.ctx.length-1);
 if(j===C[k].last)continue;const crossed=t.compact_at.filter(a=>a>C[k].last&&a<=j).length;C[k].last=j;
 C[k].fill.style.height=(t.ctx[j]/top_*100)+"%";C[k].ctx.textContent=fmt(t.ctx[j]);
 C[k].cost.textContent=fmt(t.cost.slice(0,j+1).reduce((a,b)=>a+b,0));C[k].step.textContent=D.labels[k][t.turn[j]]||"";
 if(crossed){C[k].n+=crossed;C[k].flash.querySelector("small").textContent="第 "+C[k].n+" 次";C[k].flash.classList.add("on");
  setTimeout(()=>C[k].flash.classList.remove("on"),700/speed)}}}
function finale(){document.getElementById("final").classList.add("on");const ps=[...document.querySelectorAll(".pic")];
 ps.forEach((p,i)=>setTimeout(()=>p.classList.add("on"),(900+i*1300)/speed));
 setTimeout(()=>document.querySelector(".big").classList.add("on"),(900+ps.length*1300+600)/speed)}
let i=0;function play(){if(i<N){tick(i++);setTimeout(play,(i===1?1500:Math.max(55,11000/N))/speed)}else setTimeout(finale,1600/speed)}
setTimeout(play,1500/speed);document.addEventListener("keydown",e=>{if(e.code==="Space")location.reload()});
</script></body></html>"""


def main():
    rep = int(pc.arg("--rep", "2"))
    out = pc.arg("--out", os.path.join(pc.OUT_DIR, "video"))
    runs = pick(rep)
    arms = [a for a in ARMS if a[0] in runs]
    if len(arms) < 2:
        raise SystemExit(f"Claude 加难版第 {rep} 次至少要两组：现有 {sorted(runs)}")
    os.makedirs(out, exist_ok=True)
    tr = {k: session_trace(runs[k].get("sessions") or [runs[k]["session"]]) for k, _, _ in arms}
    # 标题里的压缩次数按实际记录写
    arms = [(k, {"B": f"压缩 {len(tr[k]['compact_at'])} 次",
                 "C": f"压缩 {len(tr[k]['compact_at'])} 次 + 记笔记"}.get(k, label), c) for k, label, c in arms]
    open(os.path.join(out, "timeline.svg"), "w", encoding="utf-8").write(timeline_svg(tr))
    pelican_check.chrome_shot("file://" + os.path.abspath(os.path.join(out, "timeline.svg")),
                              os.path.join(out, "timeline.png"), 1000, 560)
    totals = {k: sum(tr[k]["cost"]) for k in tr}
    det = {k: pelican_check.score(runs[k].get("svg"), "hard") for k in tr}
    cols, pics = [], []
    for k, label, color in arms:
        flash = f'<div class="flash" style="color:{color}">🧠 压缩<small></small></div>' if tr[k]["compact_at"] else ""
        cols.append(f'<div class="col {k}"><div class="name" style="color:{color}">{label}</div><div class="meter">'
                    f'<div class="fill" style="background:linear-gradient({color},#222)"></div><div class="ctx">0</div>{flash}'
                    f'</div><div class="step"></div><div class="cost">累计用量<b>0</b></div></div>')
        if runs[k].get("svg"):
            frame = (f'<div class="frame"><img src="data:image/svg+xml;base64,'
                     f'{base64.b64encode(open(runs[k]["svg"], "rb").read()).decode()}"></div>')
        else:
            reply = last_reply(runs[k]).strip() or "（什么也没画）"
            frame = f'<div class="frame empty">它的原话：<br>“{html.escape(reply[:105])}{"…" if len(reply) > 105 else ""}”</div>'
        pics.append(f'<div class="pic">{frame}<div class="meta"><div class="t" style="color:{color}">{label}</div>'
                    f'<div class="s">{det[k]["score"]}/{det[k]["total"]}</div>'
                    f'<div class="c">用量 {totals[k] / 1000:.0f}K token</div></div></div>')
    head, head2 = "", ""
    pct = lambda k: f'{"−" if totals[k] <= totals["A"] else "+"}{abs(1 - totals[k] / totals["A"]) * 100:.0f}%'
    if "B" in totals and "A" in totals and totals["A"]:
        head = "压缩太狠，AI 直接失忆" if det["B"]["score"] < det["A"]["score"] / 2 else "压缩之后，它还记得"
        head2 = f'压缩 {len(tr["B"]["compact_at"])} 次：{det["B"]["score"]}/16，用量 {pct("B")}'
        if "C" in totals:
            head2 += f'\n记笔记：{det["C"]["score"]}/16，用量 {pct("C")}'
        head2 += f'\nClaude {runs["A"]["model"]}'
    data = {"runs": tr, "labels": {k: turn_labels(k) for k in tr}}
    page = (STORY.replace("__DATA__", json.dumps(data)).replace("__COLS__", "".join(cols))
            .replace("__PICS__", "".join(pics)).replace("__HEAD__", html.escape(head)).replace("__HEAD2__", html.escape(head2)))
    open(os.path.join(out, "story.html"), "w", encoding="utf-8").write(page)
    cards = [{"label": label, "svg": runs[k].get("svg") or "", "score": det[k]["score"], "total": det[k]["total"],
              "missing": det[k]["missing"][:6], "cost": totals[k], "cost_label": f"等效 {totals[k]:,} token"}
             for k, label, _ in arms]
    pelican_check.grid(cards, os.path.join(out, "grid.png"))
    summary = {"rep": rep, "model": runs[arms[0][0]]["model"], "totals": totals,
               "scores": {k: det[k]["score"] for k in det}, "missing": {k: det[k]["missing"] for k in det},
               "compactions": {k: len(tr[k]["compact_at"]) for k in tr}, "requests": {k: len(tr[k]["ctx"]) for k in tr},
               "max_ctx": {k: max(tr[k]["ctx"] or [0]) for k in tr},
               "replies": {k: last_reply(runs[k])[:300] for k in tr if not runs[k].get("svg")}}
    json.dump(summary, open(os.path.join(out, "summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))
    print("素材：" + out)


if __name__ == "__main__":
    main()
