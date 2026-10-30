# /// script
# requires-python = ">=3.11"
# ///
"""Install AgentController and its Claude Code skills for the current user.

    uv run install.py                      # tool + /api-request + /batchy (asks for the API key once)
    uv run install.py --init PATH          # also set up a project: agentcontroller.toml + secrets dir + key
    uv run install.py --dry-run            # show what would change
    uv run install.py --uninstall

Steps:
  1. `uv tool install --editable <this checkout>`: agentctl, agentdash, agentask, agentwatch, agentwait,
     agentusage, agentpilot on PATH (editable, so `git pull` upgrades it).
  2. /api-request skill -> ~/.claude/skills/api-request (SKILL.md + ask_api.py).
  3. /batchy: clones s2-streamstore/claude-batch-toolkit at a pinned commit, copies its skill and MCP
     server into ~/.claude, registers the `claude-batch` MCP server in ~/.claude.json. The key goes only
     into that server's own `env` (its process), never into a global ANTHROPIC_API_KEY.
  4. Optional --statusline: the toolkit's batch status line (it sources ~/.claude/env, which then holds
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
BATCH_REPO = "https://github.com/s2-streamstore/claude-batch-toolkit"
# Pinned so an upstream change can't silently alter what lands in ~/.claude; bump after testing.
BATCH_COMMIT = "feb9ff9c5e060ea3abb6829411254222e7d60577"
BATCH_MODEL = "claude-opus-5-5"  # upstream's default (claude-opus-4-6) is out of date
API_SKILL = "api-request"  # not "claude-api": that name is Claude Code's built-in API reference skill
MCP_NAME = "claude-batch"

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
    ap.add_argument("--statusline", action="store_true", help="also install the batch toolkit's status line")
    ap.add_argument("--claude-dir", default="~/.claude", help="Claude Code config dir (default ~/.claude)")
    ap.add_argument("--claude-json", default="~/.claude.json", help="Claude Code user config (default ~/.claude.json)")
    ap.add_argument("--uninstall", action="store_true", help="remove the skills, the MCP server and the tool")
    ap.add_argument("--dry-run", action="store_true", help="print the changes without making them")
    ap.add_argument("-y", "--yes", action="store_true", help="never prompt (no key -> skip the MCP server)")
    args = ap.parse_args()
    DRY = args.dry_run
    claude, claude_json = Path(args.claude_dir).expanduser(), Path(args.claude_json).expanduser()

    need("uv", "Install it: https://docs.astral.sh/uv/getting-started/installation/")
    if args.uninstall:
        uninstall(args, claude, claude_json)
        return
    need("git", "Install git first.")

    install_tool(args)
    install_api_skill(claude)
    if not args.no_batchy:
        install_batchy(args, claude, claude_json)
    if args.init:
        init_project(args, Path(args.init).expanduser().resolve())
    say("done", "restart Claude Code so it picks up the skills and the MCP server. Then: /api-request, /batchy, agentdash")


if __name__ == "__main__":
    main()
