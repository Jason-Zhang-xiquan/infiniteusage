# 递归 + 免费 agent 写代码：研究结果（2026-10-19）

思路：Opus 只做主控。自成一体、能用命令验证的改代码子任务先交给免费 agent（OpenCode + Zen 免费模型），用验证命令判断成败：
- 过了：Claude 只审查 diff
- 没过：自动还原，Claude 照常自己做

## 1. 真实日志：钱花在哪（Windows，7 天）

- 子 agent 占总花费约 13%。其中**改文件的通用子 agent 占子 agent 花费的 91%**：平均每个 2.15M 等效 token、46 次请求
- 只读的子 agent（Explore 等）只占总花费约 1%，交给免费模型意义不大
- 超过 15 次请求的「重活」占总花费 64.5%，其中 74% 是在重读开始时已有的约 435K 上下文

## 2. 模拟（`bench/simulate.py`，同一份日志，855M 等效 token）

| 做法 | 节省 |
|---|---|
| 300K 压缩（已装） | 49% |
| 递归：超过 15 次请求的活交给子 agent | 41% |
| 300K 压缩 + 递归 | 53% |
| 300K 压缩 + 递归 + **免费 agent 做成一半** | **61%** |
| 外包长输出 + 300K 压缩 + 递归 + 免费 agent 做成一半 | **63%** |

- 递归和压缩压的是同一块（重读旧上下文），所以叠加后只多省 4 个百分点
- **免费 agent 是在递归之后再砍子 agent 的花费**：做成一半时，在「压缩 + 递归」的基础上再省 18%
- 模拟的假设：
  - 子 agent 起步 35K 上下文，交回 3K 结果
  - 子 agent 跑超过 55 分钟，主会话的缓存过期，要按缓存写重新计价
  - agent 行为不变
- 模拟**不评估质量**

## 3. 实测：免费 agent 能不能独立写对代码（零付费额度）

任务来自 FreeLLMAPI 源码（公开 MIT 项目），单元测试判对错，不信模型自己说「好了」。

| 测试 | 结果 |
|---|---|
| 单个 bug（12 个，`bench/free_coder.py`，deepseek-v4.1-flash-free） | **12/12 通过**，中位数 37 秒，最长 70 秒。每次只改出 bug 的那一个文件，没碰测试；9/12 的改法和原代码一模一样 |
| 一个代码库里同时有 4 个 bug（3 组，走真正的 `iu delegate`，`bench/free_sprint.py`） | **3/3 通过**：122、135、295 秒。第 3 组 deepseek 没做成，自动换 space-bunny-free 做成。都没改测试文件 |
| 还原测试（验证命令必失败） | 两个模型都没通过 → 工作区和调用前逐字节一致（git status + diff 哈希） |

对照：同样的冲刺任务，Opus 新开会话做，中位数约 97K 等效 token，12/12 成功（`night.jsonl`）。

## 4. 做好的原型（还没装进 Claude Code）

- `iu delegate --verify "<命令>" --brief-file <文件>`（`iu/delegate.py`）
  - 先做 git 快照，再让免费 agent 改代码，然后跑验证
  - 失败就全部还原：包括新建的文件、暂存过的文件、原来就没被跟踪的文件
  - 单元测试：`tests/test_delegate.py`
- `iu hook-agent`（`iu/hook.py`）：Opus 调用通用子 agent 时，在任务前面加一段说明，让子 agent 先试 `iu delegate`，失败再自己做
  - 不需要说服 Opus 改变习惯：它本来就会委派
- 护栏：
  - `.iu-private` 项目不外包
  - 必须是 git 仓库
  - 验证命令由调用方给
  - 改动不提交，留给 Claude 审查

## 局限（宣传时要说清楚）

- 测试任务是**小而明确、有现成测试**的修 bug。真实子 agent 的活更大、更模糊，成功率会更低，所以模拟只按 50% 算
- 免费线路会看到代码，可能留存数据。私有项目要标 `.iu-private`
- 端到端只测了 2 组、每组 1 次（见第 5 节），任务都很小；真实大任务的省钱比例和成功率还没测
- 每次委托会慢约 50 秒（免费 agent 改代码 + 子 agent 审查）

## 5. 端到端实测（2026-10-19 下午，真实 Opus 会话）

`bench/e2e_delegate.py`：Opus 主控把「修 4 个 bug」交给一个通用子 agent。两组都关掉长输出外包，只比较 hook-agent。

| | 照常 | 子 agent 先试免费 agent |
|---|---|---|
| S1 | 108K 等效 token，131 秒 | **89K（-17%）**，176 秒；免费 agent 84 秒做成 |
| S2 | 101K，129 秒 | **82K（-18%）**，188 秒；免费 agent 103 秒做成 |

第一次跑时 hook-agent 那组反而**贵 40%、慢 6 倍**：免费 agent 两次都卡到超时，一个文件都没改。原因有两个，已修：

1. Claude Code 的 Bash 工具里 stdin 是一直开着的管道，OpenCode 会等它读完 → 子进程一律 `stdin=DEVNULL`（`iu explore` 也有同样的问题，一起修了）
2. 免费 agent 的预算（900 秒）超过了 Bash 工具最多等的 600 秒 → 默认改成 420 秒，验证 150 秒

排查中还发现，基准脚本按名字 `pkill -f vitest` 清理残留，会误杀整台机器上别的 vitest，包括并行跑的 `iu delegate`。现在改成只杀自己启动的那组进程。

写代码用的模型顺序（`iu/delegate.py` 的 `CODER_MODELS`）：deepseek → glm-5.3-flash → space-bunny → nemotron-3-ultra → mimo → beam。数据见 `docs/free_providers.md`。
