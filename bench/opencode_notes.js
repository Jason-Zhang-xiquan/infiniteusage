// InfiniteUsage（iu install 自动加的，iu uninstall 会删掉这个文件）：把项目根目录的 NOTES.md 放进 OpenCode 的系统提示。
// 和 Claude Code / Codex 上的 iu hook-compact 一样：新会话开场读一次，每次压缩之后重读一次。
// 实测（docs/code_map_feasibility.md）：新会话不提上次时，只有 NOTES.md 1/16，开场把它塞进上下文 16/16。
// 不是每次请求都重读：笔记一改系统提示就变，前面的缓存全部作废，所以每个会话记一份，压缩后才换。
import fs from "node:fs"
import path from "node:path"

const MAX = 9000
const seen = new Map() // 会话 id -> 这个会话放进系统提示的那段文字（空字符串 = 没有笔记）

function load(dirs) {
  for (const d of dirs) {
    const p = path.join(d, "NOTES.md")
    let text
    try {
      text = fs.readFileSync(p, "utf8")
    } catch {
      continue
    }
    if (!text.trim()) continue
    const cut = `\n…(truncated here: read ${p} for the rest)`
    if (text.length > MAX) text = text.slice(0, MAX) + cut
    return (
      `Loaded by the InfiniteUsage plugin the user installed. This is the project notes file ${p}, where ` +
      "requirements and decisions from earlier sessions in this project are recorded. Follow the ones that apply to " +
      "what the user asks; if the user now says something different, the user's latest words win. Keep the file up " +
      "to date.\n\n--- NOTES.md ---\n" + text
    )
  }
  return ""
}

export const InfiniteUsageNotes = async ({ directory, worktree }) => {
  const dirs = [...new Set([directory, worktree].filter(Boolean))]
  return {
    event: async ({ event }) => {
      if (event?.type === "session.compacted") seen.delete(event.properties?.sessionID)
    },
    "experimental.chat.system.transform": async (input, output) => {
      if (process.env.IU_NO_NOTES) return
      const key = input?.sessionID || ""
      if (!seen.has(key)) seen.set(key, load(dirs))
      const text = seen.get(key)
      if (text) output.system.push(text)
    },
  }
}
