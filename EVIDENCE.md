# 证据：每个机制为什么收录或不收录（v0.2，2026-10-25）

原则：只收录实测有效的机制。数字都能复现，脚本在 `bench/`，结果在 `bench/results/`，说明在 `docs/`。

## 收录

### 1. 约 300K 压缩（`CLAUDE_CODE_AUTO_COMPACT_WINDOW`）

- **为什么有用**：每次请求都要把整段上下文重读一遍。用户的真实会话平均每次请求带 45 万 token，第 200 次请求以后约 70 万。其中约 40% 是助手自己之前的输出，主要是完整的思考内容（`bench/context_residual.py`）。Claude Code 默认要到约 967K 才压缩（[官方文档](https://code.claude.com/docs/en/model-config)），所以长会话几乎从不压缩
- **证据**：反事实模拟（`bench/simulate.py`）在用户过去 7 天的真实日志上（15,328 次请求，模拟基线和实际用量完全一致）：

| 压缩点 | 节省 | 悲观假设下（压缩后要重读到 100K） |
|---|---|---|
| 200K | 59% | 47% |
| **300K（默认）** | **50%** | **43%** |
| 400K | 42% | 36% |

  Mac 上的会话较短，压缩几乎不触发，所以没有效果

- **触发点校准（2026-10-31 更正）**：压缩在窗口减约 33K 处触发：100K 窗口 64~71K，448K 窗口 414~416K（60 次真实压缩），默认 1M 窗口约 967K。所以 300K 压缩 = 窗口 333K。之前按「窗口的 67%」算成了 448K，**旧版标称 300K，实际在约 415K 压缩**；下面和第 9 节「装后真实工作」的数字都是在约 415K 下得到的，300K 只有模拟数字，还没有真实使用数据
- **风险**：压缩会丢细节。短任务实测 13/13 成功（`docs/night_results_2026-10-19.md`，W 组）；**长会话的质量还没有对照实验**。在短任务上它反而贵了 19%：会话短，压缩本身的成本省不回来。所以只适合长会话

### 2. 外包长输出（PreToolUse hook + 免费模型 + 护栏 + 规则兜底）

- **质量**：10 类真实输出（测试失败、构建、搜索、git 等）的压缩质量基准：免费模型命中 97%，编造 0；摘要只有 Claude 原本看到的 5.8%（`docs/benchmark_2026-10-18.md`）
- **护栏**：
  - 摘要里出现原文中不存在的 文件:行 → 丢掉，改用规则过滤
  - 关键报错行没有原样保留 → 自动补上
  - 免费模型失败或超时（30 秒）→ 规则过滤（88% 命中，0 编造）
  - hook 坏了 → `|| true` 原样放行（`docs/phase_A_results.md`）
- **省多少**：
  - Sonnet 长任务冲刺 A/B：花费中位数 -20%，合计 -28%，成功 6/6 vs 5/6（`docs/phase_B_results.md`）
  - 真实日志模拟：-10~13%，**但这是高估**。那次模拟把所有超过 2000 字符的工具输出都算成了被压缩，包括读代码、看图、网页。实际装的 hook 只管白名单里的 Bash 命令。2026-10-19 在 Windows 真实日志上按工具拆分（7 天，6.92B token·次）：
    - hook 白名单直接覆盖的只占上下文 0.2%；测试/构建/git 这类输出合计也只有约 1%
    - 大头是读代码：Bash 的 cat/sed/grep 占 12.3%，Read 读代码占 3.2%。改代码要用原文，不能压缩
    - 其余：图片 4.8%，网页/浏览器 3.1%
    - **在这位用户的真实工作里，外包能省的大约只有 1~2%**；测试/构建很多的工作流（像 Sonnet 冲刺 A/B 那样）才省得多
  - **Opus 上的对照实验还没做**
- **路由**：按任务类型给免费模型打分（能力画像，`docs/profiles.md`），按从强到弱的链调用

### 3. 子 agent 改代码前先试免费 agent（`iu hook-agent` + `iu delegate`，2026-10-19 加入）

- **为什么有用**：真实日志里，改代码的通用子 agent 占子 agent 花费的 91%，平均每个 2.15M 等效 token（`docs/recursion_free_agent.md`）。Opus 本来就会把这类活交给子 agent。hook 让子 agent 先把任务交给免费 agent（OpenCode + Zen 免费模型），由验证命令判断成败：
  - 过了：子 agent 只需审查 diff
  - 没过：全部还原，子 agent 照常自己做
- **免费 agent 写代码的质量**（单元测试判定，付费额度 0）：
  - 单个 bug：deepseek 12/12；另外 6 个免费模型各 6 题，35/36 通过
  - 4 个 bug 一组：3/3
  - 验证失败时，工作区逐字节还原
- **端到端**（真实 Opus 会话，4 个 bug 一组，`bench/e2e_delegate.py`）：

| | 照常 | 先试免费 agent |
|---|---|---|
| S1 | 108K 等效 token，131 秒 | **89K（-17%）**，176 秒 |
| S2 | 101K，129 秒 | **82K（-18%）**，188 秒 |

  - 测试全过，没改测试文件
  - 省下的在子 agent 那部分（6 万 → 4~5 万）。任务越大，省的比例应该越大，但还没测过
  - 代价：每次慢约 50 秒
- **第一次端到端反而贵 40%、慢 6 倍**。查出两个 bug，已修：
  - Claude Code 的 Bash 工具里 stdin 一直开着，OpenCode 卡住
  - 免费 agent 的预算超过了 Bash 工具最多等的 600 秒
- **风险**：
  - 免费线路会看到代码。`.iu-private` 项目不外包
  - 每组样本只有 1 次
  - 测试任务小而明确，真实大任务的成功率还没测

### 4. Codex：约 150K 压缩（`~/.codex/config.toml` 顶层 `model_auto_compact_token_limit`，2026-10-25 加入）

- **为什么有用**：Codex 默认不设这个值，约在上下文窗口的 90% 才压缩：`models_cache.json` 里 272K 窗口的模型（gpt-6-astra 等）约 232K，gpt-6.1-sol（500K）约 427K。用户 Windows 上的 Codex 会话每次请求带的上下文中位 144K、P90 254K；真实压缩 96 次，压缩前中位 224K，压缩后第一次请求中位 41K
- **模拟**（`bench/codex_compact_sim.py`，只读本机日志，思路同 `bench/simulate.py`；权重同 iu 账本：未缓存输入 1、缓存读 0.1、输出 8；模拟基线和实际用量一致）。Windows 最近 14 天 22 个日志、8030 次请求、206.7M 等效：

| 压缩点 | 节省 | 悲观假设下（压缩后要多重读 50K） |
|---|---|---|
| 100K | 26% | —（起点 41K + 50K 太接近压缩点，会一轮接一轮地压缩） |
| 120K | 24% | — |
| **150K（默认）** | **21%** | **6%** |
| 180K | 16% | 8% |
| 200K | 13% | 7% |

  选 150K：乐观和悲观都为正，压缩次数（+137 次）不会太频繁。Mac 上 Codex 会话很短（最大上下文 21K），不触发，0%。结果：`bench/results/codex_compact_sim_{windows,mac}.{txt,json}`
- **小规模实测**（`bench/codex_sprint.py`，Mac，codex 0.157.1，gpt-6-astra medium，`workspace-write` 沙箱；修 bug 冲刺 S1~S3 各 2 次，三组交替跑，一次只跑一个；评分同 `ab_sprint.py`：lib 单元测试全过、没改测试、答案关键字）：

| 组 | 次数 | 成功 | 测试全过 | 平均等效 token | 相对默认 | 平均请求 | 最大上下文 | 压缩次数 |
|---|---|---|---|---|---|---|---|---|
| D 默认 | 6 | 5 | 5 | 111,017 | — | 16.8 | 60,711 | 0 |
| K150（装的值） | 6 | 6 | 6 | 110,010 | −1% | 16.8 | 53,114 | 0 |
| K40（强制压缩） | 6 | 6 | 6 | 111,909 | +1% | 18.0 | 41,624 | 6 |

  - 冲刺任务上下文最多 6 万，**150K 根本不触发**，K150 和默认的配置实际一样，−1% 是噪声（同一配置单次 6.9 万～20.1 万）。所以这组只说明「装了不会让短任务变贵」，**省多少只能看上面的模拟**
  - K40 每次都压缩 1 次，6/6 全过、用量 +1%、多 1.2 次请求、慢约 20 秒：Codex 的压缩在这类任务上没看到丢东西
  - 默认组那次失败：S2 r1 有一个 bug 没修对（没压缩，和压缩无关）
  - 评分修正：Mac 的全局 AGENTS.md 要求中文，S2 的答案写成「每 12 小时」，原关键字只认英文，第一次全判错；`ab_sprint.py` 加了「12 小时」，`bench/report_codex_sprint.py` 按会话记录里的 ANSWERS.md 重判（人工核对过三组答案都对）
  - 花费：19 次（含 1 次试跑）合计 212 万等效 token；Codex 周额度 44% → 50%（同期别的 Codex 使用也算在里面）
  - 结果：`bench/results/codex_sprint_ab.jsonl`、`codex_sprint_ab_report.txt`
- **安装 / 卸载**：`iu install` 只按行改 config.toml 顶层那一行（插在第一个 `[表]` 之前，保留原来的换行符），改前备份，原来那一行记进 `install.json`；`iu uninstall` 还原那一行或删掉，用户的真实 config.toml 副本装卸后逐字节一致。用户后来自己改了这个值，卸载不动它。单元测试 9 个（`tests/test_install.py`）
- **风险**：和 Claude Code 一样，压缩会丢细节，**长会话的质量还没有对照实验**；悲观假设下只省 6%

### 5. Codex 的 PreToolUse hook 能改写命令，但只做成试验选项（2026-10-25）

- **能不能改写**：能。codex 0.157.1 源码（`codex-rs/hooks/src/engine/output_parser.rs`、`codex-rs/core/src/tools/registry.rs`、`tools/handlers/unified_exec/exec_command.rs`）和官方文档一致：`hookSpecificOutput.updatedInput` 只在 `permissionDecision: "allow"` 时生效，Bash（`exec_command`）的 `updatedInput` 必须带字符串 `command`，只替换 `cmd`，别的参数不变。改写发生在执行之前，审批和沙箱照样按改写后的命令走（审批是另一个 `PermissionRequest` 事件），所以 `allow` 不替用户放行。没带 `allow` 的 `updatedInput` 算 hook 失败、按原命令执行
- **做了什么**：`iu hook-pre --codex` 按 Codex 格式输出（带 `allow`、`updatedInput` 只有 `command`）；`iu install --codex-offload` 往 `~/.codex/hooks.json` 加 PreToolUse（Bash），命令以 `|| true` 结尾，卸载精确删掉。`iu sum` 在沙箱里写不了 `~/.infiniteusage` 时，原始输出退到临时目录，写不了账本也不影响退出码
- **为什么默认不装**：
  - 没做端到端实测：Codex 只运行用户信任过的 hook，非交互测试要加 `--dangerously-bypass-hook-trust`，这次被自动权限拦了，要用户决定（在 Mac 的 Codex 里 `/hooks` 信任，或同意测试时用这个开关）
  - Codex 默认的 `workspace-write` 沙箱不让子进程联网，连不上免费模型，只能用规则过滤
  - 能省的不多：Windows 14 天 Codex 日志（`bench/history_potential.py`）里，测试/构建输出只占工具输出花费的 1.1M/71.7M；hook 白名单能管到的搜索、git、测试合计约 31M，占全部 206.7M 用量的 15%，**这是上限**：搜索结果要原文，实际能压缩的远少于这个数

### 6. NOTES.md 跨会话记住要求（规则 + 开场 hook / 插件，2026-10-25）

- **测法**（「跨会话失忆鹈鹕」，`bench/pelican_cross.py --final plain`）：第一个会话里用户分几轮定下 16 条具体要求（颜色、数量、文字），第二个**新会话**只说「画一只骑车的鹈鹕」，完全不提上次；脚本按 SVG 判 16 条各保留没有
- **结果**（每格是每次的得分，满分 16）：

| 宿主 | 不记笔记（X） | 只记 NOTES.md（N） | 笔记 + 规则「新会话先读 NOTES.md」（R） | 笔记 + 开场 hook / 插件（H） |
|---|---|---|---|---|
| Claude Code（Sonnet） | 1、1、1 | 1 | — | 16、15、16 |
| Codex（gpt-6-sol medium） | 2、1、1 | 2、1、1 | **16、16、16** | **16、16** |
| OpenCode（免费模型） | 1 | 16（旧规则让它加载 skill 时顺路读到） | 16 | 16 |

  - 光写笔记没用：Codex 三次都写了 NOTES.md，新会话不提「上次」就不去读，1~2/16
  - 规则或 hook 二选一就够：Codex 上规则 3/3、hook 2/2 都是满分；新会话的用量也没变多（R 组第二个会话 4.0~4.9 万，X 组 3.4~4.7 万）
  - Claude Code 那次 15/16 是脚本把同色的腿数成了光芒，人工核对 16/16
- **装了什么**：常驻规则写进 Claude Code 和 Codex 的全局说明（新会话先读 NOTES.md，用户这次说的优先），两家都用 SessionStart hook（`iu hook-compact`，startup + compact）。宿主只做这两家；OpenCode 只当免费工人，插件不再装（留在 `bench/opencode_notes.js` 复现上表）。Codex 的 hook 要在 `/hooks` 里信任一次；Windows 上暂不装 Codex hook，靠规则
- **花费**：Codex 全部 11 次周额度约 7 个点；OpenCode 免费

### 7. 常驻规则在 Claude Code 上是净省（A/B，Opus，2026-10-25）

- 详见 `docs/agents_landscape_2026-10.md` 4.1。Claude Code 已经有 hook，担心规则只是固定开销；修 bug 冲刺 S1~S3，三组都开着全局 hook
- **结果**（委托正常的那几次）：B 只有 hook 6 次平均 101,722 等效 token、75 秒；A 加规则 3 次平均 87,435（**−14%**），美元 −11%，9/9 全过。没有规则时 Opus 主会话 0 次加载 skill、0 次委托，规则是让主会话用上免费 agent 的唯一途径
- **代价是慢**：委托要 2~4 分钟，A 平均 254 秒
- **免费服务挂了时**（2026-10-25 下午 Mac 上 OpenCode 免费额度用完，`Free usage limit reached`）：旧版 `iu delegate` 让 OpenCode 一直重试、卡满 420 秒预算，7 次平均 134,949（比 B **+33%**）、每次 8~9 分钟、1 次答错（结果单独存在 `bench/results/rule_ab_contaminated.jsonl`，不计入上面）。已修，见第 8 节
- 修过的新版规则（NOTES 只记用户说的具体要求、委托前台等待）这次没测成：重跑时正好撞上额度用完。旧写法的 N 组 3 次平均 102,072（−1.5%），一次把委托放后台轮询，所以改了规则

### 8. 免费模型测速 + 额度用完马上换（`iu/speed.py`，2026-10-25）

- **测速**：每次真实调用（`iu sum` / `iu ask` / `iu delegate`）记下耗时和成败；新会话开场或 `iu sum` 时超过 1 小时没测，就在后台并行探测所有免费 API（OpenRouter 按次数限额，不探测）。排序：刚失败的放最后；其余估计耗时加 5 秒余量后每慢 1.5 倍一档，同一档保持能力画像从强到弱的顺序
- **实测**（Windows，FreeLLMAPI）：6 个模型并行探测 2.6 秒，发现路由表里的 nemotron-3-super-120b 已下线（404）；同一条 25.6 万字符的测试输出，第一次排第一的模型 30 秒超时退回规则过滤，记下后第二次 4.3 秒拿到免费模型摘要
- **委托**：每个免费 agent 最多用剩余预算的 60%（至少 150 秒），卡住的不会吃光整个预算；OpenCode 加 `--print-logs`，一看到「额度用完」类报错就停，同一个 agent 的其他免费模型（共用额度）这次不再试，直接换下一家。Mac 实测：额度用完时从卡满 419 秒变成 **4 秒**判定失败
- **额度用完换自己的 key**：OpenCode Zen 的免费模型额度用完后，`iu delegate` 把用户 `iu api add` 加的免费 API 临时交给 OpenCode（`OPENCODE_CONFIG_CONTENT` 只写环境变量名，key 只进子进程环境变量，不改用户的 OpenCode 配置）。Mac 实测（Zen 额度已用完，后备用本机 FreeLLMAPI）：Zen 4 秒判定用完，换后备 17 秒修好，验证通过，全程 **22 秒**；下次测速表直接把 Zen 排到后面
- **加模型自动资格测试**（`iu/qualify.py`）：`iu api add` 加完，用能力画像同一套规则和门槛（质量 ≥ 85%、编造 0、失败 ≤ 30%）测 7 条真实输出（构建 4、git 1、搜索 1、读文件问答 1；语料来自 FreeLLMAPI，MIT，作者和邮箱已匿名），有 OpenCode 时再让它用这个 key 修一个小 bug（单元测试判定）。没过的那类任务以后不交给它；没过、也没编造的再测一轮合起来判；一次回答都没拿到（限流、网络）记「没测成」，不拦。Mac 实测：FreeLLMAPI `auto` 5 类全过，约 1.5～2 分钟；`qwen3.5-9b` 构建摘要过，其余三类被限流 / 502，改代码 240 秒超时没过
- **安全**：交给免费 agent 的任务说明先脱敏；OpenCode 子进程拿不到环境变量里别的 key / token（只有我们给的那一个）；`iu api add` 拒绝非本机的 http 地址
- 单元测试：`tests/test_speed.py`（18 个）、`tests/test_qualify.py`（12 个）

### 9. 压缩点按本机日志选，效果按会话算（`iu/compactsim.py`，2026-10-26）

- **为什么**：300K 是在作者自己的日志上选的（会话常长到 50 万～100 万）。别人装上后用 `iu compare` 看到「每次请求 +23%」：他装前 7 天平均上下文 215K、一次没压缩过，装后一天里碰巧有几个长会话。按天汇总的对比被会话长短带偏，说明不了压缩有没有用；而会话本来就短的人，提前压缩也确实帮不上忙
- **install**：第一次装时在本机最近 14 天日志上重放 300K / 400K / 500K / 600K（`bench/simulate.py` 的压缩部分，悲观假设：压缩后起点 6 万、摘要 8K）。和最好的差不到 2 个百分点的里面选压缩最少的；悲观下不省或省不到 3% 就不改压缩设置；日志少于 300 次请求先用 300K。重装沿用上次的点，`--compact-at auto` 只用第一次装之前的日志重新模拟（装后的会话已被压缩过，看不出不压缩会长多长）
- **实测**：作者 Windows（装之前 14 天，21,238 次请求）：300K 省 55%（悲观 52%）、400K 48%、500K 38%、600K 35% → 选 300K。Mac（最近 14 天，15,847 次请求，会话短）：300K 省 8%、400K 0.6%、500K 以上 0
- **compare 按会话算账**：装后每次提前压缩，不压缩的话压掉的上下文之后每次请求都要再读一遍（缓存命中按读 0.1，过期按写），减去压缩本身（读一遍旧上下文 + 写摘要，按 `compact_boundary` 里的 preTokens / postTokens 估）；约 950K 时默认设置也会自己压缩，之后不算。接着旧会话继续时旧记录会抄进新文件，message id 和压缩记录跨文件去重。作者 Windows 装后 7 天：47 次提前压缩，花约 4.0M、之后少花约 244M 等效 token，净省 43%（和模拟一致）；1 次亏（压缩完会话就结束了）；83 个会话里 54 个没长到压缩点
- 单元测试：`tests/test_compactsim.py`（7 个）、`tests/test_install.py` 新增 4 个

## 测了但不收录

| 机制 | 结果 | 出处 |
|---|---|---|
| 交接文件 + 开新会话 | -11%，耗时 +156% | phase_B_results.md |
| ponytail | 合计 +11%，多 1 次失败 | phase_B_methods_results.md |
| 调小 Bash / 读文件输出上限 | +8%，多 1 次失败 | phase_B_methods_results.md |
| 降推理强度（Sonnet medium / Opus medium） | +2% / +6% | 同上 + night_results |
| 探索闸门（搜代码交给免费 agent） | Sonnet -11%（区间跨过 0）；Opus -2% | night_results_2026-10-19.md |
| 只保留最近 N 个工具输出、删旧思考（模拟） | +6~10%：清理频繁，缓存一直重建 | bench/simulate.py |
| 主线程从 Opus 换成 Sonnet / Haiku | 更贵：Opus 用的 token 只有 Sonnet 的 1/4 | night_results_2026-10-19.md |
| rtk / headroom / context-mode（别人的独立测试） | +7.6% ~ +72% | research/raw/09_prior_benchmarks.md |
| headroom 0.41.0 代理（我们自己重测，3 组 Sonnet 冲刺） | +2.2%，测试都全过 | bench/results/e2e_headroom.jsonl |
| 压缩太频繁（每约 6.7 万压缩一次，「失忆鹈鹕」，Sonnet 各 1 次） | 压缩 7 次：16 条规格全忘（0/16），用量 +2%；加记笔记：12/16，用量 +27%；不压缩 16/16 | docs/visual_compaction_test_plan.md |
| 代码地图 MAP.md（新会话先读地图再找代码；零成本上限测试，Windows 228 个新会话、22.8 亿等效 token，`bench/session_explore.py`） | 第一次改文件之前的探索（含携带）只占 4.4%，地图可省上限 1.5%（严格）～3.8%（宽松），没到 15% 门槛，没做 | docs/code_map_feasibility.md |
| 另装记忆框架防「跨会话失忆」（「跨会话失忆鹈鹕」，`bench/pelican_cross.py`，Mac Sonnet，12 次合计约 5.95 美元） | 新会话提「上次的规格」：不记笔记 0/16、0/16、14/16（这次新会话自己翻 `~/.claude/projects/` 读到另一次测试的会话记录，串数据，人工 16/16），隔离重跑 0/16；Claude Code 自动记忆一个文件都没写，正常表现是请用户重发（没乱编）；记 NOTES.md 1 次，人工 16/16。新会话完全不提上次：不记笔记 1/16 ×3、只有 NOTES.md 1/16，NOTES.md + 开场 hook 16/16、16/16（脚本 15/16，把同色的腿数成光芒）、16/16。被问到「上次」时 Claude Code 会主动翻磁盘上的旧会话记录，但 4 次里只用上 1 次。不另装框架；开场 hook 已做进 iu hook-compact（SessionStart startup） | docs/code_map_feasibility.md |
| 跨会话失忆换成 Codex / OpenCode（`bench/pelican_cross.py --host`，新会话不提上次，Mac 第一轮各 1 次；加样本后的结果见上面第 6 节） | Codex（gpt-6-sol medium）：不记笔记 2/16、只记笔记 2/16、笔记 + 规则「新会话先读 NOTES.md」16/16、笔记 + SessionStart hook 16/16，周额度不到 1%。OpenCode 免费模型：不记笔记 1/16，其余三组 16/16（只记笔记那组是旧规则让它加载 skill 时顺路读到的）。所以规则和 hook 都做进了 iu install：规则写进各家全局说明，Codex 用 hooks.json，OpenCode 用插件 | docs/code_map_feasibility.md |
| 长会话里的小问题挪到新会话 / 免费模型（`iu ask`；零成本上限测试，Windows 2235 个用户回合、23.4 亿等效 token，`bench/small_questions.py`） | 短的只读回合（≤120 字、≤5 次请求、不改文件、上下文 ≥5 万）占 6.4%，扣掉挪不走的缓存重建后 3.3%；其中岔题只占 0.9%（扣重建后 0.65%），没到 5% 门槛，不做 `iu ask`，规则里只加一句提醒 | docs/small_questions.md |
| 真实日志：压缩后有没有变笨（Windows，装前 14 天 vs 装后 5.5 天，`bench/compact_quality.py`，只读日志） | 刚压缩完的 3 句里用户纠正率：装前 1/52、装后 1/54，没有变高；压缩后重读压缩前读过的文件：10% → 21%（多花一点，已算在 −34% 里） | 样本小，只能排除「明显变笨」；正则只认得常见的纠正说法 |

## 局限

- A/B 实验用的是公开仓库里的短冲刺任务（上下文 4~16 万），和真实长会话不一样；压缩的证据来自对真实日志的模拟（只算 token，不评估质量）
- 花费是 API 等价价格，不是订阅额度 %
- 下一步：在接近真实规模的长 Opus 会话上做 A/B（压缩 + 外包 vs 不装），并用官方额度 % 核对
