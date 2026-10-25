# InfiniteUsage

**装一次，你的 coding agent 少烧付费 token，结果不变。**

一个 skill 加一个小命令行（`iu`，只用 Python 标准库），给**花订阅额度的 Claude Code 和 Codex** 用。skill 在 WorkBuddy、CodeBuddy、Cursor、Trae 以及所有读 `~/.agents/skills` 的 agent 里也能用；OpenCode 这类免费 agent 负责干外包的活。装进去的每一样都先实测过；测下来没用的，全部扔掉。

[English](README.md)

![新会话、同样 16 条要求：不记笔记 Claude Code 和 Codex 都忘光；装了 InfiniteUsage 两家都是 16/16](assets/amnesia.png)

## 安装

跟你的 agent 说：

> 帮我装 https://github.com/Jason-Zhang-xiquan/infiniteusage：clone 下来，运行 `python run_iu.py install`。

或者自己装：

```bash
git clone https://github.com/Jason-Zhang-xiquan/infiniteusage
python infiniteusage/run_iu.py install --dry-run
python infiniteusage/run_iu.py install
```

`--dry-run` 先列出要改的每一处。改之前会备份设置，`python run_iu.py uninstall` 能精确还原。装完新开一个会话生效。

## 做了什么，实测多少

| | 做什么 | 实测 |
|---|---|---|
| **约 30 万就压缩，不等到约 96.7 万**（Claude Code） | 每次请求都要把整段上下文重读一遍。默认设置下长会话几乎不压缩，每轮都在重读 40～70 万 token | 在 7 天真实日志上回放：**省 43%～50%**（Codex 约 15 万压缩：省 21%，悲观估计 6%）。装上之后作者自己的真实工作：**每次请求 −34%**，上下文 −38%（装前装后对比，不是对照实验） |
| **不忘你定的要求**（Claude Code、Codex） | 每条要求记进 `NOTES.md`；新会话开场、每次压缩之后，hook 自动把它放回上下文 | 新会话、事先定好 16 条细节：不装只记得 **1/16**；装了之后两家都是 **16/16**。光写笔记不够：Codex 笔记写了照样 1/16，加上规则或 hook 让它先读才满分。频繁压缩：0/16 → 16/16 |
| **先让免费 agent 改代码** | `iu delegate` 把能用命令验证的改动（修测试、按测试实现）交给免费 coding agent（OpenCode 免费模型；它的免费额度用完，就换成你加的免费 API key 接着干）。你的验证命令通过才保留，不过就全部还原 | 免费 agent 修单个 bug 47/48，4 个一组 3/3（单元测试判定）。端到端 Opus：**−17% / −18%**，测试全过，慢约 50 秒。让主会话也用上它的是全局规则：比只有 hook **−14%**。免费额度用完换你自己的 key：**22 秒**修好 |
| **自动挑最快、能用的免费模型** | 每次调用都记下快慢和失败，过期了就在后台并行补测。挂掉或被限流的排到最后，免费额度用完的 agent 马上放弃、换下一家 | 6 个免费模型并行测完 2.6 秒，揪出一个已下线的。一次 30 秒超时的摘要，下次 4.3 秒拿到。免费额度用完时：**4 秒放弃，不再卡 7 分钟**（之前这样多花了 33%） |
| **长输出变摘要**（可选） | 长的测试、构建、git 输出交给免费模型摘要，原始日志存成文件，报错行原样保留 | 关键信息保留 97%，编造 0。测试多的 Sonnet 任务 **−20%** |

skill 加上写进全局说明文件的一段带标记的规则（`~/.claude/CLAUDE.md`、`~/.codex/AGENTS.md`），教 agent 什么时候用这些。只放 skill 时 Opus 一次都不委托；加了规则每次都委托。

## 测过、扔掉的

实测没用，所以没装：

- **主模型换成 Sonnet 或 Haiku**：Opus 每个任务反而**便宜 49%**（轮数少、上下文小）。Haiku 当主模型比 Opus 贵，还失败了 1 次
- **降推理强度**：+2%（Sonnet）、+6%（Opus）
- **压缩代理**（headroom）：+2.2%
- **代码地图、记忆框架**（claude-mem、serena 这类按符号读代码）：在真实日志上的上限只有 2%～4%，比两次运行之间的随机波动还小
- **小问题换个地方问**，不在长会话里问：0.65%
- 交接文件、调小输出上限、遮掉旧的工具输出；短任务提前压缩（+19%：会话短时，压缩本身的花费省不回来）

每一行的数字、脚本和注意事项：[EVIDENCE.md](EVIDENCE.md)、`bench/`、`docs/`。

## 支持哪些 agent

| Agent | Skill | 全局规则 | 自动加载 NOTES.md | 压缩设置 | 免费 agent hook |
|---|---|---|---|---|---|
| **Claude Code** | ✅ | ✅ | ✅ hook | ✅ 约 30 万 | ✅ |
| **Codex** | ✅ | ✅ | ✅ hook（Mac/Linux；要在 `/hooks` 里信任一次） | ✅ 约 15 万 | — |
| WorkBuddy、CodeBuddy、Cursor、Trae、Copilot、Amp、Goose、Windsurf、Cline | ✅ | — | — | — | — |

免费 coding agent（OpenCode、Kilo、Qwen Code、CodeBuddy）是 `iu delegate` 交活的工人。它们不写全局规则，所以被叫来干活时不会又绕回 `iu delegate`。

委托需要这台电脑上有免费 coding agent：`iu agents` 会列出找到的和可以装的，装之前一定先问你。长输出摘要需要一个免费 API（见下）；没有的话这一项直接跳过。

## 加一个免费 API

长输出摘要（`iu sum`、Bash hook）可以用任何免费的 OpenAI 兼容接口；OpenCode 自带的免费额度用完时，`iu delegate` 也会把这些 key 交给它接着改代码。自己去官网注册（一家一个账号，用官方免费层），然后在**你自己的终端**里运行：

```bash
python run_iu.py api add gemini
```

它会给出注册地址，用不显示的输入框让你粘贴 key，存进 `~/.infiniteusage/keys.env`（只有你能读），再从这家的 `/models` 里挑一个模型。agent 看不到、也不会替你输入 key；没有真正的终端时 `api add` 会拒绝运行。

| 预设 | 注册地址 |
|---|---|
| `gemini` | https://aistudio.google.com/apikey |
| `groq` | https://console.groq.com/keys |
| `openrouter` | https://openrouter.ai/settings/keys（`:free` 模型） |
| `cerebras` | https://cloud.cerebras.ai |
| `mistral` | https://console.mistral.ai/api-keys |
| `nvidia` | https://build.nvidia.com |
| `zhipu`（智谱）/ `siliconflow`（硅基流动）/ `dashscope`（阿里云百炼） | https://open.bigmodel.cn · https://cloud.siliconflow.cn · https://bailian.console.aliyun.com |

加完 `api add` 会马上跑一遍资格测试（通常 1~3 分钟，用这家的免费额度）：7 条真实命令输出，按能力画像同一套规则和门槛打分（质量 ≥ 85%、编造 0、失败 ≤ 30%）；装了 OpenCode 的话再让它用这个 key 修一个小 bug。哪类任务没过，以后就不交给它；一次回答都没拿到的（限流、网络）记「没测成」，不拦。`api list` 看结果，`api qualify gemini` 重测，`--no-qualify` 跳过。

别的接口：`api add myapi --base-url https://example.com/v1`。之后可以 `api list`、`api test gemini`、`api remove gemini`。加了好几家时按加的顺序依次试。如果你自己搭了 [FreeLLMAPI](docs/freellmapi_setup.md) 网关，会先用它。

## 要知道的

- 委托给 OpenCode 时，只把它需要的那一个 key 给它，环境变量里别的 key、token 都去掉；任务说明发出去之前先遮掉 key。`api add` 不接受非本机的 http 地址
- 免费模型可能拿你发过去的内容训练。在项目根目录放一个空文件 `.iu-private`，这个项目就一律不外发；或者设 `IU_NO_TRAINING_MODELS=1`
- 压缩会丢细节，所以才有笔记。装上之后查过真实日志：刚压缩完的那几句里，用户纠正的比例没有变高（装前 1/52，装后 1/54），样本小
- 只用合法的免费额度：一家一个账号，用自己的 key
- `python run_iu.py compare` 能看你自己装前装后的对比
- 输出跟着系统语言用中文或英文；想固定就设 `IU_LANG=zh` 或 `IU_LANG=en`
- 速度：委托和摘要会先用免费模型，所以一个任务的墙钟时间可能变长（一次测试里委托改代码最多慢约 3 倍）。不想要哪项就在安装时加 `--no-delegate`、`--no-offload`、`--no-notes` 或 `--no-compact`
- 免费模型按实测速度挑：每次真实调用都记下用了多久、有没有失败；新会话开场时，如果超过一小时没测过，就在后台并行把所有免费 API 测一遍。刚失败的排最后，明显更慢的往后排，差不多快的保持从强到弱的顺序。卡住的免费 agent 也不会再吃光整个委托预算。`python run_iu.py speed` 看测速表，`speed --probe` 马上测一遍，`IU_NO_SPEED_PROBE=1` 关掉探测
- Codex：hook 要在 Codex 里输入 `/hooks` 信任一次才会运行。Windows 上暂时不装 Codex 的 NOTES.md hook（只在 Mac / Linux 上实测过），全局规则照样会让 Codex 先读 `NOTES.md`

## 许可证

MIT
