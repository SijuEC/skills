## What it does

`retro-plus` is [retro](https://aihero.dev/skills-retro) with a memory for patterns. It runs a small miner over the recent [session](https://www.aihero.dev/ai-coding-dictionary/session) logs on your machine, then reviews the current session with that evidence beside it, and proposes changes to the agent's **[environment](https://www.aihero.dev/ai-coding-dictionary/environment)** ranked by the tokens each would save per session.

It looks across sessions rather than at one. A mistake the agent made once is noise; the same failed command in five sessions is a fact about your machine that every future session will pay for again. The miner reads saved logs after the fact, so it needs no hooks and changes nothing while you work, and nothing changes afterwards until you approve it.

## When to reach for it

You invoke this by typing `/retro-plus`, and the agent won't reach for it on its own.

| Situation | Reach for |
| --- | --- |
| One session went badly and you want to learn from that session | [retro](https://aihero.dev/skills-retro) |
| The agent keeps tripping on the same things across sessions, or sessions feel expensive | `retro-plus` |
| You want a verdict on the code itself | [code-review](https://aihero.dev/skills-code-review) |

## Prerequisites

Python 3 on `PATH`, and session logs on the machine: Copilot CLI keeps them under `~/.copilot/session-state/`, Claude Code under `~/.claude/projects/`. It writes environment facts to `docs/agents/environment-facts.md` in the repo, or to `~/.agents/environment-facts.md` when a fact holds across repos, but only for the items you approve.

## Evidence, not anecdotes

The miner turns logs into four kinds of **evidence**:

| Evidence | What it is | What `retro-plus` does with it |
| --- | --- | --- |
| **Environment fact** | A failure that recurs in three or more sessions: a command spelling that works here, a missing tool, an env var that must be set, a host that is unreachable | Proposes a one-line fact in the facts file, plus the pointer that makes agents read it before running shell commands |
| **Pruning candidate** | A model-invoked skill, MCP server or built-in tool whose definition rides along on every model call but went unused | Lists it with its token cost and how to switch it off; you decide |
| **Stuck turn** | One prompt-turn where the same failure repeated with mostly the same arguments | Traces it to the cause: a missing fact, check, pointer or access |
| **Permission denial** | A call the agent made that was refused | Proposes the allow-list entry |

Each fact line carries a key, so when a later session contradicts it (the tool is installed now) the miner flags it and `retro-plus` proposes deleting it. Facts that stop being true do not pile up.

## Common questions

**Why didn't it propose a fact for a mistake I saw?**

A fact needs three sessions of evidence. A mistake seen once or twice shows up in the miner's report with its count, but it is not proposed until it recurs. That threshold is what keeps the facts file short and true.

**Will it switch off skills or tools I rarely use?**

No. Pruning candidates are inform-only. A skill you use once a month can still be the right tool that month, so `retro-plus` shows what each one costs and the command to switch it off, and leaves the call to you. User-invoked skills cost nothing in context, so they never appear.

**What happens on a machine where the miner can't find the logs?**

It prints what it looked for and what it found instead of a stack trace. The agent then reads the miner's spec and writes a small local reader for that machine, in `~/.agents/retro-plus/` outside the skill folder, so updating the skill never overwrites it. The detection rules stay the same, so results are comparable everywhere.

**Do my logs leave the machine?**

No. The miner is a local Python script that reads files and prints a report into the session.

## It's working if

- The same failed command stops appearing in new sessions after its fact lands.
- Your facts file stays short, because contradicted facts get removed.
- Each proposal names the sessions or report item it came from, and the top of the list is the change that saves the most.

## Where it fits

`retro-plus` sits where [retro](https://aihero.dev/skills-retro) sits, at the end of the main chain, and replaces it when the lessons span sessions rather than one. Its proposals feed the coding standards [code-review](https://aihero.dev/skills-code-review) reads, and its writing follows [writing-for-agents](https://aihero.dev/skills-writing-for-agents). [ask-matt](https://aihero.dev/skills-ask-matt) routes across the whole set.
