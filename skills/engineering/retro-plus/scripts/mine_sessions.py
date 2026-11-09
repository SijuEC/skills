"""Mine recent coding-agent session logs for retro-plus.

Reads Copilot CLI and Claude Code session logs, keeps the sessions relevant to
the current repo, and reports environment facts, pruning candidates,
permission denials, stuck turns and wasted retries, each with a rough
estimate of tokens saved per session. MINER_SPEC.md beside this file is the
contract: detection rules, output shape, and how to write a local reader.

    python mine_sessions.py [--json] [--n 10] [--repo owner/name]

Exit codes: 0 report printed, 2 no sessions found (a diagnosis is printed).
"""
import argparse
import collections
import glob
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOME = os.path.expanduser("~")
COPILOT_DIR = os.environ.get("COPILOT_HOME") or os.path.join(HOME, ".copilot")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
LOCAL_READER = os.path.join(HOME, ".agents", "retro-plus", "reader_local.py")
MACHINE_FACTS = os.path.join(HOME, ".agents", "environment-facts.md")
REPO_FACTS = os.path.join("docs", "agents", "environment-facts.md")

FACT_MIN_SESSIONS = 3
MACHINE_MIN_REPOS = 2
REPO_WINDOW = 10
TOOL_WINDOW = 5
SKILL_WINDOW = 20
MACHINE_WINDOW = 40
LOOP_MIN_REPEATS = 3

# Built-in tools per agent. Keep in sync with MINER_SPEC.md "Built-in tools".
BUILTINS = {
    "copilot": {"powershell", "bash", "read_powershell", "write_powershell", "stop_powershell",
                "list_powershell", "read_bash", "write_bash", "stop_bash", "list_bash", "view",
                "create", "edit", "apply_patch", "str_replace", "grep", "glob", "rg", "web_fetch",
                "skill", "ask_user", "task", "read_agent", "write_agent", "list_agents", "sql",
                "session_store_sql", "store_memory", "fetch_copilot_cli_documentation",
                "task_complete", "exit_plan_mode"},
    "claude": {"Bash", "BashOutput", "KillShell", "Read", "Edit", "MultiEdit", "Write", "Grep",
               "Glob", "NotebookEdit", "WebFetch", "WebSearch", "Task", "TodoWrite", "ExitPlanMode",
               "Skill", "SlashCommand", "AskUserQuestion"},
}
PROTECTED = {"powershell", "bash", "view", "edit", "create", "grep", "glob", "ask_user", "task",
             "read_powershell", "read_bash", "apply_patch", "str_replace",
             "Bash", "Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "AskUserQuestion", "Task"}
SHELL_TOOLS = {"powershell", "bash", "shell", "Bash"}
AVG_TOOL_TOKENS = 490  # fallback per-tool definition size, from observed Copilot usage records

NOT_FOUND = re.compile(r"is not recognized as (?:the name of )?a cmdlet|command not found|"
                       r"wird nicht als Name eines Cmdlet|: not found\s*$", re.I | re.M)
NO_MODULE = re.compile(r"No module named '([\w.]+)'")
ENV_VAR = re.compile(r"(?:environment variable|env var)\s+[`'\"]?([A-Z][A-Z0-9_]{2,})|"
                     r"\b([A-Z][A-Z0-9_]{2,})\b (?:is )?not set", re.I)
CHARMAP = re.compile(r"UnicodeEncodeError: 'charmap'")
PS_SYNTAX = re.compile(r"ParserError|is not a valid statement separator")
NETWORK = re.compile(r"(?:Could not resolve host|getaddrinfo \w+|ECONNREFUSED|Connection refused|"
                     r"Name or service not known)[:\s]*([\w-]+(?:\.[\w-]+)+)?", re.I)
TEST_RUNNER = re.compile(r"\b(pytest|jest|vitest|mocha|go test|manage\.py test|rspec)\b")
NO_TESTS = re.compile(r"no tests ran|not found: |No test files found|No tests found|"
                      r"no test specified|0 tests", re.I)
ERR_LINE = re.compile(r"error|exception|not recognized|not found|cannot|fail|denied|refused|"
                      r"traceback|timeout", re.I)
DENIAL = re.compile(r"Permission denied and could not request|permission to use .* (?:has been|was) denied|"
                    r"doesn't want to proceed with this tool use", re.I)


# ---------------------------------------------------------------- readers
# A reader returns normalised sessions:
#   {"id", "agent", "repo", "cwd", "start", "model_calls", "tool_tokens",
#    "skills": set, "calls": [{"tool", "mcp", "turn", "cmd", "args",
#                               "failed", "denied", "out"}]}


def new_session(sid, agent):
    return {"id": sid, "agent": agent, "repo": None, "cwd": None, "start": None,
            "model_calls": 0, "tool_tokens": [], "skills": set(), "calls": []}


def read_copilot():
    sessions = []
    for path in glob.glob(os.path.join(COPILOT_DIR, "session-state", "*", "events.jsonl")):
        s = new_session(os.path.basename(os.path.dirname(path)), "copilot")
        starts = {}
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t, d = e.get("type"), e.get("data") or {}
                if t in ("session.start", "session.context_changed", "session.resume"):
                    ctx = d.get("context") or {}
                    s["repo"] = ctx.get("repository") or s["repo"]
                    s["cwd"] = ctx.get("gitRoot") or ctx.get("cwd") or s["cwd"]
                    s["start"] = s["start"] or e.get("timestamp")
                elif t == "session.usage_record":
                    u = d.get("usage") or {}
                    s["model_calls"] += 1
                    if u.get("availableToolCount"):
                        s["tool_tokens"].append(u.get("toolTokenCount", 0) / u["availableToolCount"])
                elif t == "skill.invoked" and d.get("name"):
                    s["skills"].add(d["name"])
                elif t == "tool.execution_start":
                    args = d.get("arguments") if isinstance(d.get("arguments"), dict) else {}
                    cmd = args.get("command")
                    starts[d.get("toolCallId")] = {
                        "tool": d.get("toolName"), "mcp": d.get("mcpServerName"),
                        "turn": d.get("interactionId") or d.get("turnId"),
                        "cmd": cmd if isinstance(cmd, str) else "", "args": args}
                    if d.get("toolName") == "skill" and args.get("skill"):
                        s["skills"].add(args["skill"])
                elif t == "tool.execution_complete" and d.get("toolCallId") in starts:
                    c = starts.pop(d["toolCallId"])
                    c["turn"] = d.get("interactionId") or c["turn"]
                    exit_code = (d.get("shellExecution") or {}).get("exitCode")
                    err = d.get("error") or {}
                    c["failed"] = (not d.get("success")) or exit_code not in (0, None)
                    c["out"] = ((d.get("result") or {}).get("content") or err.get("message") or "")[:4000]
                    c["denied"] = err.get("code") == "denied" or bool(DENIAL.search(c["out"][:300]))
                    s["calls"].append(c)
        if s["calls"] and s["start"]:
            sessions.append(s)
    return sessions


def read_claude():
    """Unverified against a real transcript; see MINER_SPEC.md "Claude Code reader"."""
    sessions = []
    for path in glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl")):
        s = new_session(os.path.splitext(os.path.basename(path))[0], "claude")
        pending, turn = {}, 0
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                s["cwd"] = s["cwd"] or e.get("cwd")
                s["start"] = s["start"] or e.get("timestamp")
                msg = e.get("message") or {}
                content = msg.get("content")
                if e.get("type") == "user" and isinstance(content, str):
                    turn += 1
                    continue
                if e.get("type") == "assistant":
                    s["model_calls"] += 1
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_use":
                        name, args = part.get("name") or "", part.get("input") or {}
                        mcp = name.split("__")[1] if name.startswith("mcp__") and name.count("__") >= 2 else None
                        pending[part.get("id")] = {
                            "tool": name, "mcp": mcp, "turn": turn, "args": args,
                            "cmd": args.get("command", "") if name == "Bash" else ""}
                        if name == "Skill":
                            sk = args.get("skill") or args.get("command") or args.get("name")
                            if sk:
                                s["skills"].add(str(sk).lstrip("/"))
                    elif part.get("type") == "tool_result" and part.get("tool_use_id") in pending:
                        c = pending.pop(part["tool_use_id"])
                        out = part.get("content")
                        if isinstance(out, list):
                            out = "\n".join(x.get("text", "") for x in out if isinstance(x, dict))
                        c["out"] = (out or "")[:4000]
                        c["denied"] = bool(DENIAL.search(c["out"][:300]))
                        c["failed"] = bool(part.get("is_error")) or c["denied"]
                        s["calls"].append(c)
        if s["calls"] and s["start"]:
            sessions.append(s)
    return sessions


def read_local():
    if not os.path.isfile(LOCAL_READER):
        return None
    spec = importlib.util.spec_from_file_location("reader_local", LOCAL_READER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.read_sessions())


def load_sessions(diag):
    local = None
    try:
        local = read_local()
    except Exception as exc:  # a broken local reader must not hide the built-in ones
        diag.append(f"local reader {LOCAL_READER} failed: {exc!r}")
    if local is not None:
        diag.append(f"local reader {LOCAL_READER}: {len(local)} sessions")
        return local
    out = []
    for name, reader, where in (("copilot", read_copilot, os.path.join(COPILOT_DIR, "session-state")),
                                ("claude", read_claude, os.path.join(CLAUDE_DIR, "projects"))):
        try:
            got = reader()
        except Exception as exc:
            diag.append(f"{name} reader failed on {where}: {exc!r}")
            continue
        diag.append(f"{name}: {where} {'exists' if os.path.isdir(where) else 'missing'}, {len(got)} sessions parsed")
        out.extend(got)
    return out


# ---------------------------------------------------------------- helpers


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def current_repo():
    url = run(["git", "remote", "get-url", "origin"])
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    root = run(["git", "rev-parse", "--show-toplevel"]) or os.getcwd()
    return (m.group(1) if m else None), os.path.normcase(os.path.abspath(root))


def repo_key(s):
    return s.get("repo") or os.path.normcase(os.path.abspath(s.get("cwd") or "?"))


def in_repo(s, slug, root):
    if slug and s.get("repo"):
        return s["repo"].lower() == slug.lower()
    cwd = os.path.normcase(os.path.abspath(s.get("cwd") or ""))
    return cwd == root or cwd.startswith(root + os.sep)


def one_line(text, n=120):
    return " | ".join(x.strip() for x in (text or "").splitlines() if x.strip())[:n]


def heads(cmd):
    out = []
    for seg in re.split(r"&&|\|\||;|\|", cmd or ""):
        m = re.match(r"\s*(?:&\s*)?['\"]?([\w.\-\\/:]+)", seg)
        if m:
            out.append(os.path.basename(m.group(1)).lower())
    return out


def same_intent(failed, retry, head):
    """A retry repairs a failed form when it reuses the failed head or one of its arguments."""
    words = lambda cmd: set(re.findall(r"[\w.\-/]{2,}", cmd.lower()))
    rest = words(failed) - {head}
    return head in words(retry) or bool(rest & words(retry))


def err_line(text):
    for line in (text or "").splitlines():
        if ERR_LINE.search(line):
            return line.strip()
    lines = (text or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def signature(text):
    line = re.sub(r"0x[0-9a-f]+|\d+", "#", err_line(text), flags=re.I)
    line = re.sub(r"[A-Za-z]:\\\S+|/\S+|https?:\S+", "<path>", line)
    return hashlib.md5(line.encode()).hexdigest()[:10], line[:120]


def tokens(*texts):
    return sum(len(t or "") for t in texts) // 4


def avg_model_calls(sessions):
    vals = [s["model_calls"] or len(s["calls"]) for s in sessions]
    return sum(vals) / len(vals) if vals else 0


# ---------------------------------------------------------------- facts


def fact_events(s):
    """Yield (key, family, text, call) for every fact-shaped failure in one session."""
    calls = [c for c in s["calls"] if c["tool"] in SHELL_TOOLS and c["cmd"]]
    ok_heads = {h for c in calls if not c["failed"] for h in heads(c["cmd"])}
    for i, c in enumerate(calls):
        if not c["failed"] or c["denied"]:
            continue
        out = c["out"]
        if PS_SYNTAX.search(out):
            yield ("FormRepair:powershell-bash-syntax", "FormRepair",
                   "PowerShell rejects bash syntax here (`&&`, heredocs, `export`); use `;` and PowerShell forms.", c)
        elif CHARMAP.search(out):
            yield ("EnvValue:PYTHONIOENCODING", "EnvValue",
                   "Python crashes printing Unicode here (cp1252); set `PYTHONIOENCODING=utf-8`.", c)
        elif NO_MODULE.search(out):
            mod = NO_MODULE.search(out).group(1).split(".")[0]
            yield (f"AbsentTool:{mod}", "AbsentTool", f"Python module `{mod}` is not installed here.", c)
        elif NOT_FOUND.search(out):
            h = (heads(c["cmd"]) or ["?"])[0]
            if h in ok_heads:
                continue
            nxt = next((x for x in calls[i + 1:i + 6]
                        if not x["failed"] and h not in heads(x["cmd"]) and same_intent(c["cmd"], x["cmd"], h)), None)
            if nxt:
                yield (f"FormRepair:{h}", "FormRepair",
                       f"`{h}` is not found here; `{one_line(nxt['cmd'], 60)}` worked instead.", c)
            else:
                yield (f"AbsentTool:{h}", "AbsentTool", f"`{h}` is not available here.", c)
        elif ENV_VAR.search(out):
            m = ENV_VAR.search(out)
            var = m.group(1) or m.group(2)
            yield (f"EnvValue:{var}", "EnvValue", f"Commands here need `{var}` set.", c)
        elif NETWORK.search(out):
            host = NETWORK.search(out).group(1) or "network"
            yield (f"Boundary:{host}", "Boundary", f"Network calls to `{host}` fail here.", c)
        elif TEST_RUNNER.search(c["cmd"]) and NO_TESTS.search(out):
            runner = TEST_RUNNER.search(c["cmd"]).group(1)
            later = [x for x in calls[i + 1:i + 6] if not x["failed"] and runner in x["cmd"]]
            if later:
                yield (f"ArgRepair:{runner.replace(' ', '-')}", "ArgRepair",
                       f"`{runner}` rejected the test selector in `{one_line(c['cmd'], 60)}`; "
                       f"`{one_line(later[0]['cmd'], 60)}` worked.", c)


def mine_facts(repo_sessions, machine_sessions):
    repo_ids = {s["id"] for s in repo_sessions}
    found = {}
    for s in {x["id"]: x for x in machine_sessions + repo_sessions}.values():
        for key, family, text, c in fact_events(s):
            f = found.setdefault(key, {"family": family, "fact": text, "sessions": set(), "repos": set(),
                                       "repo_sessions": set(), "waste": 0, "sample_cmd": one_line(c["cmd"])})
            f["sessions"].add(s["id"])
            f["repos"].add(repo_key(s))
            f["waste"] += tokens(c["cmd"], c["out"])
            if s["id"] in repo_ids:
                f["repo_sessions"].add(s["id"])
    out = []
    for key, f in found.items():
        machine = len(f["repos"]) >= MACHINE_MIN_REPOS
        if not machine and not f["repo_sessions"]:
            continue
        n = len(f["sessions"]) if machine else len(f["repo_sessions"])
        out.append({"key": key, "family": f["family"], "fact": f["fact"],
                    "scope": "machine" if machine else "repo",
                    "target": MACHINE_FACTS if machine else REPO_FACTS,
                    "sessions": n, "confirmed": n >= FACT_MIN_SESSIONS,
                    "est_tokens": f["waste"] // max(len(f["sessions"]), 1),
                    "sample_cmd": f["sample_cmd"]})
    return sorted(out, key=lambda x: (not x["confirmed"], -x["est_tokens"]))


def stale_facts(machine_sessions):
    """Facts already written whose key a recent session contradicts."""
    stale = []
    ok = [c for s in machine_sessions for c in s["calls"] if not c["failed"]]
    ok_heads = {h for c in ok if c["cmd"] for h in heads(c["cmd"])}
    ok_text = "\n".join(c["cmd"] + "\n" + c["out"][:200] for c in ok)
    for path in (REPO_FACTS, MACHINE_FACTS):
        if not os.path.isfile(path):
            continue
        for line in open(path, encoding="utf-8", errors="replace"):
            m = re.search(r"<!--\s*(\w+):([\w.\-]+)\s*-->", line)
            if not m:
                continue
            family, arg = m.groups()
            hit = None
            if family in ("AbsentTool", "FormRepair") and arg.lower() in ok_heads:
                hit = f"`{arg}` ran successfully in a recent session"
            elif family == "Boundary" and arg in ok_text:
                hit = f"a call to `{arg}` succeeded in a recent session"
            if hit:
                stale.append({"key": f"{family}:{arg}", "file": path, "line": line.strip(), "evidence": hit})
    return stale


# ---------------------------------------------------------------- pruning


def frontmatter(path):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    fm = {}
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip().strip("\"'")
    return fm


def skill_inventory(root):
    dirs = [os.path.join(HOME, ".agents", "skills"), os.path.join(HOME, ".copilot", "skills"),
            os.path.join(CLAUDE_DIR, "skills")]
    dirs += [os.path.join(root, p, "skills") for p in (".github", ".claude", ".agents")]
    skills = {}
    for d in dirs:
        for p in glob.glob(os.path.join(d, "*", "SKILL.md")):
            fm = frontmatter(p)
            if fm.get("disable-model-invocation", "").lower() == "true":
                continue  # user-invoked skills put nothing in context
            skills[fm.get("name") or os.path.basename(os.path.dirname(p))] = tokens(fm.get("description", ""))
    return skills


def mcp_inventory(root, sessions):
    servers = set()
    for p in (os.path.join(COPILOT_DIR, "mcp-config.json"), os.path.join(HOME, ".claude.json"),
              os.path.join(root, ".mcp.json"), os.path.join(root, ".vscode", "mcp.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        servers |= set(data.get("mcpServers") or data.get("servers") or {})
    servers |= {c["mcp"] for s in sessions for c in s["calls"] if c["mcp"]}
    return servers


def platform_absent():
    """Copilot loads the powershell tool family on Windows and the bash family elsewhere."""
    fam = "bash" if os.name == "nt" else "powershell"
    return {fam, f"read_{fam}", f"write_{fam}", f"stop_{fam}", f"list_{fam}"}


def mine_pruning(repo_sessions, machine_sessions, root):
    out = []
    skill_win = machine_sessions[:SKILL_WINDOW]
    used_skills = {k for s in skill_win for k in s["skills"]}
    per_session = avg_model_calls(skill_win)
    for name, desc in sorted(skill_inventory(root).items()):
        if name not in used_skills:
            out.append({"kind": "skill", "name": name, "window": f"{len(skill_win)} sessions, all repos",
                        "est_tokens": int(desc * per_session)})
    tool_win = repo_sessions[:TOOL_WINDOW]
    per_tool = [t for s in tool_win for t in s["tool_tokens"]]
    tool_tokens = sum(per_tool) / len(per_tool) if per_tool else AVG_TOOL_TOKENS
    per_session = avg_model_calls(tool_win)
    used_tools = {c["tool"] for s in tool_win for c in s["calls"]}
    used_mcp = {c["mcp"] for s in tool_win for c in s["calls"] if c["mcp"]}
    for server in sorted(mcp_inventory(root, machine_sessions) - used_mcp):
        out.append({"kind": "mcp_server", "name": server, "window": f"{len(tool_win)} sessions, this repo",
                    "est_tokens": int(tool_tokens * per_session)})
    for agent in sorted({s["agent"] for s in tool_win}):
        for tool in sorted(BUILTINS.get(agent, set()) - PROTECTED - used_tools - platform_absent()):
            out.append({"kind": f"builtin_tool ({agent})", "name": tool,
                        "window": f"{len(tool_win)} sessions, this repo",
                        "est_tokens": int(tool_tokens * per_session)})
    return sorted(out, key=lambda x: -x["est_tokens"])


def mine_denials(repo_sessions):
    found = collections.defaultdict(lambda: {"count": 0, "sessions": set(), "sample": ""})
    for s in repo_sessions:
        for c in s["calls"]:
            if c["denied"]:
                first = next((x.strip() for x in (c["cmd"] or "").splitlines() if x.strip()), "")
                m = re.match(r"(?:&\s*)?['\"]?([\w.\-\\/:]+)", first)
                key = f"{c['tool']}: {os.path.basename(m.group(1)).lower()}" if m else c["tool"]
                d = found[key]
                d["count"] += 1
                d["sessions"].add(s["id"])
                d["sample"] = d["sample"] or one_line(c["cmd"]) or c["tool"]
    return sorted(({"target": k, "count": v["count"], "sessions": len(v["sessions"]), "sample": v["sample"],
                    "est_tokens": v["count"] * 200 // max(len(v["sessions"]), 1)}
                   for k, v in found.items()), key=lambda x: -x["count"])


# ---------------------------------------------------------------- loops


def mine_loops(repo_sessions):
    stuck, wasted = [], []
    for s in repo_sessions:
        groups = collections.defaultdict(list)
        for c in s["calls"]:
            if c["failed"] and not c["denied"]:
                groups[(c["turn"], signature(c["out"])[0])].append(c)
        merged = collections.defaultdict(lambda: {"turns": 0, "calls": []})
        for (_, sig), calls in groups.items():
            if len(calls) >= LOOP_MIN_REPEATS:
                merged[sig]["turns"] += 1
                merged[sig]["calls"].extend(calls)
        for g in merged.values():
            calls = g["calls"]
            distinct = len({json.dumps(c["args"], sort_keys=True, default=str) for c in calls})
            item = {"session": s["id"], "agent": s["agent"], "turns": g["turns"], "repeats": len(calls),
                    "distinct_args": distinct, "error": signature(calls[-1]["out"])[1],
                    "last_call": one_line(calls[-1]["cmd"]) or calls[-1]["tool"],
                    "est_tokens": sum(tokens(c["cmd"], c["out"]) for c in calls[1:])}
            (stuck if distinct <= len(calls) / 2 else wasted).append(item)
    return (sorted(stuck, key=lambda x: -x["est_tokens"]), sorted(wasted, key=lambda x: -x["est_tokens"]))


# ---------------------------------------------------------------- report


def print_markdown(r):
    print(f"# Session evidence: {r['repo']} ({len(r['sessions'])} sessions; {r['machine_sessions']} machine-wide)\n")
    print(f"## Environment facts (propose when sessions >= {FACT_MIN_SESSIONS})\n")
    for f in r["facts"]:
        mark = "PROPOSE" if f["confirmed"] else f"{f['sessions']}/{FACT_MIN_SESSIONS}"
        print(f"- [{mark}] ~{f['est_tokens']} tok, {f['scope']}: {f['fact']} <!-- {f['key']} -->")
        print(f"    e.g. `{f['sample_cmd']}`")
    for f in r["stale_facts"]:
        print(f"- [REMOVE] {f['file']}: {f['line']} ({f['evidence']})")
    print("\n## Pruning candidates (inform only; tokens re-sent per session, mostly cache reads)\n")
    for p in r["pruning"]:
        print(f"- ~{p['est_tokens']} tok/session, {p['kind']}: {p['name']} (unused in {p['window']})")
    print("\n## Permission denials\n")
    for d in r["denials"]:
        print(f"- {d['target']}: {d['count']} denied in {d['sessions']} session(s), e.g. `{d['sample']}`")
    for title, items in (("Stuck turns", r["stuck_turns"]), ("Wasted retries", r["wasted_retries"])):
        print(f"\n## {title}\n")
        for x in items:
            print(f"- ~{x['est_tokens']} tok, {x['session'][:8]} x{x['repeats']} over {x['turns']} turn(s), "
                  f"{x['distinct_args']} distinct args: {x['error']}")
            print(f"    last: `{x['last_call']}`")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="print the JSON report")
    ap.add_argument("--n", type=int, default=REPO_WINDOW, help="sessions of this repo to read")
    ap.add_argument("--repo", help="owner/name, defaults to the origin remote")
    a = ap.parse_args()

    slug, root = current_repo()
    slug = a.repo or slug
    diag = []
    sessions = sorted(load_sessions(diag), key=lambda s: s["start"] or "", reverse=True)
    repo_sessions = [s for s in sessions if in_repo(s, slug, root)][:a.n]
    machine_sessions = sessions[:MACHINE_WINDOW]
    if not repo_sessions:
        diag.append(f"0 sessions matched repo {slug or '?'} (root {root}) out of {len(sessions)} parsed")
        report = {"error": "no sessions found", "diagnosis": diag}
        print(json.dumps(report, indent=1) if a.json else "No sessions found.\n" + "\n".join(f"- {d}" for d in diag))
        sys.exit(2)

    stuck, wasted = mine_loops(repo_sessions)
    report = {"repo": slug or root, "sessions": [s["id"] for s in repo_sessions],
              "agents": sorted({s["agent"] for s in repo_sessions}),
              "machine_sessions": len(machine_sessions),
              "facts": mine_facts(repo_sessions, machine_sessions),
              "stale_facts": stale_facts(machine_sessions),
              "pruning": mine_pruning(repo_sessions, machine_sessions, root),
              "denials": mine_denials(repo_sessions),
              "stuck_turns": stuck, "wasted_retries": wasted, "diagnosis": diag}
    if a.json:
        print(json.dumps(report, indent=1, default=sorted))
    else:
        print_markdown(report)


if __name__ == "__main__":
    main()
