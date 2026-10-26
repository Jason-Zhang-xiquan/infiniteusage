---
name: infiniteusage
description: Cut paid model usage without lowering result quality. Hand self-contained, command-verifiable code changes to free coding agents on this machine (iu delegate), compress long command output with free models (iu sum), find other coding agents and free APIs available here (iu agents), and keep long sessions compaction-friendly. Use at the start of a coding task, before doing or delegating a code change that a test/build can verify, before running commands with long output (tests, builds, git log/diff), whenever a tool result starts with "[iu", and when the user asks about saving tokens, usage or quota.
---

# InfiniteUsage: same results, fewer paid tokens

In this file `iu` means this exact command (works in any shell): **{{IU}}**
(If you see the placeholder unrendered, use `python3 ~/.infiniteusage/app/run_iu.py`, or `python` on Windows.)

Talk to the user in their language. Every mechanism below was measured; numbers are at the end.

## 0. Once per session: what is available here

Run `iu agents`. It lists the coding agents found on this machine (free / bring-your-own-key / paid subscription), which free APIs are configured, whether the network looks like mainland China, and what could be installed or registered.

- If no free coding agent is found, show the user the suggestions and **ask before installing anything**.
- Free APIs (used by `iu sum` / `iu ask`, and by `iu delegate` as OpenCode's backup models once its own free quota runs out): the user registers at the provider's own site, one account per provider, official free tiers only, then adds it **in their own terminal**: `iu api add gemini` (presets: gemini, groq, openrouter, cerebras, mistral, nvidia, zhipu, siliconflow, dashscope; any other OpenAI-compatible endpoint: `iu api add <name> --base-url <url>`). The key is typed into a hidden prompt and stored in `~/.infiniteusage/keys.env`. `iu api list` shows the sign-up links and each model's qualification result, `iu api test <name>` checks one. `iu api add` runs a qualification test right after adding (usually 1-3 min); task types a model fails are never sent to it. A self-hosted FreeLLMAPI gateway also works (docs/freellmapi_setup.md).
- **Never register accounts, create keys, or type or paste API keys for the user**, and never ask them to paste a key into the chat. `iu api add` refuses to run without a real terminal for this reason.
- The delegation order lives in `~/.infiniteusage/agents.json`. Paid subscriptions such as Codex are not used unless the user adds them: `iu agents --set-chain opencode,kilo`.

## 1. Code changes: try a free coding agent first

Use this for a **self-contained change that a command can verify**: fix failing tests, implement a function against existing tests, a small refactor where the build and tests must stay green, or a lint fix. This applies both when you would do it yourself and when you are about to spawn a sub-agent for it.

1. Prepare a complete brief: the goal, the relevant file paths, the constraints, and the exact verification command.
2. Pass the brief on stdin; do not write it to a file outside the project, because some agents block that. Run it blocking, NOT in the background, with tool timeout 600000 ms:
   ```
   iu delegate --verify "<command that proves it works>" <<'BRIEF'
   <the brief>
   BRIEF
   ```
   If your shell has no heredoc (PowerShell), pass the brief as one quoted argument instead: `iu delegate --verify "..." "<brief>"`.
   If you are yourself one of the agents in the delegation order, `iu delegate` skips that agent automatically.
3. If it prints `委托成功 / delegated OK`: the change is in the working tree and not committed. Review the printed diff, read the changed files if needed, fix anything wrong or missing, run the check once yourself, then continue.
4. If it prints `委托失败 / delegation failed`: every change was already rolled back. Do the task yourself as usual.

Do not delegate:
- design decisions
- work that depends on this conversation's context
- anything with no verification command
- secrets in the brief

Projects that contain a `.iu-private` file are never sent out.

## 2. Long command output: read a summary, not 20,000 characters

- If a Bash result starts with `[iu 免费模型 free model …]`, a hook already summarized it with a free model. Trust the exit code and the quoted error lines; they are verbatim.
- If your host has no such hook, wrap commands with long output yourself:
  `iu sum --goal "<what you need to know>" -- <command>`
- For details, read the raw log it names with line ranges. **Never re-run the command just to see more.** Don't pipe through `| head` / `| tail`, because truncation can hide the error.

## 3. Long sessions: compaction-friendly

Paid cost is roughly turns × context: every request re-reads the whole context. `iu install` makes Claude Code compact earlier than the default ~967K (300K-600K, picked by replaying this machine's own logs; left alone if sessions rarely get that long), and Codex at about 150K. `iu compare` shows per session what each early compaction cost and saved.

Compaction summaries drop things. In a measured test (Sonnet, frequent compaction) the user's 16 agreed requirements were all lost without notes; with notes 12/16 survived, and the 4 that were lost were exactly the small details the model had not written down. So notes go in a file, and they must be complete:

- **Keep `NOTES.md` in the project root.** Write down **every requirement the user states, one line per item, verbatim values included** (colors, names, numbers, exact strings, file names), including details that look minor or cosmetic. Do not summarize or pick the "important" ones; if the user said it, it goes in. One-off task descriptions ("fix these tests") do not need to be copied in.
- **Update it in the same turn the requirement is given or changed**: replace the old value instead of appending a contradiction, and mark removed items as removed. Also note decisions, file:line locations and open TODOs.
- **At the start of every new session, if the project root has `NOTES.md`, read it before doing anything**, even when the user does not mention earlier sessions: it holds what the user asked for before. The user's latest words win over the notes; update the notes when they differ. On Claude Code and Codex, `iu install` loads it automatically with a SessionStart hook.
- **After a compaction, read `NOTES.md` first, then continue.** On Claude Code, `iu install` adds a hook that puts `NOTES.md` back into context right after each compaction, together with the user's messages sent since it was last saved; add anything those messages require to `NOTES.md` first. The same hook also loads `NOTES.md` at the start of every new session, so requirements carry over to later sessions. Compaction often lands right after the user gives a requirement and before you write it down, so write it down before doing anything else in that turn.
- After a compaction, re-open only the code lines you need.
- Finish one sub-task before starting the next.

## Evidence (bench/ and docs/ in the InfiniteUsage repo)

| Mechanism | Result |
|---|---|
| Compact at ~300K (Claude Code) | -43% to -50% simulated on the author's real 7-day logs |
| Free agent first for code changes | free agents fixed 47/48 single bugs and 3/3 four-bug sets, judged by unit tests. End to end with Opus: -17% / -18%, tests green, about 50 s slower |
| Long-output summaries | 97% of key facts kept, 0 fabricated; -20% on test-heavy Sonnet tasks |
| NOTES.md across sessions | requirements a new session still follows: 1/16 without, 16/16 with the rule or hook (Claude Code, Codex) |
| Compact at ~150K (Codex) | -21% simulated on real Codex logs |
| Not included (tested, no gain) | handoff files, output caps, lower effort, masking old tool output, switching the main model to Sonnet or Haiku |
