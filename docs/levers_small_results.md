# 大杠杆小范围验证（2026-10-18，部分完成）

## A 探索外包给免费 agent（OpenCode，免登录，plan 只读 agent）：很有希望

在 FreeLLMAPI 仓库上问 10 个查代码的问题，agent 要自己找文件。

| 模型 | 答对 | 编造位置 | 耗时中位数 |
|---|---|---|---|
| deepseek-v4.1-flash-free | 10/10 | 0 | 12s |
| longcat-2.5-preview-free | 10/10 | 1（多半是误报：文件在 client 目录，检查只搜了 server） | 24s |
| space-bunny-free | 10/10 | 1（同上） | 14s |

不花 Claude 额度。**还没测：Claude 把探索交出去以后，实际省多少。**

## B / E 主线程换模型、提前压缩（Sonnet 冲刺任务，S2 和 S3 各 1 次）

| 冲刺 | 组 | 成功 | 花费 | 相对 Sonnet |
|---|---|---|---|---|
| S2 | Sonnet 基线 | ✓ | $1.44 | — |
| S2 | Opus | ✓ | $0.39 | -73% |
| S2 | Haiku | ✓ | $0.35 | -76% |
| S2 | Sonnet + 100K 压缩窗口 | ✓ | $0.86 | -40%（压缩 1 次） |
| S2 | Opus + 100K 压缩窗口 | ✓ | $0.36 | -75% |
| S2 | Opus + effort medium | ✓ | $0.45 | -69% |
| S3 | Sonnet 基线 | ✓ | $0.64 | — |
| S3 | Opus | ✓ | $0.35 | -44% |
| S3 | Haiku | ✓ | $0.32 | -50% |
| S3 | 其余 3 组 | — | $0 | **用量到上限，Claude 没有工作，作废** |

**初步看法（每格只有 1 次，不能当结论）：**

- 在这类任务上，Opus 比 Sonnet 便宜：轮数少一半，上下文更小
- 提前压缩能触发，在 Sonnet 上省了 40%，任务也成功了
- 花费是 Claude Code 报的 API 等价价格；订阅额度里不同模型的扣法可能不一样，要用官方额度 % 再核对一次

## 内存管理

全程一次只跑一个会话，vitest 最多 2 个 worker，node 堆上限 2GB，内存看门狗一直开着。可用内存一直在 88~89%，没有触发中止。

## 探索外包的触发问题（后来补测）

- 只在系统提示里写「查代码先用 iu explore」：agent 能复述这条规则，但做任务时 **0 次调用**。说明提示词不可靠
- 改用 hook 强制（`iu hook-explore-gate`）：在试点项目里，agent 自己搜代码（Grep / Glob / rg / grep -r / find / git grep）会被拒绝，并被告知改用 iu explore。读文件照常放行；同一会话被拒 4 次后放行，免得卡死
  - S2 实测：被拒 2 次后改用 iu explore 2 次，引用全部核实，任务成功

## 今晚的大规模研究（已排队，Mac 串行）

1. 补跑 S3 作废的 3 组（W、OPUS_W、OPUS_E）→ `/tmp/iu_night_s3fix.jsonl`
2. 8 组交错跑：C0、OPUS、HAI、W、OPUS_W、OPUS_E、X（Sonnet + 探索闸门）、OPUS_X（Opus + 探索闸门），3 个冲刺 × **1 次** = 24 次 → `/tmp/iu_night.jsonl`
   - 本来每组跑 3 次；用户周额度只剩 10%，缩成每组 1 次（今晚合计约 27 次，约 $16，约占周额度 1~2%）
   - 剩下的 2 次重复（48 次）等周额度重置后再补

保护措施：一次只跑一个；内存看门狗；vitest 2 个 worker；node 堆上限 2GB；撞上用量上限就等 30 分钟重试（最多 8 次）。日志在 `/tmp/night.log`。

## 还没做

- S3 那 3 组（Sonnet + 压缩、Opus + 压缩、Opus + effort medium），等额度恢复后补跑
- 今晚的大规模研究**还没安排**：撞上了额度上限，现在排了也会全部作废
