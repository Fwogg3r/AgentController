"""Load and merge the four layers: model facts, tier policy, repo settings, environment."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

AUTONOMY = ["locked", "edit", "shell", "full"]  # increasing; shell/full need a sandbox
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiers.example.toml"


@dataclass
class Model:
    name: str
    id: str
    tier: str
    endpoint: str = "http://localhost:1234/v1"
    api_key: str = "local"
    context_tokens: int = 32768
    max_output: int = 8192
    reserve: int = 1024
    chars_per_token: float = 3.2
    no_think: dict = field(default_factory=dict)   # {"method": "suffix", "value": "/no_think"} | {"method": "param", ...}
    reasoning: dict = field(default_factory=lambda: {"field": "reasoning_content", "tag": "think"})
    sampling: dict = field(default_factory=dict)
    timeout: int = 900
    aider_model: str = ""                          # e.g. "openai/<id>"; default derived from id
    aider_settings: dict = field(default_factory=dict)  # extra keys for aider's model settings (examples_as_sys_msg...)

    @property
    def input_budget(self) -> int:
        return self.context_tokens - self.max_output - self.reserve


@dataclass
class Tier:
    name: str
    runner: str = "staged"            # staged | aider-single | passthrough
    decompose: str = "always"         # always | never
    max_stages: int = 8
    max_edit_files: int = 2
    allow_new_files: bool = True
    thinking: dict = field(default_factory=lambda: {"plan": 8000, "spec": 6000, "fix": 4000, "edit": 0})
    edit_format: str = "diff"
    map_tokens: int = 0               # aider repo map; staged runs pass exact files, passthrough may want ~4096
    max_fix_attempts: int = 2
    autonomy: str = "edit"            # locked: no unattended test runs; edit: tier's gates; shell/full: sandbox only
    max_replans: int = 1              # replans of the remaining stages after a stage fails its fix attempts
    gates: list[str] = field(default_factory=lambda: ["plan", "stage", "tests"])
    instructions: str = ""            # path to an instruction file given to every call (the model's "model.md")


@dataclass
class Repo:
    root: Path
    test_cmd: str = "uv run pytest -q"
    acceptance_allow: list[str] = field(default_factory=lambda: ["uv run pytest*"])
    protected: list[str] = field(default_factory=lambda: ["tests/**"])
    approve_hook: str = ""            # command run with the approval JSON on stdin; exit 0 = approve
    tiers_path: str = ""


@dataclass
class Settings:
    model: Model
    tier: Tier
    repo: Repo
    sandbox: bool

    @property
    def autonomy(self) -> str:
        """Effective autonomy = min(tier, environment): without a sandbox, never above 'edit'."""
        cap = "full" if self.sandbox else "edit"
        return AUTONOMY[min(AUTONOMY.index(self.tier.autonomy), AUTONOMY.index(cap))]

    def gates(self) -> list[str]:
        """The tier's gates, plus a tests gate when the effective autonomy is 'locked'."""
        return list(dict.fromkeys(self.tier.gates + (["tests"] if self.autonomy == "locked" else [])))

    def instructions(self) -> str:
        p = self.tier.instructions
        return Path(os.path.expanduser(p)).read_text(encoding="utf-8") if p and Path(os.path.expanduser(p)).exists() else ""


def _repo(root: Path) -> Repo:
    f = root / "agentcontroller.toml"
    data = tomllib.loads(f.read_text(encoding="utf-8")).get("local", {}) if f.exists() else {}
    return Repo(root=root, **{k: v for k, v in data.items() if k in Repo.__dataclass_fields__ and k != "root"})


def tiers_file(repo: Repo) -> Path:
    for p in (os.environ.get("AGENTCONTROLLER_TIERS"), repo.tiers_path, "~/.agentcontroller/tiers.toml"):
        if p and Path(os.path.expanduser(p)).exists():
            return Path(os.path.expanduser(p))
    return EXAMPLE


def _pick(cls, name: str, data: dict):
    return cls(name=name, **{k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "name"})


def load(model: str, root: Path, tier: str | None = None) -> Settings:
    repo = _repo(root)
    data = tomllib.loads(tiers_file(repo).read_text(encoding="utf-8"))
    models, tiers = data.get("models", {}), data.get("tiers", {})
    if model not in models:
        raise SystemExit(f"unknown model {model!r}; known: {', '.join(models) or 'none'} (tiers file {tiers_file(repo)})")
    m = _pick(Model, model, {"id": model, **models[model]})
    tname = tier or m.tier
    if tname not in tiers:
        raise SystemExit(f"unknown tier {tname!r}")
    t = _pick(Tier, tname, tiers[tname])
    if t.autonomy not in AUTONOMY:
        raise SystemExit(f"tier {tname!r}: autonomy {t.autonomy!r} is not one of {AUTONOMY}")
    if t.runner not in ("staged", "aider-single", "passthrough"):
        raise SystemExit(f"tier {tname!r}: unknown runner {t.runner!r}")
    sandbox = os.environ.get("AGENTLOCAL_SANDBOX") == "1"
    return Settings(m, t, repo, sandbox)
