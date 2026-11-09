# Miner spec

`mine_sessions.py` is one implementation of this spec. When it cannot read a machine's logs, you write a **local reader** and keep everything else here unchanged, so reports stay comparable across machines.

## Local reader

Write `~/.agents/retro-plus/reader_local.py` defining `read_sessions()`, which returns a list of **normalised sessions** (below). The miner loads it in place of its built-in readers whenever the file exists, and a skill update never touches it, because it lives outside the skill folder. Change only how logs are found and parsed; the detection rules and output contract stay as written here. Done when `python mine_sessions.py` exits 0 and its diagnosis names the local reader.

A normalised session:

```json
{
  "id": "session id",
  "agent": "copilot | claude | <other>",
  "repo": "owner/name or null",
  "cwd": "absolute working directory or git root",
  "start": "ISO timestamp, used for ordering",
  "model_calls": 0,
  "tool_tokens": [490.0],
  "skills": ["names of skills invoked (a set in Python)"],
  "calls": [
    {"tool": "tool name", "mcp": "MCP server name or null", "turn": "id of the user prompt this call served",
     "cmd": "shell command text, empty for non-shell tools", "args": {},
     "failed": true, "denied": false, "out": "result or error text, first 4000 chars"}
  ]
}
```

- `failed` is true when the call reported failure **or** a shell call exited non-zero. Copilot reports non-zero exits with `success: true`, so read the exit code separately.
- `denied` is true when the agent was refused permission. A denied call is excluded from facts and loops and reported under denials.
- `tool_tokens` holds per-tool definition size samples (`toolTokenCount / availableToolCount`) where the log records them; leave it empty otherwise.
- `turn` groups calls by the user prompt that started them. Loop detection depends on it.

## Built-in readers

### Copilot CLI

`~/.copilot/session-state/<id>/events.jsonl` (override the root with `COPILOT_HOME`). One JSON event per line.

| Field | Source |
| --- | --- |
| repo, cwd, start | `session.start` / `session.context_changed` / `session.resume`: `data.context.repository`, `gitRoot` or `cwd`, event `timestamp` |
| calls | `tool.execution_start` (`toolCallId`, `toolName`, `arguments`, `mcpServerName`, `interactionId`) joined to `tool.execution_complete` (`success`, `error.code`, `shellExecution.exitCode`, `result.content`) |
| denied | `error.code == "denied"` |
| skills | `skill.invoked.data.name`, or a `skill` tool call's `arguments.skill` |
| model_calls, tool_tokens | one `session.usage_record` per model call; `usage.toolTokenCount`, `usage.availableToolCount` |

The format is observed (CLI 1.0.105), not documented, and may change between versions.

### Claude Code

`~/.claude/projects/<project>/<session>.jsonl` (override with `CLAUDE_CONFIG_DIR`). This reader is **unverified** against a real transcript; Anthropic documents the format as internal. Expected shape:

| Field | Source |
| --- | --- |
| cwd, start | top-level `cwd`, `timestamp` on each line |
| turn | a `type: "user"` line whose `message.content` is a string starts a new turn |
| calls | `tool_use` parts (`id`, `name`, `input`) in assistant messages joined to `tool_result` parts (`tool_use_id`, `content`, `is_error`) |
| mcp | tool names shaped `mcp__<server>__<tool>` |
| skills | a `Skill` tool call's `input.skill` |
| denied | result text such as "permission ... has been denied" |

## Detection rules

Evidence windows: the current repo's sessions are the last 10 matching its `origin` slug or git root. Machine-wide is the last 40 sessions across all repos.

### Environment facts

| Family | Evidence in a failed shell call | Key |
| --- | --- | --- |
| FormRepair | Command not found, then within 5 calls a different command reusing its head or an argument succeeds; or a PowerShell `ParserError` from bash syntax | `FormRepair:<head>` |
| AbsentTool | Command or Python module not found, and that head never succeeds in the session | `AbsentTool:<name>` |
| EnvValue | Output names an unset environment variable; or Python's `charmap` UnicodeEncodeError (`PYTHONIOENCODING`) | `EnvValue:<VAR>` |
| ArgRepair | A test runner finds no tests for a selector, then a reworded call to the same runner succeeds | `ArgRepair:<runner>` |
| Boundary | DNS or connection refused for a host | `Boundary:<host>` |

- A fact is **confirmed** at 3 distinct sessions. Scope is **machine** when the key appears in 2 or more repos, else **repo**.
- Targets: repo facts go to `docs/agents/environment-facts.md`, machine facts to `~/.agents/environment-facts.md`. Each fact line ends with its key as an HTML comment, for example `- \`jq\` is not available here. <!-- AbsentTool:jq -->`.
- A written fact is **stale** when a recent session contradicts its key: the tool or form ran successfully, or a call to the host succeeded.

### Pruning candidates

| Unit | Inventory | Unused when | Estimated tokens per session |
| --- | --- | --- | --- |
| Skill | model-invoked `SKILL.md` files in `~/.agents/skills`, `~/.copilot/skills`, `~/.claude/skills`, and the repo's `.github/`, `.claude/`, `.agents/` skill folders | not invoked in the last 20 sessions, all repos | description tokens x model calls per session |
| MCP server | `~/.copilot/mcp-config.json`, `~/.claude.json`, repo `.mcp.json`, `.vscode/mcp.json`, servers seen in logs | not called in the last 5 sessions of this repo | per-tool definition tokens x model calls per session |
| Built-in tool | the agent's list under "Built-in tools" | not called in the last 5 sessions of this repo | same as MCP |

User-invoked skills (`disable-model-invocation: true`) put nothing in context and are never candidates. Token figures count definitions re-sent with every model call; most are cache reads, so they overstate cost but rank candidates fairly.

### Built-in tools

- Copilot CLI: `powershell` or `bash` families (with `read_`, `write_`, `stop_`, `list_` variants), `view`, `create`, `edit`, `apply_patch`, `str_replace`, `grep`, `glob`, `rg`, `web_fetch`, `skill`, `ask_user`, `task`, `read_agent`, `write_agent`, `list_agents`, `sql`, `session_store_sql`, `store_memory`, `fetch_copilot_cli_documentation`, `task_complete`, `exit_plan_mode`.
- Claude Code: `Bash`, `BashOutput`, `KillShell`, `Read`, `Edit`, `MultiEdit`, `Write`, `Grep`, `Glob`, `NotebookEdit`, `WebFetch`, `WebSearch`, `Task`, `TodoWrite`, `ExitPlanMode`, `Skill`, `SlashCommand`, `AskUserQuestion`.
- Protected, never candidates: the shell tool and its reader, `view`/`Read`, `edit`/`Edit`/`MultiEdit`/`apply_patch`/`str_replace`, `create`/`Write`, `grep`/`Grep`, `glob`/`Glob`, `ask_user`/`AskUserQuestion`, `task`/`Task`.

### Stuck turns and wasted retries

- A failure **signature** is the first error-looking line of the output, with digits, hex, paths and URLs masked, hashed.
- A **stuck turn**: one signature recurs 3 or more times in one turn, and distinct argument sets are at most half the repeats.
- A **wasted retry**: the same, but with more distinct arguments than half the repeats (a search pattern, such as many URLs each returning 404).
- Estimated tokens: command plus output tokens of every repeat after the first.

## Output contract

`--json` prints one object. Keys: `repo`, `sessions`, `agents`, `machine_sessions`, `facts`, `stale_facts`, `pruning`, `denials`, `stuck_turns`, `wasted_retries`, `diagnosis`. Every item in `facts`, `pruning`, `denials`, `stuck_turns` and `wasted_retries` carries `est_tokens`. With no matching sessions the object is `{"error", "diagnosis"}` and the exit code is 2.
