# 免费 coding agent、国内对接、按已装 agent 委托（调研 2026-10-19）

两份网络调研的摘要，原始来源链接在会话记录里。标「待核实」的是只有第三方说法，或者官方和第三方说法冲突。
这类免费政策变化很快，上线前要再核一次。

## 1. 能免费、能脚本化（命令行里自动跑完）、直接改本地代码的 agent

| agent | 现状 | 脚本化命令 | 数据 |
|---|---|---|---|
| **OpenCode Zen**（在用） | 约 14 个免费模型，都写着「限时」 | `opencode run --agent build -m opencode/<m>` | 看模型：GLM / DeepSeek / LongCat / Space Bunny **零保留**；MiMo、Big Pickle 等可能训练；Nemotron 走 NVIDIA 条款，会记录 |
| **Kilo CLI** | `kilo-auto/free` 自动选免费模型 | `kilo run --auto "…"` | 可能会记录提示词，不能放敏感代码 |
| BYO（自带 key）型：Aider / Crush / Goose / Qwen Code / Cline | 软件免费，接 OpenRouter `:free`、Groq、国产免费 API 等 | 例如 `aider --message … --yes-always`、`qwen -p … --yolo`、`crush run -y …` | 取决于接的后端 |
| Codex CLI（你已付费） | 用 ChatGPT 订阅的余量 | `codex exec --sandbox workspace-write "…"` | 消费版条款待核实 |
| Copilot CLI | Free 额度很小；2026-04 起 Free 默认拿数据训练，可以关掉 | `copilot -p … --allow-all-tools` | 默认会训练 |
| Cursor CLI | Hobby 版有很少的 agent 次数，具体数字没公布 | `agent -p --force "…"` | 隐私模式要自己打开 |

**已经不行的：**
- Gemini CLI 免费登录版（2026-06 停）。另外你的全局规则也不用 Gemini 和 Antigravity
- Qwen OAuth 免费版（2026-04 停）
- iFlow CLI（2026-04 停）
- Amp Free：脚本化只扣付费额度
- Kiro Free：脚本化需要 Pro 才给的 API key
- Jules：在云端 VM 跑，不改本地
- Devin、Augment、Factory：没有长期免费版

## 2. 国内

- **Claude Code 官方不对中国大陆开放**。大陆开发者常见的用法：
  - 用 `ANTHROPIC_BASE_URL` 把 Claude Code 指向国产的 Anthropic 兼容接口：智谱、DeepSeek、百炼、MiniMax、Kimi
  - 或者直接用国产 agent
- **国产免费 API**：

| 平台 | 免费内容 | 备注 |
|---|---|---|
| 智谱 | **GLM-4.7-Flash 免费**，有 Anthropic 兼容接口 | 限速待核实；「数据不留存」要单独申请 |
| 阿里云百炼 | 新人每个模型 100 万 token，90 天 | 未实名账户会强制「用完即停」，**不会意外扣费** |
| 硅基流动 | 一批固定限速的免费模型 | 基本要实名 |
| 魔搭 ModelScope | 改成按积分算，每天约 125 次 | 待核实 |
| 火山方舟 | 有赠送额度 | 加入「协作奖励计划」要授权数据，**公司代码不要开** |
| DeepSeek | 没有免费档 | 很便宜 |

- **国产 coding agent**：
  - 腾讯 **CodeBuddy Code**：国内体验版每月 500 积分，**和 WorkBuddy 共用**，可以脚本化：`codebuddy -p "…" -y`
  - Qoder：免费档只有试用
  - Kimi Code：没有免费档
  - Trae：开源版 trae-agent 要自带 key，traecli 免费情况待核实
- **网络**：大陆访问 OpenRouter、Groq 一般要代理；OpenCode Zen 能不能直连**待实测**
- **合规**：
  - 本地运行、用户用自己 key 的开源工具，一般不属于《生成式人工智能服务管理暂行办法》要求备案的「向境内公众提供服务」。这是推断，没有官方解释
  - **不要在国内托管面向公众的共享代理**：那就算对公众提供服务了，要用已备案的模型并履行相应义务
  - 纯代码一般不触发数据出境评估。但代码里有个人信息或密钥，或属于行业重要数据目录，就要另外评估。更现实的风险是**免费档拿数据训练**，可能违反公司的保密合同

## 3. 方案：按用户电脑上已有的 agent 选择委托

核心不变：先做 git 快照，再让别的 agent 改，然后跑用户给的验证命令。过了才保留，不过全部还原。变的只是「谁来改」。

- **适配器**：每个 agent 一个，记录：
  - 怎么检测到它（命令在不在、登录了没有）
  - 脚本化命令模板
  - **花费类别**：免费 / 已付费订阅的余量 / 按量付费
  - **数据类别**：零保留 / 可能训练 / 未知
  - 支不支持 Windows
- **`iu agents`**：列出这台电脑上找到的 agent 和各自的类别。用户选择启用哪些、按什么顺序，存在 `~/.infiniteusage/agents.json`。由 agent 照着 INSTALL.md 安装时，要把这个列表给用户看，让用户选。
- **默认规则**：
  - 先用免费、零保留的
  - 再用已付费订阅的余量：比如 Codex 周额度还剩很多时用 `codex exec`。余量从现有账本里读
  - **按量付费的默认不用**
  - `.iu-private` 项目只用零保留，或用户自己订阅且不训练的 agent
- **国内用户默认配置**：
  - 用国产免费后端：智谱 GLM-4.7-Flash、百炼新人额度
  - 委托给 `codebuddy -p -y`，或给 `qwen -p` 接智谱 key
  - 安装走国内镜像（npmmirror、清华 pip 源），代码同时放一份到 Gitee
- **第一批适配器**：OpenCode（已有）、Codex（Mac 上已装）、CodeBuddy（国内，和 WorkBuddy 共用额度）、Kilo、Qwen Code（BYO），之后再加 Copilot 和 Cursor

## 4. 实测：别的 agent 当宿主，会不会自己用这个 skill（2026-10-19，`bench/host_skill.py`）

宿主是 OpenCode + 免费模型（deepseek / glm），任务是 4 个 bug 一组的修 bug，零付费额度。

| 变体 | 加载 skill | 调 iu delegate | 测试全过 |
|---|---|---|---|
| 只把 skill 放进目录，提示词不提（6 次） | **0/6** | 0/6 | 6/6 |
| OpenCode 全局说明文件里写一条规则（2 次） | 2/2 | 1/2 | 0/2 |
| 提示词里直接点名 skill（2 次） | 2/2 | 1/2 | 1/2 |
| **`iu install` 装的常驻规则**（`~/.config/opencode/AGENTS.md`，提示词不提，2 次） | **2/2** | 2/2 | **2/2** |

**结论和已修的问题：**

1. **光放 skill 不够**：宿主不会自己去加载。要在宿主的全局说明文件里加一条常驻规则，或者用 hook 强制，Claude Code 现在就是这样做的
2. **有规则时，宿主会照着做**：加载 skill，跑 `iu agents`，写任务说明，调 `iu delegate`。但暴露了两个问题，都已修：
   - 宿主把任务说明写到 `/tmp`（项目外），OpenCode 非交互模式要审批才能写项目外的文件，会话直接结束 → skill 改成用标准输入传任务说明
   - **OpenCode 当宿主、又委托给 OpenCode**：同一个项目里两个 OpenCode 实例互相覆盖文件，宿主修好的代码被改回原样 → `iu delegate` 检测到宿主是 OpenCode（环境变量 `OPENCODE=1`）就跳过 OpenCode
3. 免费宿主本来就不花钱，委托对它没有省钱意义。这个实验测的是「别的 agent 会不会照 skill 做」。真正要省钱的是 Claude Code、Codex、Cursor 这类付费宿主
4. **`iu install` 装的常驻规则**（2026-10-19 复测）：两次都自己加载 skill，先跑 `iu agents`，再调 `iu delegate`，S1 还用了 3 次 `iu sum`，最后测试全过、没改测试文件。
   两次委托都按设计返回「没有可用的 coding agent」：宿主自己就是 OpenCode，被跳过，Mac 上又没有别的免费 agent。宿主收到失败就自己修。
   `iu sum` 那 3 次，Mac 上的免费模型超时，退回规则过滤，仍然只给宿主看压缩后的结果（原始输出 10 万字以上）
5. 统计口径：OpenCode 的事件流里同一次调用会出现两次，skill 正文里也有「委托成功」字样。`bench/host_skill.py` 现在只数跑完的 bash 调用；之前写进 `host_skill.jsonl` 的次数偏高，「有没有调用」的结论不受影响

### 4.1 Claude Code 当宿主：常驻规则是省还是费（2026-10-25，`bench/rule_ab.py`，结果 `bench/results/rule_ab.jsonl`）

Claude Code 已经有 hook 强制（hook-pre 压缩长输出，hook-agent 让子 agent 先试免费 agent），担心规则加上每次加载 skill 只是固定开销。
在 Mac 上用 `claude -p --model opus` 跑修 bug 冲刺（S1/S2/S3 各一次：4 个 bug + 查代码问题，lib 单元测试 + ANSWERS.md 判对错），提示词不提 skill，三组都开着全局 hooks：

- **A**：现状，`~/.claude/CLAUDE.md` 里是 Mac 上装的规则（3 条：加载 skill、`iu delegate`、`iu sum`）
- **B**：临时去掉 `infiniteusage:begin/end` 那一段，只留 hooks
- **N**：换成当前仓库 `render_rule` 生成的新版规则（多了 2 条 NOTES.md 要求）

每次跑之前改写 CLAUDE.md，跑完恢复，核对 sha256 一致（`5eab1348bf3b`）。等效 token = 输入×1 + 缓存写×2 + 缓存读×0.1 + 输出×5，用量取 result 事件里的 `modelUsage`（含子 agent）。

| 组 | 等效 token（S1 / S2 / S3） | 平均 | 美元合计 | 轮数 | 墙钟平均 | 测试全过 | 加载 skill | 委托成功 | 写 NOTES.md |
|---|---|---|---|---|---|---|---|---|---|
| B 只有 hooks | 98,005 / 115,488 / 97,439 | 103,644 | $1.047 | 14 / 13 / 12 | 80 s | 3/3 | 0/3 | 0/3 | 0/3 |
| **A 现状规则** | 68,526 / 97,260 / 96,519 | **87,435（-15.6%）** | $0.909（-13%） | 10 / 15 / 12 | 254 s | 3/3 | 3/3 | 3/3 | 0/3 |
| N 新版规则 | 60,196 / 107,053 / 138,967 | 102,072（-1.5%） | $1.027（-2%） | 7 / 13 / 17 | 177 s | 3/3 | 3/3 | 3/3 | 3/3 |

逐对比（同一冲刺）：A 比 B 是 -30% / -16% / -1%，三对都不更费；N 比 B 是 -39% / -7% / +43%。

**结论：**

1. **规则在 Claude Code 上是净省，不是纯固定开销**。没有规则时，Opus 主会话 0/3 次加载 skill、0/3 次委托：hook-agent 只在开子 agent 时起作用，主会话自己修 bug 根本不经过它；hook-pre 在这组任务里也几乎没触发（测试输出短）。有规则时 3/3 次加载 skill、跑 `iu agents`、调 `iu delegate`，免费 agent（OpenCode deepseek-v4.1-flash-free）三次都修好，Opus 只复核。加载 skill + `iu agents` 的固定开销远小于省下的修 bug 过程
2. **代价是慢**：委托本身 2~4 分钟，A 平均 254 s，B 80 s（多 ~3 倍）。结果质量相同（9/9 全过，没改测试文件，答案都对）
3. **新版规则（多 2 条 NOTES.md）在这类一次性任务上把省下的又吃回去了**：3/3 次先写 NOTES.md（把任务要求抄一遍，多 1~2 轮）；S3 那次还把 `iu delegate` 放到后台跑、再写循环轮询结果，17 轮、+43%，违反了 skill 里「阻塞运行，不要后台」的要求。N 只有 3 次，方差大，但方向是 NOTES 那两条在短任务上是开销；它们的收益在长会话、压缩后保留用户要求（skill 里引用的压缩实验：16 条要求没笔记全丢，有笔记留 12 条），这里测不到
4. 样本小（每组 3 次），结论看方向：A < B 三对一致；具体百分比别当精确值。S2、S3 期间 Mac 上同时有一个 Codex 冲刺在跑，可能让墙钟略长，不影响 token

**建议：** 默认继续对 Claude Code 写常驻规则，它是主会话里触发 `iu delegate` 的唯一途径（-13%~-16%）。新版里的 NOTES.md 两条在 Claude Code 上可以考虑去掉或改弱（Claude Code 已有 SessionStart / 压缩后的 NOTES hook 负责把笔记放回上下文），比如改成「用户提出具体要求时才记」，避免一次性任务也先抄一遍 TASK.md；同时在规则里重复一句「`iu delegate` 阻塞运行，不要放后台轮询」。这两处改动要再跑一组 N 才能确认

**补跑（2026-10-25 下午）：**

- 两处改动已经做进 `render_rule`（NOTES 只记用户说的具体要求、一次性任务说明不用抄；委托前台阻塞运行）
- B 组加到 6 次：平均 101,722（S1 98,005 / 87,062，S2 115,488 / 105,801，S3 97,439 / 106,536），全部 6/6。A 组 3 次平均 87,435，比 B −14%
- A、N 的补跑全部作废，结果单独放在 `bench/results/rule_ab_contaminated.jsonl`：Mac 上 OpenCode 免费额度用完（日志里是 `Free usage limit reached`），OpenCode 不退出、一直重试，`iu delegate` 每次卡满 419 秒。7 次平均 134,949（比 B +33%），每次 8~9 分钟，1 次答错。一开始以为是和跨会话测试抢同一个免费模型，停掉跨会话之后照样卡，直接让 7 个免费模型各回一句 OK 全部超时，才从 `--print-logs` 里看到额度报错
- 修复：`iu delegate` 加 `--print-logs` 边读边看，见到额度用完的报错马上放弃，同一个 agent 的其他免费模型这次不再试；每次尝试最多用剩余预算的 60%；测速记录（`iu/speed.py`）让失败过的排到后面。Mac 实测 4 秒判定失败
- 修过的 N 版规则没测成，不再补测：OpenCode 只是免费外包工，额度用完就是用完了，委托会在 4 秒内放弃、交回主 agent 自己做
