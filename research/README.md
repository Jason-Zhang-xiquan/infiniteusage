# InfiniteUsage 调研报告（2026-10-17）

目标：让 Claude Code、Codex、WorkBuddy 等 agent 订阅干更多活：同样的额度干更多事，额度在重置前不浪费，超出的部分交给免费/便宜模型，agent 无人值守时也一直有活干。

调研顺序按你要求：① Mac mini 上的 IG「General AI」收藏视频 → ② 全 GitHub 开源项目 → ③ 自己调研额度机制和灵感。

| 文件 | 内容 |
|---|---|
| [videos/01_视频评估.md](videos/01_视频评估.md) | 10 条视频逐条评估、核实 |
| [videos/transcripts/](videos/transcripts/) | 9 条视频的转写稿 |
| [raw/02_github_survey.md](raw/02_github_survey.md) | GitHub 六大类 ~60 个项目（star 和最后更新时间都是 10-17 查的） |
| [raw/03_quota_mechanics_and_ideas.md](raw/03_quota_mechanics_and_ideas.md) | 各家额度机制、省额度技巧、社区做法、ToS 风险（带来源链接） |

---

## 一句话结论

**开源界已经有很多「看额度」「省 token」「并行跑」的单点工具，但没有一个工具能同时知道 Claude、Codex、WorkBuddy、国产 coding plan 各自还剩多少额度、什么时候重置，再把任务队列派给有余量的那家。** 这就是 InfiniteUsage 最值得做的东西。而且你有一台一直开着的 Mac mini，正好当 24 小时跑任务的机器。

---

## 一、视频评估（详见 [01_视频评估.md](videos/01_视频评估.md)）

10 条里真正和「更多用量」有关的是 3 个方向：

1. **省 token**：Graphify（代码知识图谱）。视频说 70 倍，实际 README 写 80%，独立实测 5–10 倍。值得试。
2. **溢出到免费模型**：免费 LLM API 清单。视频里那个 22k★ 的 repo **已经 404 了**，用 [mnfst/awesome-free-llm-apis](https://github.com/mnfst/awesome-free-llm-apis) 替代。
3. **挖自己的会话记录**：视频推的付费插件找不到对应功能，但思路对，可以自己做。

其余：Agent Reach、Obscura、claude-video 是给 agent 加能力；Jev 是廉价判断模型，可用于压缩；Higgsfield「开源整个平台」**是假的**。
这批视频几乎全是「评论关键词领 repo」引流，数字普遍夸大，以后只取工具名，数字自己核实。

---

## 二、各家额度怎么算（决定了能钻哪些空子）

| 产品 | 计量方式 | 关键点 |
|---|---|---|
| **Claude Pro/Max** | 滚动 5 小时 + 每周上限，claude.ai 和 Claude Code 共用 | 订阅下主对话缓存 1 小时，子 agent 5 分钟。`/model` 切换、`/compact`、增删 MCP 会打断缓存。`autoContinueAtUsageLimit` 可以到点自动继续。Max 每月送 $100/$200 API 额度（不累积，不能用于交互式 Claude Code）。`claude -p` 消耗订阅额度 |
| **Codex（ChatGPT）** | Plus：5 小时 + 每周；Pro 目前没有 5 小时限制 | **选模型是最大的杠杆**：Luna 和 Sol 每百万输出 token 的消耗差 20 倍；Fast 模式 2.5 倍，Ultrafast 8 倍。云端任务比本地更费 |
| **WorkBuddy / CodeBuddy** | 积分制，两个产品共用。国际版 Pro $10 = 2000 积分/月，不累积；国内 ¥99/199/999 | 每日活跃奖励只认客户端操作，用脚本刷会被取消资格。各模型扣多少积分没公开。官方 CLI 是 `@tencent-ai/codebuddy-code` |
| **国产 coding plan** | 智谱 GLM、Kimi、MiniMax、阿里都是「5 小时 + 每周」 | 都能用 `ANTHROPIC_BASE_URL` 接进 Claude Code。GLM 旗舰模型在 14–18 点（北京时间）扣 2–3 倍 |
| **Gemini CLI** | 6-18 起免费个人账号转去 Antigravity | 你不用 Antigravity，所以 Gemini 只当付费 key 的溢出 |

**能合法利用的「空子」**：

- 5 小时窗口可以提前「锚定」：早上发一个很小的请求，让窗口在工作时间中段重置。它只移动空闲时间，不增加每周总量，6 月后有报告说计时可能变了，要自己验证。
- 每周额度在重置前用不完就作废。heavy user 的做法是重置前一晚跑 Ralph 循环，把剩余额度烧掉。
- 同一目录下的并行会话共享缓存，所以批量任务放在同一 repo 的多个 worktree 里更省。

---

## 三、GitHub 上值得先试的（详见 [02_github_survey.md](raw/02_github_survey.md)）

| 优先级 | 项目 | 用途 | 备注 |
|---|---|---|---|
| 1 | [ccusage](https://github.com/ccusage/ccusage) + [ccstatusline](https://github.com/sirmalloc/ccstatusline) | 先量化，建立基线 | ccusage 同时读 `~/.claude` 和 `~/.codex` |
| 2 | [CodeZeno/Claude-Code-Usage-Monitor](https://github.com/CodeZeno/Claude-Code-Usage-Monitor) | Windows 任务栏显示 Claude/Codex 的余量和重置时间 | CodexBar 只有 macOS 版，可装在 Mac mini 上 |
| 3 | [rtk](https://github.com/rtk-ai/rtk)、[serena](https://github.com/oraios/serena)、[codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)、[Graphify](https://github.com/Graphify-Labs/graphify) | 压缩命令输出、按符号读代码、代码图谱 | 独立测试的效果远低于宣传（caveman 约 8.5%，rtk 不明显），**必须用 ccusage 自己做前后对比** |
| 4 | [openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc) | Claude Code 里把评审和批量活交给 Codex | 官方插件，走 ChatGPT 额度，没有 ToS 风险 |
| 5 | [claude-code-router](https://github.com/musistudio/claude-code-router) / [cc-switch](https://github.com/farion1231/cc-switch) | 把后台和简单子任务路由到国产 plan 或免费模型 | 用 API key 才安全 |
| 6 | [vibe-kanban](https://github.com/BloopAI/vibe-kanban) / [claude-squad](https://github.com/smtg-ai/claude-squad) + [worktrunk](https://github.com/max-sixty/worktrunk) | 多个 worktree 并行跑 | 真正「把余量烧成产出」的方式 |
| 7 | [ralph-claude-code](https://github.com/frankbria/ralph-claude-code) | 通宵无人值守循环，带退出检测 | 要配测试门槛 |
| 8 | [claude-warmup](https://github.com/vdsmon/claude-warmup) + [unsnooze](https://github.com/saaranshM/unsnooze) | 锚定窗口；重置后自动继续 Claude/Codex/Kimi 等 | |
| 9 | [claude-reflect](https://github.com/BayramAnnakov/claude-reflect)、[claude-devtools](https://github.com/matt1398/claude-devtools) | 从会话记录里挖规则、看 token 花在哪 | 视频 3 那个思路的开源版 |

**WorkBuddy 生态**：GitHub 上几乎全是中文的多账号池、刷积分工具（workbuddy2api-panel、WorkDaddy 等），**ToS 风险高，不建议用**。唯一干净的路线是包装官方 `codebuddy` CLI（如 [LinHaiJ/workbuddy-mcp](https://github.com/LinHaiJ/workbuddy-mcp)，只有 4★，可以自己写）。

---

## 四、InfiniteUsage 可以做什么（两份调研的空白点合并后排序）

按「价值 × 可行性」排序，前 3 个合起来就是 MVP：

1. **跨平台额度账本**：读 `~/.claude/projects/*.jsonl`、`~/.codex/sessions`、statusline 的 `rate_limits`，加上手动或 API 录入的 WorkBuddy、GLM、Kimi 余额，显示「每个池还剩多少、多久重置、这周还能跑几次重活」。现有工具只覆盖单家，或者只看历史；**没人做跨家的「向前看」和每周节奏**。
2. **「重置前不浪费」调度器**：一个待办队列（issue、TODO、补测试、重构），按难度打标签。某个池快重置且还有余量时，自动用官方 CLI（`claude -p`、`codex exec`、`codebuddy`）在 worktree 里开跑，早上看 PR。跑在 Mac mini 上。现有的 Ralph 只会循环，routines 一天最多 15 次，**都不看额度**。
3. **任务交接包**：Claude 撞上限时，自动写一个状态文件（计划、diff、失败的测试），让 Codex 或 GLM 从断点接着做，回来再交还。**目前没人做**。
4. **token 浪费审计**：回放会话日志，找出缓存未命中及原因（切模型、MCP 变动、空闲超过 TTL、CLAUDE.md 过大、子 agent 用了 Opus），给出具体改配置的建议，每周出一个浪费分。覆盖 Codex。
5. **自己任务上的真实省 token 基准**：同一批任务分别开/关 rtk、serena、caveman、Graphify，比 token 和成功率。宣传数字和独立测试差太多，这个本身就很有价值。
6. **任务→模型匹配学习**：从历史里学哪类任务在便宜模型上也能成功，默认路由过去。
7. **会话开始时的模型/effort 建议 hook**：一开始就选对模型，避免中途切换打断缓存。
8. **额度过期提醒**：Max 每月 API 额度、WorkBuddy 积分快过期时，自动派去跑批量任务（Message Batches 做评测、写文档）。
9. **Windows 优先 + 支持 WorkBuddy**：现有监控大多是 macOS 的，WorkBuddy 只有灰色工具。

**建议的第一步**：先装 ccusage 量一周基线，同时做 #1 额度账本的最小版（只读本地日志 + 重置倒计时）。有了数据再做 #2 调度器。

---

## 五、红线（会封号的）

- **高风险，不做**：把 Claude 订阅的 OAuth 用在第三方工具里（2026-04-04 起封禁）、订阅转 API 的代理（CLIProxyAPI、claude-relay-service）、多账号轮换躲限额（claude-swap）、WorkBuddy 账号池和刷积分。V2EX 上有中转商反映约 90% 的号被封。
- **安全**：在自己账号上跑官方 CLI 的无头模式（`claude -p`、`codex exec`、routines），以及用真正的 API key 接国产 plan 或免费模型。

InfiniteUsage 的原则：**只驱动官方客户端 + 真 API key，每条路由标出风险等级。** 这一点本身也是现有工具缺的。

---

## 六、没核实 / 待办

- Claude 各模型在每周额度里的权重、5 月「5 小时翻倍」的说法、Codex 每周数字、WorkBuddy 各模型扣多少积分、Kimi/GLM/MiniMax 每周数字：都只有第三方来源。
- 窗口锚定在 6 月后是否还有效，要用 `/usage` 实测。
- 有一篇未核实的文章说，从年中起定时跑的 `claude -p` 改为单独计费，要以 Anthropic 文档为准。
- 视频：图集 Ddt16ilkRGB 里另外 4 个 repo 没确认。剩下 7 条视频 Mac 会在 **10-18 08:05** 起自动下载（日志 `~/general_ai_fetch.log`），下完可以补看图集内容。
