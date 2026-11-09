---
name: retro-plus
description: "Conduct a retrospective on a coding session, backed by evidence mined from recent session logs."
disable-model-invocation: true
---

The user has asked for a **retrospective**. You are suggesting improvements to the coding agent's **environment** to improve future runs. Your **evidence** is the session itself plus what the miner finds across recent sessions: a mistake that recurs across sessions outranks one seen once.

## Steps

1. Call the Skill tool with `writing-for-agents` for the writing style guide.

2. Run the miner from the repo root: `python <this skill's base directory>/scripts/mine_sessions.py --json`. It reads the recent Copilot CLI and Claude Code session logs on this machine and reports environment facts, stale facts, pruning candidates, permission denials, stuck turns and wasted retries, each with `est_tokens`. Done when you hold its report. If it exits 2 or reports zero sessions, read [MINER_SPEC.md](scripts/MINER_SPEC.md), write the local reader it describes for this machine, and rerun.

3. Read the primary sources for the session the user specifies. This may mean searching through session logs on this machine. If the user doesn't specify a session, default to the current one.

4. Look for candidates for improvement in these categories. Every candidate traces to a specific moment in the session or an item in the miner's report.

- **Environment facts**: does the miner report `confirmed` facts or `stale_facts`? Propose each confirmed fact as one line, key comment included, in its `target` file: `docs/agents/environment-facts.md` for `scope: repo`, `~/.agents/environment-facts.md` for `scope: machine`. A new facts file also needs its **navigation pointer**: for a repo file, a line in `AGENTS.md`/`CLAUDE.md` reading `Environment facts: read docs/agents/environment-facts.md before running shell commands.`; for the machine file, the same line pointing at `~/.agents/environment-facts.md` in `~/.copilot/copilot-instructions.md` and `~/.claude/CLAUDE.md`. Propose deleting each stale fact's line. _Use when_ the miner reports a confirmed or stale fact.
- **Navigation**: how easy was it for the agent to find the right files? Are there hidden dependencies between files? Would a **navigation pointer** make it easier? _Use when_ the session took a long time to find a piece of information.
- **Automated checks**: are there automated checks that could catch errors the agent made? Linting, typing, tests, filesystem linters? Read the repo's own check command first (its `package.json`/build-tool `lint`/`check` scripts, its CI workflow), so a check that already exists but sits unwired or silently broken is the finding, not a reinvention. A repo with no **guardrail** (no pre-commit hook and no CI job running its lint/typecheck/test command) is itself a finding: an un-linted repo is a standing missed opportunity, not a neutral default. _Use when_ the agent made a mistake an automated check could have caught, or the repo has no guardrail at all.
- **Coding standards**: should the **reviewer agent** be given a new rule to enforce? Should an existing rule be removed or clarified? Classify the violation first: a **mechanical** one (a fixed syntactic pattern, a banned API, an import shape, a file-location rule) gets a deterministic check, full stop: a custom rule in the repo's own linter, a new pre-commit hook, or a new CI job, whichever the repo's language and existing guardrail make cheapest. Default to building the check over writing the rule. Reserve `CODING_STANDARDS.md` for genuine **judgement calls** (cross-file consistency, "matches the surrounding style," anything no guardrail could ever substitute for). _Use when_ the reviewer agent failed to catch a mistake.
- **Global AGENTS.md**: are there any steering instructions that should be moved to coding standards (or automated checks) instead? _Use when_ the AGENTS.md file is particularly large - in the repo OR the user's global scope.
- **Tool economy**: did the agent make expensive tool calls that could be streamlined? Is there any custom tooling (CLI's, MCP's) that is particularly token-inefficient? Report the miner's `pruning` candidates and `denials` here. These are **inform-only**: list each with its `est_tokens` and how to switch it off (see [Switching off](#switching-off)), and leave the decision with the user, because a rarely used skill or tool may still be needed for an edge case. For a denial, propose the allow-list entry that would have let the call through. _Use when_ the agent made an expensive tool call, or the miner reports pruning candidates or denials.
- **No-ops**: look for instructions in steering files that don't modify the agent's behavior. _Use when_ the steering files are large and unwieldy.
- **Information access**: look for opportunities to increase the agent's access to information. Teeing dev server logs, readonly access to third-party services. _Use when_ a crucial piece of information was not available to the agent.

**Stuck turns** are symptoms, not a category. For each item in the miner's `stuck_turns`, find that turn in the session log, ask what the agent was missing, and file the candidate under the category that holds the cause: a missing fact, check, pointer, or access. Treat `wasted_retries` the same way, remembering that many are a search pattern rather than a loop.

5. Present the candidates as a numbered list, ranked by estimated tokens saved per session: the miner's `est_tokens` where it has one, otherwise your own estimate, marked as such. Each item names its category, its evidence (session and moment, or miner item), the change, and whether it is **apply** or **inform-only**. Done when every candidate has a rank and evidence.

6. Apply the items the user approves. Inform-only items stay with the user.

## Reference

### Implementation vs Review

Remember that all work goes through two stages: implementation and review. The implementation agent has the most **context pressure**. They are responsible for exploration, writing code, and debugging failures.

The review agent has the least context pressure - it receives a diff, so no exploration needed. It often does not need to write code or debug.

This means that the review agent should be responsible for imposing coding standards, not the implementation agent.

### Files

You have access to several files in the repo:

- `CLAUDE.md`/`AGENTS.md`: these files are pushed to the context window of any agent working in this repo. They should be used incredibly sparingly, usually only for **navigation pointers** to other files.
- `CODING_STANDARDS.md`: this file is read during review, not implementation. Add **navigation pointers** to docs folders if the standards file gets more than 1,000 lines long.
- Environment facts files: one line per fact, each ending in its miner key, read through a navigation pointer before running shell commands.
- Docs: use docs as references files, pointed to by other files. Look for existing docs before writing new ones.
- Skills: use skills for docs (since their description goes into the agent's context window), or for user-invoked commands. Follow the advice in the `writing-for-agents` skill.

### Switching off

| Candidate | Copilot CLI | Claude Code |
| --- | --- | --- |
| Skill | `copilot skill disable <name>` (back with `enable`) | set `disable-model-invocation: true` in its `SKILL.md`, which keeps `/<name>` working |
| MCP server | add it to `disabledMcpServers` in `.github/copilot/settings.local.json` | add `mcp__<server>` to `permissions.deny` in `.claude/settings.local.json` |
| Built-in tool | launch with `--excluded-tools <name>` | add the tool name to `permissions.deny` in `.claude/settings.local.json` |
