# 安装 InfiniteUsage（给 AI agent 照着做）

目标：结果质量不变，付费 token 明显减少。每一项都有证据，见 [EVIDENCE.md](EVIDENCE.md)。
只用 Python 标准库，需要 Python 3.9 以上。

**通用**：skill 装进各家 coding agent 读的目录，包括 Claude Code、Codex、Cursor、OpenCode、Kilo、Copilot、Amp、Goose、Windsurf，以及 CodeBuddy、WorkBuddy、Trae、Qwen Code、Cline（这几家只有装了才装）。
任何一家里，agent 都能找出这台电脑上的其他 coding agent 和免费 API，把改代码的子任务先交给免费的那个。
**宿主**：只有花订阅额度的 Claude Code 和 Codex。其他 agent（WorkBuddy、CodeBuddy、Cursor、Trae 等）只装 skill，照样能用；OpenCode、Kilo、Qwen Code 这些免费 agent 是 `iu delegate` 交活的工人。
**常驻规则**：光放 skill，宿主不会自己去用（实测：OpenCode 0/6，Opus 主会话一次都不委托），所以还会在 Claude Code、Codex 的全局说明文件（`~/.claude/CLAUDE.md`、`~/.codex/AGENTS.md`，装了那个 agent 才写；免费工人不写，免得被叫来干活时又绕回 `iu delegate`）末尾加一段带 `infiniteusage:begin/end` 标记的规则。原有内容不动，卸载时按标记删掉，文件恢复成原样。
**Claude Code 额外**会改设置（压缩点和两个 hook）。**Codex 额外**会改 `~/.codex/config.toml` 顶层的一行 `model_auto_compact_token_limit = 150000`（提前压缩），并在 Mac / Linux 上装 NOTES.md hook。其他 agent 只装 skill。

## 装了什么

| 机制 | 做什么 | 证据 |
|---|---|---|
| 常驻规则（Claude Code / Codex） | 全局说明文件里加一段：写代码前先加载 skill，能验证的改动先 `iu delegate`，长输出用 `iu sum` | Claude Code（Opus 修 bug 冲刺）：没有规则 0 次委托，有规则每次都委托，比只有 hook −14% |
| skill（所有 agent） | 教 agent 用 `iu agents` 找可用的 agent 和免费 API，用 `iu delegate` 把能用命令验证的改代码子任务先交给免费 agent，用 `iu sum` 读长输出的摘要 | 免费 agent 修 bug：47/48 个单 bug、3/3 组 4 个 bug 一起修，都由单元测试判定 |
| 约 300K 压缩（Claude Code） | 设置 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`。默认要到约 967K 才压缩，长会话的每一轮都在重读几十万 token | 在用户的真实日志上模拟：省 43~50% |
| 约 150K 压缩（Codex） | `~/.codex/config.toml` 顶层设 `model_auto_compact_token_limit = 150000`。默认不设时约在窗口的 90% 才压缩（272K 窗口的模型约 232K，gpt-6.1-sol 约 427K）。只按行改这一行，别的内容一字不动；改前备份，原值记进 install.json，卸载时一字节不差地还原（你后来自己改了这个值，卸载就不动它） | 在用户的真实 Codex 日志上模拟：Windows 省 21%（悲观假设 6%）；Mac 会话短，不触发。Mac 小规模实测（修 bug 冲刺 6 次/组）：150K 在短任务上不触发，6/6 全过、用量 −1%（噪声）；强制 40K 压缩 6/6 全过、+1% |
| 子 agent 先试免费 agent（Claude Code） | PreToolUse hook（Agent 工具）：通用子 agent 改代码前先交给免费 agent，验证命令通过才保留，不过就全部还原 | 端到端 Opus：-17~18%，测试全过 |
| 外包长输出（Claude Code） | PreToolUse hook：长的测试、构建、git 输出交给免费模型压缩，原始输出存成文件，出错原样放行 | 测试/构建多的任务：Sonnet -20%。以读代码为主的工作只省 1~2% |
| NOTES.md 自动放进上下文（Claude Code / Codex） | 新会话开场、压缩之后，把项目根目录的 NOTES.md 放进上下文：SessionStart hook（`iu hook-compact`）。常驻规则里也写了「新会话先读 NOTES.md」 | 「跨会话失忆鹈鹕」新会话不提上次：不记笔记 Claude Code、Codex 都只有 1~2/16；规则或 hook 之后都是 16/16。频繁压缩：不记笔记 0/16，笔记 + hook 16/16 |

## 步骤（agent 执行）

1. 确认 Python：`python --version` 或 `python3 --version`，需要 3.9 以上
2. 拿到代码（克隆或复制这个仓库），记下仓库路径 `<repo>`
3. 看这台电脑上有什么：

   ```bash
   python <repo>/run_iu.py agents
   ```

   - 列出找到的 coding agent、已配置的免费 API、网络像不像在国内，以及建议装什么、注册什么
   - **装软件之前先问用户**
   - 不替用户注册账号，也不替用户填 key，也不要让用户把 key 贴进对话
   - 外包长输出要一个免费 API（OpenCode 自带的免费额度用完时，委托也会用它当后备模型）：让用户自己去官网注册（一家一个账号，官方免费层），然后**在用户自己的终端**运行 `python <repo>/run_iu.py api add gemini`（预设：gemini、groq、openrouter、cerebras、mistral、nvidia、zhipu、siliconflow、dashscope；别的 OpenAI 兼容接口加 `--base-url`）。key 用不显示的输入框输入，agent 代跑会被拒绝。`api list` 列出注册地址，`api test <名字>` 试一下
   - 至少要有一个免费 coding agent，`iu delegate` 才有用。海外推荐 OpenCode：`npm i -g opencode-ai`，不用登录

4. 预览要改什么：

   ```bash
   python <repo>/run_iu.py install --dry-run
   ```

5. **把预览结果给用户看，等用户同意**，然后再执行：

   ```bash
   python <repo>/run_iu.py install
   ```

   - 程序复制到 `~/.infiniteusage/app`。skill 和 hook 都指向这里，仓库挪走也不影响
   - 改 Claude Code 设置前会先备份，原来的设置都保留
   - 新开的会话才生效
   - 只想装 skill、不动 Claude Code 设置：`install --agent skills`

## 看效果（装前 / 装后对比账本）

```bash
python <repo>/run_iu.py compare
```

比较的是「每次请求的等效 token」和「每次请求平均带多少上下文」，不比总量（每天干的活多少不一样）。装后请求超过 200 次，数字才比较稳定。

## 调整和卸载

- 换压缩点：`install --compact-at 400000`（想更保守就调大）；Codex 的：`install --codex-compact-at 200000`，`--codex-compact-at 0` 不改 Codex；`--no-compact` 两家都不改
- 试验：Codex 也外包长输出（PreToolUse → `iu hook-pre --codex`，只在 Mac / Linux 上装，默认不装）：`install --codex-offload`。要先在 Codex 里 `/hooks` 信任；在 `workspace-write` 沙箱里连不上免费模型，只会用规则过滤
- 不要外包长输出：`install --no-offload`
- 不要「子 agent 先试免费 agent」：`install --no-delegate`
- 不要往全局说明文件里加常驻规则：`install --no-rules`
- 不要自动放 NOTES.md（Claude Code / Codex 的 hook 都不装）：`install --no-notes`；临时关掉：`IU_NO_NOTES=1`
- 委托顺序：`iu agents --set-chain opencode,kilo`
  - Codex 等付费订阅默认不接，要用就自己加进去
  - 不想让代码进可能拿去训练的模型：设环境变量 `IU_NO_TRAINING_MODELS=1`
- 某个项目不想发给免费模型：在项目根目录建一个空文件 `.iu-private`
- 临时关掉：环境变量 `IU_NO_OFFLOAD=1` / `IU_NO_DELEGATE=1`
- 免费 API：`api list` / `api add <名字>` / `api test <名字>` / `api remove <名字>`；加了好几家时按加的顺序依次试，自建的 FreeLLMAPI 网关（`keys.env` 里的 `FREELLMAPI_KEY`，可选 `FREELLMAPI_SSH_HOST` 走 SSH 隧道）排在最前。加完自动跑资格测试（7 条真实输出 + 有 OpenCode 时 1 个改代码任务，门槛同能力画像），哪类任务没过就不交给它；`api qualify <名字>` 重测、`--no-qualify` 跳过。委托时 OpenCode Zen 额度用完，就按同样顺序把这些 key 临时交给 OpenCode（不改 OpenCode 的配置文件，key 只进子进程环境变量）
- 测速：`speed` 看每个免费模型的真实调用耗时、探测耗时和是否在冷却；`speed --probe` 并行测一遍（OpenRouter 按次数限额，不探测）。新会话开场和 `iu sum` 时超过一小时没测过会在后台自动测；`IU_NO_SPEED_PROBE=1` 关掉。委托时每个免费 agent 最多用剩余预算的 60%（至少 150 秒），后面的才有机会试
- 语言：输出跟着系统语言，固定用 `IU_LANG=zh` 或 `IU_LANG=en`
- 速度：委托和摘要先用免费模型，墙钟时间可能变长（一次测试里委托改代码最多慢约 3 倍）；在意速度就关掉对应的项（上面的 `--no-delegate` / `--no-offload`）
- 卸载（删 skill，删掉常驻规则那一段，精确还原 Claude Code 设置和 Codex 的 config.toml，改之前也会备份）：

  ```bash
  python <repo>/run_iu.py uninstall
  ```

## 已知风险

- **压缩会丢细节**：压缩后 agent 可能忘掉早先读过的内容，需要再读一遍。短任务实测 13/13 成功；**长会话的质量还没有对照实验**。看对比账本，觉得不对就调大 `--compact-at` 或卸载
- **免费 agent 只测过小而明确的修 bug 任务**；大任务的成功率还没测。失败会自动还原，代价是多花约 1~2 分钟
- 常驻规则实测过的：Claude Code（Opus 有规则每次都委托，比只有 hook −14%）；「新会话先读 NOTES.md」那一条在 Codex 上 3/3 次 16/16。WorkBuddy、Cursor、Trae 等只装 skill（已核对它们读的目录：WorkBuddy 读 `~/.workbuddy/skills`，Cursor 读 `~/.cursor`、`~/.agents`、`~/.claude`、`~/.codex` 下的 skills）
- **Codex 提前压缩**：省多少只来自真实日志模拟（21%，悲观 6%），实测的短任务到不了 150K；长 Codex 会话压缩后的质量还没有对照实验。觉得不对：`install --codex-compact-at 0` 或卸载
- **Codex 的 hook 要你信任一次才会运行**：装完在 Codex 里输入 `/hooks` 看一遍再信任。Windows 上先不装 Codex 的 hook（没验证过），只靠规则
- 免费模型可能拿代码去训练；在意的话标 `.iu-private` 或设 `IU_NO_TRAINING_MODELS=1`
