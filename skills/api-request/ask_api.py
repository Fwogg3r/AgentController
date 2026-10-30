# /// script
# requires-python = ">=3.11"
# ///
"""Shim over `agentask` (AgentController): one-shot Messages API call, recorded as a run in agentdash.

    uv run ask_api.py "prompt" [-f file ...] [-m opus|fable|sonnet|haiku] [-e effort] [-o out.md]
    uv run ask_api.py --spend

The project (key file, budget cap, system prompt, default model/effort) comes from the nearest
agentcontroller.toml above the cwd, or --project PATH. Uses the installed `agentask`; without it,
runs AgentController from GitHub through uvx (slower first call, nothing installed).

This spends API credit, separately from the Claude plan. Never export the key as ANTHROPIC_API_KEY
(Claude Code would then bill the API instead of the plan).
"""
import shutil
import subprocess
import sys

exe = shutil.which("agentask")
cmd = [exe] if exe else ["uvx", "--from", "git+https://github.com/Fwogg3r/AgentController", "agentask"]
sys.exit(subprocess.call([*cmd, *sys.argv[1:]]))
