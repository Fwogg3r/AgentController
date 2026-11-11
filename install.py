# /// script
# requires-python = ">=3.11"
# ///
"""Install AgentController and its Claude Code skills for the current user.

    uv run install.py                      # tool + /api-request + /batchy (asks for the API key once)
    uv run install.py --init PATH          # also set up a project: agentcontroller.toml + secrets dir + key
    uv run install.py --no-dash-task       # skip the always-on dashboard logon task (Windows)
    uv run install.py --local              # also install aider (pinned) for agentlocal, the local-model agents
    uv run install.py --dry-run            # show the plan and what would change; changes nothing
    uv run install.py --uninstall

Steps:
  1. `uv tool install --editable <this checkout>`: agentctl, agentdash, agentask, agentwatch, agentwait,
     agentusage, agentpilot on PATH (editable, so `git pull` upgrades it).
  2. /api-request skill -> ~/.claude/skills/api-request (SKILL.md + ask_api.py).
  3. /batchy: clones s2-streamstore/claude-batch-toolkit at a pinned commit, copies its skill and MCP
     server into ~/.claude, registers the `claude-batch` MCP server in ~/.claude.json. The key goes only
     into that server's own `env` (its process), never into a global ANTHROPIC_API_KEY.
  4. Windows: a Task Scheduler logon task "AgentController dashboard" running `agentdash --all` hidden
     (--dash-task / --no-dash-task), and ~/.agentcontroller/config.toml with commented defaults.
  5. The `roadmap` CLI (WebRoadmap, pinned commit) as a uv tool, for the dashboard's Roadmap tab (asks first; -y skips
     the question; --no-roadmap skips it). With --local (or when you say yes): aider at a pinned commit.
  6. Checks for the Claude Code CLI (never installs system software; prints how), then runs `agentdoctor`.
  7. Optional --statusline: the toolkit's batch status line (it sources ~/.claude/env, which then holds
     the key as well, readable only by scripts that source it).

Every file it overwrites in ~/.claude is backed up to <file>.bak first.
"""
import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))  # for agentcontroller.registry (TASK_NAME)
BATCH_REPO = "https://github.com/s2-streamstore/claude-batch-toolkit"
# Pinned so an upstream change can't silently alter what lands in ~/.claude; bump after testing.
BATCH_COMMIT = "feb9ff9c5e060ea3abb6829411254222e7d60577"
BATCH_MODEL = "claude-opus-5-5"  # upstream's default (claude-opus-4-6) is out of date
API_SKILL = "api-request"  # not "claude-api": that name is Claude Code's built-in API reference skill
MCP_NAME = "claude-batch"
# Third-party tools installed from GitHub at pinned commits (bump after testing, like BATCH_COMMIT).
ROADMAP_SPEC = "git+https://github.com/Fwogg3r/WebRoadmap@108eb29893362f30ef6d7f9ba6b971e82bf9da69"
AIDER_SPEC = "git+https://github.com/Aider-AI/aider.git@253f0368b873ba30d8ee26e463718f0c03614ddf"
AIDER_PYTHON = "3.12"  # aider's pinned dependency set is built for 3.12
CLAUDE_HINT = ("Claude Code (https://docs.claude.com/en/docs/claude-code/setup): `npm install -g @anthropic-ai/claude-code` "
               "or the native installer on that page")

DRY = False


def say(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}")


def run(cmd: list[str], **kw) -> None:
    say("run", " ".join(cmd))
    if not DRY:
        subprocess.run(cmd, check=True, **kw)


def backup(path: Path) -> None:
    if path.exists() and not DRY:
        shutil.copy2(path, path.with_name(path.name + ".bak"))


def write(path: Path, text: str, private: bool = False) -> None:
    say("write", str(path))
    if DRY:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def remove(path: Path) -> None:
    if not path.exists():
        return
    say("remove", str(path))
    if DRY:
        return
    shutil.rmtree(path) if path.is_dir() else path.unlink()


def need(exe: str, hint: str) -> None:
    if not shutil.which(exe):
        sys.exit(f"[error] `{exe}` not found on PATH. {hint}")


def read_key(args, existing: str | None) -> str | None:
    """--key-file, then the key already registered for the batch server, then a prompt. Never printed."""
    if args.key_file:
        return Path(args.key_file).expanduser().read_text(encoding="utf-8").strip()
    if existing:
        say("key", f"reusing the key already registered for the {MCP_NAME} MCP server")
        return existing
    if args.yes or not sys.stdin.isatty():
        return None
    key = getpass.getpass("Anthropic API key (input hidden; Enter to skip): ").strip()
    return key or None


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"[error] {path} is not valid JSON ({e}); fix it before installing")


def install_tool(args) -> None:
    if args.no_tool:
        return
    try:
        run(["uv", "tool", "install", "--force", "--editable", str(REPO)])
    except subprocess.CalledProcessError:
        # On Windows a running agentdash/agentpilot locks the tool's Scripts dir, so uv can't replace it.
        sys.exit("[error] uv couldn't reinstall the tool. Close agentdash/agentpilot (they run from it) and rerun, "
                 "or rerun with --no-tool if it's already installed.")


def confirm(args, question: str, default: bool) -> bool:
    """-y answers yes; without a terminal the default stands; otherwise ask."""
    if args.yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return default
    ans = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not ans else ans in ("y", "yes")


def tool_exe(name: str) -> str | None:
    """A uv-tool command: on PATH, or in uv's tool bin dir (which may not be on PATH yet)."""
    if p := shutil.which(name):
        return p
    try:
        d = Path(subprocess.run(["uv", "tool", "dir", "--bin"], capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None
    for c in (d / name, d / (name + ".exe")):
        if c.exists():
            return str(c)
    return None


def install_roadmap(args, claude: Path) -> None:
    """The `roadmap` CLI (WebRoadmap) at a pinned commit, plus its Claude skill."""
    if args.no_roadmap:
        return
    exe = tool_exe("roadmap")
    if exe:
        say("skip", "the roadmap CLI is already installed")
    elif confirm(args, "Install the roadmap CLI (WebRoadmap, for the dashboard's Roadmap tab) with uv?", True):
        try:
            run(["uv", "tool", "install", ROADMAP_SPEC])
        except subprocess.CalledProcessError:
            say("warn", f"couldn't install the roadmap CLI; try `uv tool install {ROADMAP_SPEC}`")
            return
        exe = tool_exe("roadmap")
    else:
        say("skip", "roadmap CLI not installed (the Roadmap tab stays empty)")
        return
    dest = claude / "skills" / "roadmap"
    if (dest / "SKILL.md").exists():
        say("skip", "the roadmap skill is already installed")
    elif DRY:
        say("run", f"roadmap install-skill --dest {dest}")
    elif exe:
        run([exe, "install-skill", "--dest", str(dest)])


def install_aider(args) -> None:
    """aider for agentlocal, only on request (--local, or yes at the prompt): it is a big install."""
    if tool_exe("aider"):
        say("skip", "aider is already installed (agentlocal is tested with commit 253f036; "
                    f"to match it: uv tool install --force --python {AIDER_PYTHON} {AIDER_SPEC})")
        return
    if not args.local and not confirm(args, "Also install aider for agentlocal (local-model agents; LM Studio needed to use it)?", False):
        say("skip", "aider not installed (agentlocal needs it; rerun with --local)")
        return
    try:
        run(["uv", "tool", "install", "--python", AIDER_PYTHON, AIDER_SPEC])
    except subprocess.CalledProcessError:
        say("warn", f"couldn't install aider; try `uv tool install --python {AIDER_PYTHON} {AIDER_SPEC}`")


def check_prereqs() -> None:
    """System software is never installed here; say how when something is missing."""
    if not shutil.which("claude"):
        say("warn", "Claude Code CLI not found. The /api-request and /batchy skills and agentpilot need it "
                    f"(agentctl and agentask don't: the SDK bundles its own). Install {CLAUDE_HINT}.")
    else:
        say("ok", "Claude Code CLI found")


def run_doctor(args) -> None:
    exe = None if DRY else tool_exe("agentdoctor")
    cmd = [exe] if exe else ["uv", "run", "--project", str(REPO), "agentdoctor"]
    cmd += ["-y"] if args.yes else []
    say("run", " ".join(cmd))
    if DRY:
        return
    if subprocess.run(cmd).returncode != 0:
        say("warn", "agentdoctor reports missing required pieces (see above); fix them and rerun `agentdoctor`")


def install_api_skill(claude: Path) -> None:
    dst = claude / "skills" / API_SKILL
    for f in (REPO / "skills" / API_SKILL).iterdir():
        write(dst / f.name, f.read_text(encoding="utf-8"))


def fetch_batch_toolkit(tmp: Path) -> Path:
    src = tmp / "claude-batch-toolkit"
    # init + fetch of one commit: works for any pinned sha without cloning the whole history.
    for cmd in (["git", "init", "-q", str(src)],
                ["git", "-C", str(src), "fetch", "-q", "--depth", "1", BATCH_REPO, BATCH_COMMIT],
                ["git", "-C", str(src), "checkout", "-q", "FETCH_HEAD"]):
        say("run", " ".join(cmd))
        subprocess.run(cmd, check=True)
    return src


def install_batchy(args, claude: Path, claude_json: Path) -> None:
    if DRY:  # no clone in a dry run: say what would happen
        say("plan", f"fetch {BATCH_REPO} @ {BATCH_COMMIT[:7]} (pinned); write {claude / 'mcp' / 'claude_batch_mcp.py'}, "
                    f"{claude / 'skills' / 'batchy' / 'SKILL.md'}, {claude / 'batches'}; register the {MCP_NAME} MCP server in "
                    f"{claude_json} if an API key is given")
        return
    with tempfile.TemporaryDirectory() as t:
        src = fetch_batch_toolkit(Path(t))
        mcp = (src / "mcp" / "claude_batch_mcp.py").read_text(encoding="utf-8")
        # mcp 2.x changed the server API the script uses; pin to 1.x until upstream catches up.
        mcp = mcp.replace('#     "mcp",', '#     "mcp<2",', 1)
        write(claude / "mcp" / "claude_batch_mcp.py", mcp)
        write(claude / "skills" / "batchy" / "SKILL.md", (src / "skills" / "batchy" / "SKILL.md").read_text(encoding="utf-8"))
        statusline = (src / "statusline.sh").read_text(encoding="utf-8")

    batches = claude / "batches"
    if not (batches / "jobs.json").exists():
        write(batches / "jobs.json", '{"version": 1, "jobs": {}}\n')
    if not DRY:
        (batches / "results").mkdir(parents=True, exist_ok=True)

    cfg = load_json(claude_json)
    old = cfg.get("mcpServers", {}).get(MCP_NAME, {}).get("env", {})
    key = read_key(args, old.get("ANTHROPIC_API_KEY"))
    if not key:
        say("warn", f"no API key: skipped the {MCP_NAME} MCP server (rerun with --key-file, or interactively)")
        return
    env = {**old, "ANTHROPIC_API_KEY": key}
    env.setdefault("CLAUDE_MODEL", BATCH_MODEL)
    cfg.setdefault("mcpServers", {})[MCP_NAME] = {
        "command": "uv",
        "args": ["run", (claude / "mcp" / "claude_batch_mcp.py").as_posix(), "--mcp"],
        "env": env,
    }
    write(claude_json, json.dumps(cfg, indent=2) + "\n")

    if args.statusline:
        install_statusline(claude, statusline, key)


def install_statusline(claude: Path, script: str, key: str) -> None:
    write(claude / "statusline.sh", script)
    env_file = claude / "env"
    lines = [l for l in (env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else [])
             if not l.startswith("export ANTHROPIC_API_KEY=")]
    write(env_file, "\n".join([*lines, f'export ANTHROPIC_API_KEY="{key}"']) + "\n", private=True)
    settings_path = claude / "settings.json"
    settings = load_json(settings_path)
    cmd = f"bash {(claude / 'statusline.sh').as_posix()}"
    current = settings.get("statusLine", {}).get("command")
    if current and current != cmd:
        say("warn", f"statusLine is already set ({current}); left it alone")
        return
    settings["statusLine"] = {"type": "command", "command": cmd}
    write(settings_path, json.dumps(settings, indent=2) + "\n")


GLOBAL_CONFIG = """# AgentController global config (read by agentdash --all). Everything is optional.
# include = "Z:/shared/agentcontroller/dash.toml"   # merged first; the keys below win
[dash]
# port = 8765
# host = "0.0.0.0"                  # LAN viewing: bind all interfaces (needs remote = true)
# remote = true                     # token auth (~/.agentcontroller/dash_token.txt); read by the logon task too
# scan = ["~/Documents/GitHub"]     # also list repos here that have an agentcontroller.toml
# hide = ["C:/some/repo"]           # never list these projects
# [pmjc]
# url = "http://127.0.0.1:8770"
# token_file = "Z:/secrets/pmjc/agentdash.token"
# [lmstudio.hosts]
# local = "http://127.0.0.1:1234"
# [dash.access]       # Cloudflare Access sign-in on a second listener for a tunnel (see README "Internet access")
# enabled = true
# listen_port = 8766
# team_domain = "myteam.cloudflareaccess.com"
# aud = ["<application AUD tag>"]
# super_admin = "you@example.com"
# grants_file = "~/.agentcontroller/grants.json"
# public_origin = "https://agentdash.example.com"
# audit_log = "~/.agentcontroller/access-audit.jsonl"
"""


def init_global_config() -> None:
    cfg = Path(os.environ.get("AGENTCONTROLLER_HOME") or Path.home() / ".agentcontroller") / "config.toml"
    if cfg.exists():
        say("skip", f"{cfg} exists")
    else:
        write(cfg, GLOBAL_CONFIG)


def _tool_python() -> Path:
    """pythonw.exe of the uv tool's environment (so agentdash runs without a console window)."""
    try:
        d = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True, check=True).stdout.strip()
        exe = Path(d) / "agentcontroller" / "Scripts" / "pythonw.exe"
        if exe.exists():
            return exe
    except (OSError, subprocess.SubprocessError):
        pass
    exe = Path(sys.executable)
    return exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe


def dash_task_xml() -> str:
    from xml.sax.saxutils import escape
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>AgentController unified dashboard (agentdash --all)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId><Delay>PT20S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{escape(str(_tool_python()))}</Command><Arguments>-m agentcontroller.dash --all</Arguments><WorkingDirectory>{escape(str(REPO))}</WorkingDirectory>
  </Exec></Actions>
</Task>"""


def install_dash_task() -> None:
    if os.name != "nt":
        say("skip", "the dashboard logon task is Windows-only (ensure_dash() starts agentdash on demand elsewhere)")
        return
    from agentcontroller.registry import TASK_NAME
    xml = Path(tempfile.gettempdir()) / "agentcontroller-dash-task.xml"
    say("write", f"{xml} -> schtasks /Create /TN \"{TASK_NAME}\"")
    if DRY:
        return
    xml.write_text(dash_task_xml(), encoding="utf-16")
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml), "/F"], capture_output=True, text=True)
    xml.unlink(missing_ok=True)
    if r.returncode != 0:
        say("warn", f"couldn't create the dashboard task: {(r.stderr or r.stdout).strip()}")
    else:
        say("task", f"'{TASK_NAME}' runs agentdash --all at logon (start it now: schtasks /Run /TN \"{TASK_NAME}\")")


def remove_dash_task() -> None:
    if os.name != "nt":
        return
    from agentcontroller.registry import TASK_NAME
    say("run", f"schtasks /Delete /TN \"{TASK_NAME}\" /F")
    if not DRY:
        subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True)


def init_project(args, project: Path) -> None:
    """Minimal per-project setup from examples/agentcontroller.toml (its default secrets dir is .agentcontroller)."""
    toml = project / "agentcontroller.toml"
    if toml.exists():
        say("skip", f"{toml} exists")
    else:
        write(toml, (REPO / "examples" / "agentcontroller.toml").read_text(encoding="utf-8"))
    gitignore = project / ".gitignore"
    ignored = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    if ".agentcontroller/" not in ignored:
        write(gitignore, "\n".join([*ignored, ".agentcontroller/"]) + "\n")
    key_path = project / ".agentcontroller" / "api.key"
    if key_path.exists():
        say("skip", f"{key_path} exists")
        return
    key = read_key(args, None)
    if key:
        write(key_path, key + "\n", private=True)
    else:
        say("warn", f"no key stored: put your API key in {key_path} before using agentask/agentctl")


def uninstall(args, claude: Path, claude_json: Path) -> None:
    remove(claude / "skills" / API_SKILL)
    remove(claude / "skills" / "batchy")
    remove(claude / "mcp" / "claude_batch_mcp.py")
    cfg = load_json(claude_json)
    if cfg.get("mcpServers", {}).pop(MCP_NAME, None) is not None:
        write(claude_json, json.dumps(cfg, indent=2) + "\n")
    say("note", "kept ~/.claude/batches (job history), ~/.claude/env and any statusLine setting")
    if not args.no_tool and shutil.which("agentctl"):
        run(["uv", "tool", "uninstall", "agentcontroller"])


def main() -> None:
    global DRY
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--init", metavar="PATH", help="also set up this project directory (toml, gitignore, key)")
    ap.add_argument("--key-file", help="read the Anthropic API key from this file instead of prompting")
    ap.add_argument("--no-tool", action="store_true", help="don't (re)install the agentcontroller uv tool")
    ap.add_argument("--no-batchy", action="store_true", help="skip the batch toolkit (/batchy + MCP server)")
    ap.add_argument("--local", action="store_true", help="also install aider (pinned) for agentlocal, the local-model agents")
    ap.add_argument("--no-roadmap", action="store_true", help="skip the roadmap CLI (WebRoadmap)")
    ap.add_argument("--no-doctor", action="store_true", help="don't run agentdoctor at the end")
    ap.add_argument("--statusline", action="store_true", help="also install the batch toolkit's status line")
    ap.add_argument("--claude-dir", default="~/.claude", help="Claude Code config dir (default ~/.claude)")
    ap.add_argument("--claude-json", default="~/.claude.json", help="Claude Code user config (default ~/.claude.json)")
    ap.add_argument("--dash-task", action="store_true", help="install only the dashboard logon task and exit (Windows)")
    ap.add_argument("--no-dash-task", action="store_true", help="skip the dashboard logon task; remove it with --uninstall")
    ap.add_argument("--uninstall", action="store_true", help="remove the skills, the MCP server and the tool")
    ap.add_argument("--dry-run", action="store_true", help="print the changes without making them")
    ap.add_argument("-y", "--yes", action="store_true", help="never prompt (no key -> skip the MCP server)")
    args = ap.parse_args()
    DRY = args.dry_run
    claude, claude_json = Path(args.claude_dir).expanduser(), Path(args.claude_json).expanduser()

    need("uv", "Install it: https://docs.astral.sh/uv/getting-started/installation/")
    if args.dash_task:
        install_dash_task()
        return
    if args.uninstall:
        remove_dash_task()
        uninstall(args, claude, claude_json)
        return
    need("git", "Install git first.")

    if DRY:
        say("plan", "dry run: only the lines below are printed, nothing is executed or written")
    check_prereqs()
    install_tool(args)
    install_api_skill(claude)
    init_global_config()
    if not args.no_dash_task:
        install_dash_task()
    if not args.no_batchy:
        install_batchy(args, claude, claude_json)
    install_roadmap(args, claude)
    install_aider(args)
    if args.init:
        init_project(args, Path(args.init).expanduser().resolve())
    if not args.no_doctor:
        run_doctor(args)
    say("done", "restart Claude Code so it picks up the skills and the MCP server. Then: /api-request, /batchy, agentdash")


if __name__ == "__main__":
    main()
