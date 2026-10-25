# 用量账本 `iu report`

目标：**在不降低工作效率和结果的前提下，把 Claude Code / Codex / WorkBuddy 等 agent 的用量猛猛压低。**

调研结论见 [research/README.md](../research/README.md)。

## 第一步：跨平台额度账本 `iu`（最小版）

先把用量量清楚，才知道该砍哪里，砍完也能证明省了多少。只用 Python 标准库，Windows 3.12 和 Mac 自带的 3.9 都能跑。

```bash
python run_iu.py
```

```bash
python run_iu.py --days 14 --mac <ssh主机>
```

报告内容：

1. **官方额度和重置倒计时**
   - Codex：直接取会话日志里 `token_count` 事件自带的 `rate_limits`（已用 %、窗口、`resets_at`）
   - Claude：日志里没有额度百分比，只有 statusline 的输入里有。装上记录器后每次刷新状态栏存一次快照；没装时按 ccusage 的办法估 5 小时窗口
2. **按模型 / effort**：请求数、新输入、缓存写、缓存读、输出、缓存命中率、等效 token
3. **用量最多的项目**，以及平均每次请求带多少上下文
4. **每天等效 token**
5. **浪费信号**：按影响排序，每条说清原因和怎么改

「等效 token」= 按公开 API 价格比例折算成「未缓存输入 token」，用来比较钱和额度花在哪，**不是官方额度算法**。
权重：Claude 输入 1、缓存写 1.25（5 分钟）/ 2（1 小时）、缓存读 0.1、输出 5；Codex 未缓存输入 1、缓存读 0.1、输出 8。权重在 `iu/report.py` 的 `WEIGHTS` 里。

### 数据来源和去重

| 工具 | 读哪里 | 去重 |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl`（含 `subagents/`） | `message.id + requestId` |
| Codex | `~/.codex/sessions`、`~/.codex/archived_sessions` | 新版按 `token_usage_record.response_id`；旧版用 `total_token_usage` 的差值 |

增量缓存在 `~/.infiniteusage/cache-*.json`：只读日志新追加的部分，第一次全量约 10 秒，之后不到 1 秒。
`--mac` 会把 `iu/` 用 ssh 传到远端的 `~/.infiniteusage/app` 再运行 `export`，结果合并进同一份报告（额度按账号算，所有机器的用量都算进去）。

### Claude 官方额度记录器

```bash
python run_iu.py install-statusline --dry-run
```

```bash
python run_iu.py install-statusline
```

会先备份 `~/.claude/settings.json`；如果已经有别的 statusLine 就不覆盖。装上后状态栏显示：

```
Opus 5.5 · 5小时 24% ↻1时11分 · 7天 41% ↻3天11时 · 上下文 34% · 缓存命中 93%
```

快照存在 `~/.infiniteusage/claude_limits.json`，百分比每变一次追加到 `claude_limits_history.jsonl`，以后用来算「1% 额度 ≈ 多少 token」。

## 下一步

- 用历史快照校准：Claude 和 Codex 每 1% 额度对应多少 token，把等效 token 换成「额度 %」
- 把浪费信号做成可执行的修改（effort 默认值、CLAUDE.md 瘦身、换话题自动提醒 /clear）
- 同一批任务开/关省 token 工具（rtk、serena、Graphify 等）的前后对比
