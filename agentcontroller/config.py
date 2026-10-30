"""Per-project configuration: agentcontroller.toml found by walking up from the cwd (or --project)."""

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODELS = {
    "haiku": {"id": "claude-haiku-5-5", "note": "cheapest; smoke tests and small edits"},
    "sonnet": {"id": "claude-sonnet-5-5", "note": "balanced; most routine work"},
    "opus": {"id": "claude-opus-5-5", "note": "strong; harder design/debug work"},
    "fable": {"id": "claude-fable-5-1", "note": "most capable and most expensive"},
}
DEFAULT_RULES = """You are a headless engineering agent billed to an API budget.
Work only inside this worktree; commit your work here in logical commits. Never push.
Be economical with tokens: read files in targeted ranges, avoid re-reading, batch edits.
Finish with a concise report: what you built, test results, deviations, risks, next steps."""
# Always appended (after the project's rules). Cost is dominated by re-reading the whole
# context every turn (cache reads ~ turns x context size), so the levers are fewer turns,
# a smaller context, and handing off to a fresh agent instead of growing one session.
ECONOMY_RULES = """Token economy (you are billed per token; every token you add is re-read on every later turn):
- Keep tool output small: quiet builds/tests (e.g. `cargo build -q`, `cargo test -q 2>&1 | tail -20`), grep/tail long output, never dump whole logs.
- Read files in targeted line ranges; grep first. Do not re-read files you already have.
- Issue independent tool calls in the same turn. Batch edits.
- View screenshots only when needed, at the smallest size that answers the question.
- Turn budget: about {soft} turns. If you get there (or your context is large) before finishing:
  commit what works, write HANDOFF-{name}.md at the worktree root (done / in progress / next steps / gotchas),
  commit it, and end with a short report. A fresh agent will continue from that file."""
CONFIG_NAME = "agentcontroller.toml"


@dataclass
class Config:
    root: Path
    secrets: Path
    key_file: Path
    admin_key_file: Path
    cap: float | None
    worktrees: Path
    runs: Path
    briefs: Path
    usage_log: Path
    deny: list[str] = field(default_factory=list)
    path_extra: list[str] = field(default_factory=list)
    rules: str = DEFAULT_RULES
    default_model: str = "sonnet"
    default_base: str = "main"
    default_max_turns: int = 80
    default_effort: str | None = "medium"
    # "summarized" asks the CLI to return thinking text (newer models default to signature only).
    # Display only: it doesn't enable thinking or change how many thinking tokens are billed.
    thinking_display: str | None = None
    port: int = 8765
    models: dict = field(default_factory=lambda: dict(DEFAULT_MODELS))
    runner_cmd: list[str] | None = None  # override how the dashboard launches the runner (tests)
    # agentpilot ([pilot] section)
    pilot_prompt_file: Path | None = None
    pilot_handoff: str = "docs/HANDOFF.md"
    pilot_model: str = "opus"
    pilot_permission_mode: str = "auto"
    # agentask ([oneshot] section)
    oneshot_system: str = ""
    oneshot_model: str = "opus"
    oneshot_effort: str = "high"
    # Claude Code transcripts shown as plan sessions ([claude] section); None = derive from the project root
    claude_projects_dir: Path | None = None


def find_project(start: Path | None = None) -> Path | None:
    p = (start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / CONFIG_NAME).is_file():
            return d / CONFIG_NAME
    return None


def _main_checkout(root: Path) -> Path | None:
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root,
                                capture_output=True, text=True, check=True).stdout.strip()
        return Path(common).parent
    except (subprocess.CalledProcessError, OSError):
        return None


def load(project: str | Path | None = None) -> Config:
    """`project` may be a toml file or a directory containing one; without it, walk up from the cwd."""
    if project:
        p = Path(project).resolve()
        toml = p / CONFIG_NAME if p.is_dir() else p
    else:
        toml = find_project()
    raw = tomllib.loads(toml.read_text(encoding="utf-8")) if toml and toml.is_file() else {}
    root = toml.parent if toml and toml.is_file() else Path.cwd().resolve()
    paths, budget, agent, dash, pilot, oneshot, claude = (raw.get(k, {}) for k in ("paths", "budget", "agent", "dash", "pilot", "oneshot", "claude"))

    def rel(v: str, base: Path = root) -> Path:
        q = Path(os.path.expandvars(v)).expanduser()
        return q if q.is_absolute() else base / q

    secrets = rel(os.environ.get("AGENTCONTROLLER_SECRETS") or paths.get("secrets", ".agentcontroller"))
    if not secrets.exists() and not os.environ.get("AGENTCONTROLLER_SECRETS"):
        # Secrets are gitignored, so a git worktree of the project doesn't contain them.
        main = _main_checkout(root)
        alt = rel(paths.get("secrets", ".agentcontroller"), main) if main else None
        if alt and alt.exists():
            secrets = alt
    cap = budget.get("cap")
    if cap is None:
        try:
            cap = float((secrets / budget.get("cap_file", "budget.txt")).read_text(encoding="utf-8").strip().lstrip("$"))
        except (OSError, ValueError):
            cap = None
    models = {k: dict(v) for k, v in DEFAULT_MODELS.items()}
    for k, v in raw.get("models", {}).items():
        models[k] = {**models.get(k, {}), **(v if isinstance(v, dict) else {"id": v})}
    cfg = Config(
        root=root, secrets=secrets,
        key_file=rel(paths["key_file"], secrets) if "key_file" in paths else secrets / "api.key",
        admin_key_file=rel(paths["admin_key_file"], secrets) if "admin_key_file" in paths else secrets / "admin.key",
        cap=float(cap) if cap is not None else None,
        worktrees=rel(paths.get("worktrees", ".claude/worktrees")),
        runs=rel(paths["runs"]) if "runs" in paths else secrets / "agent_runs",
        briefs=rel(paths.get("briefs", "briefs")),
        usage_log=secrets / paths.get("usage_log", "api_usage.jsonl"),
        deny=list(agent.get("deny", [])), path_extra=[str(rel(x)) for x in agent.get("path", [])],
        rules=agent.get("rules", DEFAULT_RULES), default_model=agent.get("default_model", "sonnet"),
        default_base=agent.get("default_base", "main"), default_max_turns=int(agent.get("max_turns", 80)),
        default_effort=agent.get("effort", "medium") or None,
        thinking_display=agent.get("thinking_display") or None,
        port=int(dash.get("port", 8765)), models=models,
        runner_cmd=agent.get("runner_cmd"),
        pilot_prompt_file=rel(pilot["prompt_file"]) if "prompt_file" in pilot else None,
        pilot_handoff=pilot.get("handoff", "docs/HANDOFF.md"), pilot_model=pilot.get("model", "opus"),
        pilot_permission_mode=pilot.get("permission_mode", "auto"),
        oneshot_system=oneshot.get("system", ""), oneshot_model=oneshot.get("model", "opus"),
        oneshot_effort=oneshot.get("effort", "high"),
        claude_projects_dir=rel(claude["projects_dir"]) if "projects_dir" in claude else None,
    )
    return cfg
