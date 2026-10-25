# 能力画像、没用上的免费资源、没试过的省 token 方法（2026-10-18）

## 1. 能力画像：SkVM 能不能直接用？

[SkVM](https://github.com/SJTU-IPADS/SkVM)（上海交大 IPADS，MIT，约 560★，论文 [arXiv 2604.03088](https://arxiv.org/abs/2604.03088)）：

- 它把模型 + agent 框架的组合，按 26 种「原子能力」测一遍，得到 Target Capability Profile（TCP）
- 然后把 skill **编译**成适合这个模型的写法
- 命令：`skvm profile --adapter=bare-agent --model=<provider>/<model>`，测一个模型约 20 分钟
- 支持的模型来源：Anthropic 原生接口、OpenRouter。没写支持任意 OpenAI 兼容地址，所以 FreeLLMAPI 不一定能接
- 论文自己报告：完成率更高、token 最多少 40%。**没有独立复现**

**不直接用它的原因：**

1. 它测的是通用 agent 能力（工具调用、文件操作等 26 项）。我们外包的是很窄的几类活：压缩日志、从搜索结果里找位置、读文件回答问题并给行号。它的画像回答不了「这个模型压缩测试日志会不会编行号」
2. 免费模型限流严重，一个模型 20 分钟的画像大概率跑不完。OpenRouter 免费额度每天只有 50 次，没充 $10 根本不够
3. 它的用途是改写 skill，不是做路由

**借鉴它的思路，用我们自己的基准做「任务型能力画像」：**

- 画像 = 模型 × 任务类型 →（准确率、编造率、延迟、失败率、限流情况）
- 用基准 2 的框架，语料从 10 条扩到约 30 条，覆盖 6 类任务：
  - 测试和构建日志
  - 搜索结果里找位置
  - 读文件答问题并给行号
  - git 历史
  - 解释代码
  - 写测试草稿
- `iu` 按画像路由：每类任务配一条「从强到弱」的模型链，最后一档用规则过滤兜底
- 免费模型每周在变，所以画像要**每周自动重测**（Mac 上定时跑），模型掉线或变差就自动降级

如果你以后加了 OpenRouter key，可以顺便用 SkVM 测 1~2 个模型，作为对照。

## 2. 免费资源用上了哪些

| 资源 | 状态 | 卡在哪 |
|---|---|---|
| Kilo Gateway（免 key） | ✅ 在用 | 提示词会被拿去训练，只能用于公开代码 |
| OVH AI Endpoints（免 key） | ✅ 在用 | 每个模型每分钟只能调 2 次 |
| **Groq** | ❌ | 要你注册拿 key。**不留存数据**，私有代码也能用 |
| **Google AI Studio（Gemini 3.6/3.8 Flash，100 万上下文）** | ❌ | 要 key。免费版会拿提示词训练 |
| OpenRouter `:free` | ❌ | 要 key。每天 50 次，充 $10 后 1000 次 |
| NVIDIA NIM（GLM-5.2 等） | ❌ | 要 key，条款写明仅限评估使用 |
| Z.ai GLM-4.7-Flash | ❌ | 要 key |
| Mistral、Cloudflare Workers AI | ❌ | 要 key，额度小 |
| **OpenCode Zen 免费模型** | ❌ | 2026-09 起只能在 OpenCode 自己的客户端里用，API 调用返回 403。只能装 OpenCode CLI，当独立 agent 用 |
| **Jules**（每天 15 个异步任务） | ❌ | 要绑 GitHub；适合写清楚的小工单 |
| Copilot Free CLI（每月 50 次） | ❌ | 要先关掉训练 |
| CodeBuddy CLI（每月 100 积分） | ❌ | 你的 WorkBuddy 账号就有，还没测 CLI 的免费积分能不能用 |
| **Claude Max 每月送的 API 额度**（$100 / $200） | ❓ | 如果你是 Max，这笔钱已经付了，可以用来跑 Haiku/Sonnet 的子任务。要先确认你的套餐。只能用于 API 和 Agent SDK，不能用于交互式的 Claude Code |
| DeepSeek V4-Flash（付费但极便宜） | ❌ | 约 $0.004 一次，可以做最后一档兜底 |

## 3. 省 token 的方法：试过的、没试的

已经做的：`iu` 外包压缩 + 护栏 + hook v2（第 1 轮里中位数省 49%，样本小，待第 2 轮确认）；交接 + 新会话（第 1 轮几乎没省，还更慢）。

没试过的，按「独立测试结果 × 和你的用量构成是否对得上」排序：

| 方法 | 独立测试结果 | 和你的情况 | 建议 |
|---|---|---|---|
| **ponytail**（让 agent 别过度设计的 skill） | JetBrains：花费 -10.3%，耗时 -11%，质量没下降（二手来源） | 少写代码 → 少输出，也少一些上下文 | **测** |
| **Claude 降推理强度**（effort） | 没找到独立测试 | 思考占上下文 4.7%，而且是低估的（日志里只有思考的摘要） | **测** |
| **调小内置输出上限**（`BASH_MAX_OUTPUT_LENGTH` 默认 3 万字符、读文件默认 2.5 万 token） | 没有独立测试 | 免费、可随时撤回；但截断会丢信息，基准 2 里截断就丢了一半答案 | 测，和外包 hook 一起比 |
| **子 agent 用 Haiku**（`CLAUDE_CODE_SUBAGENT_MODEL`） | 没有 | 你的子 agent 只占 9% | 小收益，先不测 |
| **精简常驻上下文**（MCP、skill 列表、CLAUDE.md） | 官方文档推荐 | 附件和提醒占 4.1% | 检查一遍就行，不用 A/B |
| caveman（让输出更简短） | JetBrains：输出 -8.5%，总花费约 -10% | 输出在你的用量里占比小 | 低优先级 |
| serena / Graphify / codebase-memory-mcp（代码图谱，按需读） | Graphify 独立测 +1% / -3.3%；serena 没有独立测试 | 读文件占 18%，理论上对得上 | 可以测 serena |
| rtk | +7.6% / +13% / 不明显 | — | 不测 |
| headroom | +44% | **+2.2%**（0.41.0，--mode cache，3 组 Sonnet 冲刺，测试都全过） | 2026-10-19 重测，**不用**。代理实际只压掉 2% 的输入（tok_before→tok_after），它日志里的「节省」大多是把工具定义延后加载的估算值；轮数还多了 2~10 轮。见 `bench/e2e_headroom.py`、`bench/results/e2e_headroom.jsonl` |
| context-mode | +33% ~ +72% | — | 不测 |
| 观察遮蔽（只保留最近 N 个工具输出） | 学术论文：花费 -50%，质量不变 | Claude Code 似乎有「清理旧工具结果」的机制（缓存文档里提到过），**怎么开还要查** | 先查 |
