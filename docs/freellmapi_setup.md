# FreeLLMAPI 安装记录（Mac mini，2026-10-18）

免费 API 的统一入口：一个 `/v1` 地址，后面接很多家免费 API，一家额度用完自动换下一家。选型理由见 [research/06_免费外包方案.md](../research/06_免费外包方案.md)。

## 装在哪、怎么跑的

| 项 | 值 |
|---|---|
| 代码 | Mac `~/freellmapi`，正式发布版 **v0.15.0**（git tag，不是 main 分支） |
| Node | Node 22（brew `node@22`，keg-only，不影响默认的 Node 26）。项目要求 Node 20~24 |
| 配置 | `~/freellmapi/.env`（权限 600）：随机生成的 `ENCRYPTION_KEY`、`PORT=3001`、`HOST=127.0.0.1`、`NODE_ENV=production` |
| 数据 | `~/freellmapi/server/data/freeapi.db`（provider key 用 AES-256-GCM 加密存储） |
| 日志 | `~/freellmapi/logs/server.log` |
| 监听 | **只监听 Mac 本机 127.0.0.1:3001**，外网和局域网都访问不到 |
| 对外连接 | 每 12 小时从 api.freellmapi.co 拉一次签名的模型清单（README 说不会发送提示词和 key） |

启动（目前用 nohup 手动启动，**Mac 重启后不会自动起来**）：

```bash
ssh <服务器> 'cd ~/freellmapi && nohup /opt/homebrew/opt/node@22/bin/node server/dist/index.js > logs/server.log 2>&1 < /dev/null &'
```

从 Windows 打开后台（SSH 隧道，把 Windows 的 3001 端口接到 Mac 本机的 3001）：

```bash
ssh -N -L 3001:127.0.0.1:3001 <服务器>
```

然后浏览器打开 http://localhost:3001 。

## 需要你自己做的

1. **建管理员账号**：在后台填你自己的邮箱和密码。通过隧道访问时服务器认为请求来自本机，所以不需要设置码
2. **打开免 key 的提供商**（Keys 页面）：
   - Kilo Gateway：官方允许匿名使用，每个 IP 每小时 200 次。**提示词会被拿去训练**，只用于测试和不敏感的内容
   - OVH AI Endpoints：官方匿名模式，每个模型每分钟约 2 次
   - **不要打开 AI Horde**：由志愿者运行模型，志愿者能看到提示词
3. **复制统一 API key**（Keys 页面顶部，`freellmapi-…`），在 Mac 上存成一个只有你能读的文件，之后的测试和 `iu ask` 都从这里读：

   ```bash
   ssh <服务器>
   ```

   ```bash
   mkdir -p ~/.infiniteusage && umask 077 && nano ~/.infiniteusage/keys.env
   ```

   文件内容只有一行：`FREELLMAPI_KEY=freellmapi-...`

4. 之后注册 Groq、Google AI Studio，拿到 key 后在 Keys 页面添加。**每家只放 1 个 key**

## 安装过程中出的事故（已修复）

`brew install node@22` 顺带把共享库 simdjson 从 4.6.6 升到了 5.0.5。默认的 Node 26.7.0 链接的是旧库，于是启动不了。
修复方法是 `brew upgrade node` 把默认 Node 升到 26.12.0（同一个大版本），已验证 `node -v`、`npm -v` 正常。
以后在 Mac 上用 brew 装东西，装完要检查 `node -v`。

## 测试结果（2026-10-18 凌晨）

用户已经建好账号，打开了 Kilo 和 OVH，统一 key 用 `scripts/save_freellmapi_key.sh` 存进了 Windows 和 Mac 的 `~/.infiniteusage/keys.env`（权限 600）。

| 测试 | 结果 |
|---|---|
| `/v1/models` | 262 个模型 |
| Mac 本机，OpenAI 格式，`auto` | ✅ 2 秒，实际走 `ovh/Qwen3.8-27B` |
| Windows 经隧道，Anthropic 格式 `/v1/messages`，`auto` | ✅ 8 秒，实际走 `ovh/Qwen3.6-27B` |
| Windows 经隧道，`kilo-auto` | ❌ 60 秒超时（502）。只配了这一条线路，没有可换的 |

发现的问题：

- **思考过程混进了回答**：OVH 的 Qwen 模型把推理过程直接写在正文里。`max_tokens` 小的时候正文会被截断，还会白白多花 token。要么换不写思考过程的模型，要么在 FreeLLMAPI 里过滤掉
- **Kilo 匿名线路超时**：原因不明，可能只是暂时的
- 模型列表里有 `claude-opus-4-5` 这类名字，是映射到免费模型的别名，**不是真的 Claude**

## 明天接着做

- [ ] 查清楚 Kilo 为什么超时；处理思考过程混进正文的问题
- [ ] 用户注册 Groq、Google AI Studio，在 Keys 页面添加（每家 1 个 key）
- [ ] 测故障转移：一家失败时，能不能自动换到下一家
- [ ] 做 `iu ask` / `iu sum`，调用这个入口
- [ ] 开机自启（launchd）、Windows 不开隧道也能直接访问：都要等用户同意

## 还没做

- [x] 用户建账号、打开 Kilo / OVH、存统一 key → 跑通一次真实调用
- [ ] 开机自启（launchd），要等你同意
- [ ] Windows 不开隧道也能直接调用（绑定 Tailscale 地址 + 统一 key 鉴权），要等你同意
