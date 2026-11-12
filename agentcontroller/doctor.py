"""agentdoctor: check that AgentController and everything around it is installed and reachable.

    agentdoctor                # table of required / recommended / optional pieces; exit 0 only if all required are OK
    agentdoctor --json         # the same as JSON
    agentdoctor --scan         # also probe the local /24 for LM Studio (:1234) and pmjc (:8770); asks first
    agentdoctor -y             # answer yes to every question (scan, writing found services into the config)

Nothing here reads or prints a secret: the API key is only checked for presence (and non-zero size).
Found services can be written into ~/.agentcontroller/config.toml ([lmstudio.hosts], [pmjc] url); that is a
targeted text edit, so comments and every other key stay as they are. Tokens are never written or created.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess

from . import NO_WINDOW
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path

LMSTUDIO_PORT = 1234
PMJC_PORT = 8770
LOCAL_LMSTUDIO = f"http://127.0.0.1:{LMSTUDIO_PORT}"
LOCAL_PMJC = f"http://127.0.0.1:{PMJC_PORT}"
COMMANDS = ["agentctl", "agentdash", "agentask", "agentwatch", "agentwait", "agentusage", "agentpilot",
            "agentlocal", "agentdoctor"]
CORE_COMMANDS = ["agentctl", "agentdash", "agentask"]
TASK_NAME = "AgentController dashboard"
CLAUDE_INSTALL_HINT = ("install Claude Code (https://docs.claude.com/en/docs/claude-code/setup): "
                       "`npm install -g @anthropic-ai/claude-code`, or the native installer from that page")


@dataclass
class Check:
    id: str
    name: str
    category: str          # required | recommended | optional
    status: str            # ok | missing | warn | n/a
    detail: str = ""
    hint: str = ""

    @property
    def label(self) -> str:
        if self.status == "ok":
            return "OK"
        if self.status == "n/a":
            return "n/a"
        if self.status == "warn":
            return "WARN"
        return "MISSING" if self.category == "required" else f"missing ({self.category})"


@dataclass
class Service:
    kind: str              # lmstudio | pmjc
    url: str
    detail: str = ""
    local: bool = False


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status == "ok" for c in self.checks if c.category == "required")


# ---------- seams (tests patch these; nothing else touches the system) ----------

def _which(exe: str) -> str | None:
    return shutil.which(exe)


def _version(path: str, *args: str) -> str:
    """First line of `<path> --version`, or ''. Never raises."""
    try:
        r = subprocess.run([path, *(args or ("--version",))], capture_output=True, text=True, timeout=15, **NO_WINDOW)
        return ((r.stdout or r.stderr).strip().splitlines() or [""])[0][:120]
    except (OSError, subprocess.SubprocessError):
        return ""


def _http_json(url: str, timeout: float = 2.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (a URL from the user's own config or a LAN probe)
        return json.loads(r.read().decode("utf-8"))


def _task_exists() -> bool | None:
    """Whether the dashboard logon task exists (Windows only; None elsewhere)."""
    if os.name != "nt":
        return None
    try:
        return subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, timeout=15, **NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _dash_up(port: int) -> bool:
    try:
        return _http_json(f"http://127.0.0.1:{port}/api/health", timeout=1.0).get("mode") == "all"
    except (OSError, ValueError, AttributeError):
        return False


def _tool_bin_dir() -> Path | None:
    try:
        out = subprocess.run(["uv", "tool", "dir", "--bin"], capture_output=True, text=True, timeout=15, **NO_WINDOW).stdout.strip()
        return Path(out) if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def _local_ipv4() -> str | None:
    """The address of the interface that would be used to reach the LAN (UDP connect sends nothing)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


# ---------- config ----------

def home() -> Path:
    return Path(os.environ.get("AGENTCONTROLLER_HOME") or Path.home() / ".agentcontroller")


def config_path() -> Path:
    return home() / "config.toml"


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path | None = None) -> dict:
    """The global config as agentdash sees it: its own keys over the file named by `include`."""
    own = _read_toml(path or config_path())
    inc = own.get("include")
    base = _read_toml(Path(os.path.expandvars(inc)).expanduser()) if isinstance(inc, str) and inc else {}
    return _merge(base, own)


def _norm_url(u: str) -> str:
    return u.strip().rstrip("/").lower().replace("localhost", "127.0.0.1")


# ---------- individual checks ----------

def check_basics() -> list[Check]:
    out = []
    uv = _which("uv")
    out.append(Check("uv", "uv", "required", "ok" if uv else "missing", _version(uv) if uv else "not on PATH",
                     "" if uv else "install it: https://docs.astral.sh/uv/getting-started/installation/"))
    git = _which("git")
    out.append(Check("git", "git", "required", "ok" if git else "missing", _version(git) if git else "not on PATH",
                     "" if git else "install git (https://git-scm.com/downloads); agents work in git worktrees"))
    return out


def check_commands() -> Check:
    found = [c for c in COMMANDS if _which(c)]
    missing_core = [c for c in CORE_COMMANDS if c not in found]
    if not missing_core:
        extra = [c for c in COMMANDS if c not in found]
        return Check("commands", "AgentController commands", "required", "ok",
                     f"{len(found)}/{len(COMMANDS)} on PATH" + (f" (not yet: {', '.join(extra)}; rerun install.py)" if extra else ""))
    bindir = _tool_bin_dir()
    if bindir and any((bindir / (c + ext)).exists() for c in CORE_COMMANDS for ext in ("", ".exe")):
        return Check("commands", "AgentController commands", "required", "missing",
                     f"installed in {bindir} but that folder is not on PATH", "run `uv tool update-shell` and open a new terminal")
    return Check("commands", "AgentController commands", "required", "missing", f"not found: {', '.join(missing_core)}",
                 "run `uv run install.py` from the AgentController checkout")


def check_sdk() -> Check:
    try:
        ver = metadata.version("claude-agent-sdk")
    except metadata.PackageNotFoundError:
        return Check("sdk", "claude-agent-sdk", "required", "missing",
                     "not importable here (run agentdoctor from the installed tool, not a bare Python)",
                     "`uv run install.py` installs it with the tool")
    bundled = False
    try:
        import claude_agent_sdk
        b = Path(claude_agent_sdk.__file__).parent / "_bundled"
        bundled = b.is_dir() and any(b.iterdir())
    except Exception:  # noqa: BLE001
        pass
    return Check("sdk", "claude-agent-sdk", "required", "ok",
                 f"{ver}; " + ("bundles its own Claude Code binary, so agentctl/agentask need no separate install"
                               if bundled else "uses `claude` from PATH"))


def check_claude_cli() -> Check:
    p = _which("claude")
    if p:
        return Check("claude", "Claude Code CLI", "recommended", "ok", _version(p) or p)
    return Check("claude", "Claude Code CLI", "recommended", "missing",
                 "not on PATH: the /api-request and /batchy skills and agentpilot need it (agentctl/agentask don't)",
                 CLAUDE_INSTALL_HINT)


def check_skills(claude_dir: Path | None = None, claude_json: Path | None = None) -> list[Check]:
    cd = claude_dir or Path.home() / ".claude"
    cj = claude_json or Path.home() / ".claude.json"
    api = (cd / "skills" / "api-request" / "SKILL.md").is_file()
    out = [Check("skill-api", "/api-request skill", "recommended", "ok" if api else "missing",
                 str(cd / "skills" / "api-request") if api else "not installed", "" if api else "`uv run install.py`")]
    batchy = (cd / "skills" / "batchy" / "SKILL.md").is_file()
    try:
        mcp = "claude-batch" in json.loads(cj.read_text(encoding="utf-8")).get("mcpServers", {})
    except (OSError, ValueError):
        mcp = False
    if batchy and mcp:
        out.append(Check("skill-batchy", "/batchy skill + MCP server", "optional", "ok", "skill installed, claude-batch registered"))
    elif batchy:
        out.append(Check("skill-batchy", "/batchy skill + MCP server", "optional", "warn",
                         "skill installed but the claude-batch MCP server is not registered (no API key at install time)",
                         "rerun `uv run install.py --key-file <file>`"))
    else:
        out.append(Check("skill-batchy", "/batchy skill + MCP server", "optional", "missing", "not installed",
                         "`uv run install.py` (without --no-batchy)"))
    return out


def check_roadmap() -> Check:
    p = _which("roadmap")
    if p:
        return Check("roadmap", "roadmap CLI (WebRoadmap)", "required", "ok", _version(p) or p)
    return Check("roadmap", "roadmap CLI (WebRoadmap)", "required", "missing",
                 "not on PATH: the dashboard's Roadmap tab and project roadmaps need it",
                 "`uv run install.py` (installs it at a pinned commit)")


def check_aider() -> Check:
    p = _which("aider")
    if p:
        return Check("aider", "aider (agentlocal)", "optional", "ok", _version(p) or p)
    return Check("aider", "aider (agentlocal)", "optional", "missing", "only needed for local-model agents",
                 "`uv run install.py --local` (installs the pinned commit)")


def check_cloudflared(cfg: dict) -> Check:
    p = _which("cloudflared")
    access = bool(cfg.get("dash", {}).get("access", {}).get("enabled"))
    if p:
        return Check("cloudflared", "cloudflared", "optional", "ok", (_version(p) or p) + ("; [dash.access] enabled" if access else ""))
    return Check("cloudflared", "cloudflared", "optional", "missing",
                 "only for internet access to the dashboard" + ("; [dash.access] is enabled but there is no tunnel binary" if access else ""),
                 "see README, section Internet access")


def check_dashboard(cfg: dict) -> list[Check]:
    port = int(os.environ.get("AGENTDASH_PORT") or cfg.get("dash", {}).get("port") or 8765)
    up = _dash_up(port)
    out = [Check("dash", "dashboard (agentdash --all)", "recommended", "ok" if up else "missing",
                 f"http://127.0.0.1:{port}/" if up else f"nothing answering on port {port}",
                 "" if up else "start it with `agentdash` (the CLIs also start it on demand)")]
    t = _task_exists()
    if t is None:
        out.append(Check("task", "dashboard logon task", "optional", "n/a", "Windows only; elsewhere agentdash is started on demand"))
    else:
        out.append(Check("task", "dashboard logon task", "recommended", "ok" if t else "missing",
                         f"'{TASK_NAME}'" if t else "not installed",
                         "" if t else "`uv run install.py --dash-task`"))
    cp = config_path()
    out.append(Check("config", "global config", "recommended", "ok" if cp.is_file() else "missing", str(cp),
                     "" if cp.is_file() else "`uv run install.py` writes it with commented defaults"))
    return out


def _projects() -> list[dict]:
    d = home() / "projects"
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(e, dict) and e.get("toml"):
            out.append(e)
    return out


def check_keys(cwd: Path | None = None) -> list[Check]:
    """One row per configured project: is the API key file there and non-empty? (Presence only; never read.)"""
    from . import config as projcfg
    tomls: list[tuple[str, Path]] = []
    here = projcfg.find_project(cwd)
    if here:
        tomls.append((here.parent.name, here))
    for e in _projects():
        p = Path(e["toml"])
        if p.is_file() and all(p != t for _, t in tomls):
            tomls.append((e.get("name") or p.parent.name, p))
    if not tomls:
        return [Check("key", "API key", "recommended", "missing", "no project with an agentcontroller.toml yet",
                      "`uv run install.py --init /path/to/project` creates it and stores your key")]
    out = []
    for name, toml in tomls[:10]:
        try:
            kf = projcfg.load(toml).key_file
            st = kf.stat() if kf.is_file() else None
        except Exception as e:  # noqa: BLE001
            out.append(Check(f"key:{name}", f"API key: {name}", "recommended", "warn", f"config unreadable ({type(e).__name__})"))
            continue
        if st and st.st_size > 0:
            out.append(Check(f"key:{name}", f"API key: {name}", "recommended", "ok", "set"))
        else:
            out.append(Check(f"key:{name}", f"API key: {name}", "recommended", "missing",
                             "key file missing or empty" , f"put your Anthropic API key (sk-ant-api...) in {kf}"))
    if len(tomls) > 10:
        out.append(Check("key:more", "API key: others", "recommended", "n/a", f"{len(tomls) - 10} more projects not checked"))
    return out


# ---------- services ----------

def probe_lmstudio(url: str, timeout: float = 2.0) -> Service | None:
    try:
        data = _http_json(url.rstrip("/") + "/api/v0/models", timeout=timeout)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    models = [m for m in data["data"] if isinstance(m, dict) and m.get("type") in ("llm", "vlm", None)]
    loaded = sum(1 for m in models if m.get("state") == "loaded")
    return Service("lmstudio", url.rstrip("/"), f"{len(models)} model(s), {loaded} loaded")


def probe_pmjc(url: str, timeout: float = 2.0) -> Service | None:
    """pmjc's GET /api/health needs no token and answers {"ok", "version", "connectors", "tools", "jobs"}."""
    try:
        data = _http_json(url.rstrip("/") + "/api/health", timeout=timeout)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("ok") or "connectors" not in data:
        return None
    return Service("pmjc", url.rstrip("/"), f"version {data.get('version', '?')}, {data.get('connectors')} connectors, {data.get('jobs')} jobs")


def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def subnet_hosts(ip: str | None = None, limit: int = 254) -> list[str]:
    """The other addresses of ip's /24 (the doctor never scans anything larger), at most `limit`."""
    ip = ip or _local_ipv4()
    if not ip:
        return []
    try:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    except ValueError:
        return []
    if not net.is_private:
        return []
    return [str(h) for h in net.hosts() if str(h) != ip][:limit]


def scan_subnet(hosts: list[str], connect_timeout: float = 0.4, http_timeout: float = 1.5, workers: int = 64) -> list[Service]:
    """Fast parallel probe: a TCP connect on each port first, then identify the service over HTTP."""
    def one(host: str) -> list[Service]:
        found = []
        if _port_open(host, LMSTUDIO_PORT, connect_timeout) and (s := probe_lmstudio(f"http://{host}:{LMSTUDIO_PORT}", http_timeout)):
            found.append(s)
        if _port_open(host, PMJC_PORT, connect_timeout) and (s := probe_pmjc(f"http://{host}:{PMJC_PORT}", http_timeout)):
            found.append(s)
        return found
    out: list[Service] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(one, hosts):
            out.extend(r)
    return out


def find_local_services(cfg: dict) -> list[Service]:
    """LM Studio and pmjc on this machine, plus every LM Studio host and the pmjc URL already in the config."""
    found: list[Service] = []
    seen: set[str] = set()

    def add(s: Service | None, local: bool = False) -> None:
        if s and (s.kind, _norm_url(s.url)) not in seen:
            seen.add((s.kind, _norm_url(s.url)))
            s.local = local
            found.append(s)

    add(probe_lmstudio(LOCAL_LMSTUDIO), True)
    add(probe_pmjc(LOCAL_PMJC), True)
    for url in (cfg.get("lmstudio", {}).get("hosts") or {}).values():
        if isinstance(url, str):
            add(probe_lmstudio(url))
    if isinstance(cfg.get("pmjc", {}).get("url"), str):
        add(probe_pmjc(cfg["pmjc"]["url"]))
    return found


def service_checks(cfg: dict, found: list[Service]) -> list[Check]:
    out = []
    lms = [s for s in found if s.kind == "lmstudio"]
    configured = cfg.get("lmstudio", {}).get("hosts") or {}
    if lms:
        out.append(Check("lmstudio", "LM Studio", "optional", "ok", "; ".join(f"{s.url} ({s.detail})" for s in lms)))
    else:
        hosts = ", ".join(configured.values()) or LOCAL_LMSTUDIO
        out.append(Check("lmstudio", "LM Studio", "optional", "missing", f"not answering at {hosts}",
                         "only for local models: start LM Studio's server (Developer tab) or add a host with `agentdoctor --scan`"))
    pm = [s for s in found if s.kind == "pmjc"]
    from .hub import pmjc_token_file
    token_file = pmjc_token_file(cfg)
    if pm:
        detail = "; ".join(f"{s.url} ({s.detail})" for s in pm)
        if not token_file:
            out.append(Check("pmjc", "pmjc (PersonalMCPJobController)", "optional", "warn",
                             detail + "; no token_file or secrets_dir configured", PMJC_TOKEN_HINT))
        elif not token_file.is_file():
            out.append(Check("pmjc", "pmjc (PersonalMCPJobController)", "optional", "warn", detail + f"; token file {token_file} does not exist",
                             PMJC_TOKEN_HINT))
        else:
            out.append(Check("pmjc", "pmjc (PersonalMCPJobController)", "optional", "ok", detail + "; token file set"))
    else:
        out.append(Check("pmjc", "pmjc (PersonalMCPJobController)", "optional", "missing",
                         f"not answering at {cfg.get('pmjc', {}).get('url') or LOCAL_PMJC}",
                         "only for the Jobs tab, approvals and notifications: https://github.com/Fwogg3r/PersonalMCPJobController"))
    return out


PMJC_TOKEN_HINT = ("create a token with the `jobs` scope (add `admin` only if you want pmjc approvals in the dashboard): "
                   "`pmjc token create agentdash --scopes jobs` or the Tokens tab of pmjc's dashboard; save it as "
                   "<secrets_dir>/pmjc/agentdash.token (set `secrets_dir` at the top of ~/.agentcontroller/config.toml), "
                   "or anywhere outside a repo with `token_file` under [pmjc]")


# ---------- config merge (targeted text edits; comments and other keys survive) ----------

def _toml_str(s: str) -> str:
    return json.dumps(s)  # a JSON string is a valid TOML basic string


def _insert_after_header(lines: list[str], header: re.Pattern, new: list[str]) -> bool:
    for i, ln in enumerate(lines):
        if header.match(ln):
            lines[i + 1:i + 1] = new
            return True
    return False


def merge_services(path: Path, lm_hosts: dict[str, str] | None = None, pmjc_url: str | None = None) -> list[str]:
    """Add LM Studio hosts to [lmstudio.hosts] and a pmjc url to [pmjc] in the config file at `path`, never
    replacing an existing name or url. Returns what was written. Raises ValueError (nothing written) if the file
    does not parse before or after the edit, or defines the table in a form this editor does not touch."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        cur = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path} is not valid TOML ({e}); fix it first") from e
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    done: list[str] = []
    nl_before = "\n" if text and not text.endswith("\n") else ""

    existing_hosts = cur.get("lmstudio", {}).get("hosts") or {}
    add_hosts = {n: u for n, u in (lm_hosts or {}).items()
                 if n not in existing_hosts and _norm_url(u) not in {_norm_url(x) for x in existing_hosts.values() if isinstance(x, str)}}
    if add_hosts:
        entries = [f"{n} = {_toml_str(u)}" for n, u in add_hosts.items()]
        if "hosts" in cur.get("lmstudio", {}):
            if not _insert_after_header(lines, re.compile(r"^\s*\[lmstudio\.hosts\]\s*(#.*)?$"), entries):
                raise ValueError("[lmstudio] hosts is defined in a form agentdoctor does not edit; add the hosts by hand")
        elif "lmstudio" in cur and not isinstance(cur["lmstudio"], dict):
            raise ValueError("lmstudio is not a table")
        else:
            lines += ["", "[lmstudio.hosts]", *entries]
        done += [f"lmstudio host {n} = {u}" for n, u in add_hosts.items()]

    if pmjc_url and "url" not in cur.get("pmjc", {}):
        entry = [f"url = {_toml_str(pmjc_url)}"]
        if "pmjc" in cur:
            if not _insert_after_header(lines, re.compile(r"^\s*\[pmjc\]\s*(#.*)?$"), entry):
                raise ValueError("[pmjc] is defined in a form agentdoctor does not edit; add the url by hand")
        else:
            lines += ["", "[pmjc]", *entry,
                      "# token_file = \"...\"   # default <secrets_dir>/pmjc/agentdash.token: a pmjc token with the `jobs` scope"]
        done.append(f"pmjc url = {pmjc_url}")

    if not done:
        return []
    new = nl_before + "\n".join(lines) + "\n"
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:  # never leave a broken config behind
        raise ValueError(f"the edit would make {path} invalid ({e}); nothing written") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return done


def _lm_name(url: str, taken: set[str]) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
    base = "local" if host in ("127.0.0.1", "localhost") else "lan-" + host.split(".")[-1]
    name, i = base, 2
    while name in taken:
        name, i = f"{base}-{i}", i + 1
    return name


def plan_config_writes(cfg: dict, found: list[Service]) -> tuple[dict[str, str], str | None]:
    """Which found services are not in the config yet. A fresh [lmstudio.hosts] replaces the built-in default
    (local), so the local server is listed too when it is up."""
    hosts = cfg.get("lmstudio", {}).get("hosts") or {}
    known = {_norm_url(u) for u in hosts.values() if isinstance(u, str)}
    taken = set(hosts)
    add: dict[str, str] = {}
    lms = [s for s in found if s.kind == "lmstudio"]
    for s in sorted(lms, key=lambda s: not s.local):
        if _norm_url(s.url) in known:
            continue
        if s.local and not hosts and len(lms) == 1:
            continue  # the built-in default already covers a lone local server
        name = _lm_name(s.url, taken | set(add))
        add[name] = s.url
    if add and not hosts:
        loc = next((s for s in lms if s.local), None)
        if loc and loc.url not in add.values():
            add = {"local": loc.url, **add}
    pm_url = None
    if "url" not in cfg.get("pmjc", {}):
        pm = [s for s in found if s.kind == "pmjc"]
        if pm:
            pm_url = sorted(pm, key=lambda s: not s.local)[0].url
    return add, pm_url


# ---------- orchestration ----------

def ask(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def run_checks(found: list[Service] | None = None, cwd: Path | None = None) -> Report:
    cfg = load_config()
    rep = Report()
    rep.checks += check_basics()
    rep.checks.append(check_commands())
    rep.checks.append(check_sdk())
    rep.checks.append(check_roadmap())
    rep.checks.append(check_claude_cli())
    rep.checks += check_skills()
    rep.checks += check_dashboard(cfg)
    rep.checks += check_keys(cwd)
    rep.checks.append(check_aider())
    rep.services = find_local_services(cfg) if found is None else list(found)
    rep.checks += service_checks(cfg, rep.services)
    rep.checks.append(check_cloudflared(cfg))
    return rep


def format_table(rep: Report) -> str:
    rows = [("CHECK", "STATUS", "DETAIL")] + [(c.name, c.label, c.detail) for c in rep.checks]
    w0 = max(len(r[0]) for r in rows)
    w1 = max(len(r[1]) for r in rows)
    lines = [f"{a:<{w0}}  {b:<{w1}}  {c}".rstrip() for a, b, c in rows]
    hints = [f"  - {c.name}: {c.hint}" for c in rep.checks if c.status != "ok" and c.status != "n/a" and c.hint]
    if hints:
        lines += ["", "What to do:", *hints]
    req_bad = [c.name for c in rep.checks if c.category == "required" and c.status != "ok"]
    lines += ["", "All required pieces are OK." if not req_bad else "Required pieces missing: " + ", ".join(req_bad)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentdoctor", description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--scan", action="store_true", help="also probe the local /24 for LM Studio and pmjc (asks first)")
    ap.add_argument("-y", "--yes", action="store_true", help="answer yes to every question (scan, config writes)")
    ap.add_argument("--no-write", action="store_true", help="never offer to write found services into the config")
    args = ap.parse_args(argv)
    quiet = args.json
    rep = run_checks()

    if args.scan:
        hosts = subnet_hosts()
        if not hosts:
            if not quiet:
                print("--scan: no private IPv4 network found; nothing to probe")
        elif ask(f"Probe {len(hosts)} addresses ({hosts[0]} - {hosts[-1]}) for LM Studio (:{LMSTUDIO_PORT}) and "
                 f"pmjc (:{PMJC_PORT})? Short timeouts, this subnet only.", args.yes):
            known = {(s.kind, _norm_url(s.url)) for s in rep.services}
            new = [s for s in scan_subnet(hosts) if (s.kind, _norm_url(s.url)) not in known]
            rep.services += new
            rep.checks = [c for c in rep.checks if c.id not in ("lmstudio", "pmjc")]
            rep.checks += service_checks(load_config(), rep.services)
            if not quiet:
                print(f"scan: found {len(new)} service(s) on the LAN" + "".join(f"\n  {s.kind}  {s.url}  {s.detail}" for s in new))
        elif not quiet:
            print("scan skipped")

    cfg = load_config()
    add_hosts, pm_url = plan_config_writes(cfg, rep.services)
    written: list[str] = []
    if (add_hosts or pm_url) and not args.no_write:
        desc = [f"[lmstudio.hosts] {n} = {u}" for n, u in add_hosts.items()] + ([f"[pmjc] url = {pm_url}"] if pm_url else [])
        if not quiet:
            print("\nFound but not in " + str(config_path()) + ":\n" + "\n".join("  " + d for d in desc))
        if ask(f"Add {'it' if len(desc) == 1 else 'them'} to {config_path()}? (comments and other keys are kept; a .bak is made)", args.yes):
            try:
                written = merge_services(config_path(), add_hosts, pm_url)
            except ValueError as e:
                if not quiet:
                    print(f"not written: {e}")
        elif not quiet and not sys.stdin.isatty():
            print("(not writing without -y; rerun `agentdoctor -y` to add them)")
    if any(s.kind == "pmjc" for s in rep.services) and not quiet:
        from .hub import pmjc_token_file
        tf = pmjc_token_file(load_config())
        if not tf or not tf.is_file():
            print("\npmjc needs a token for the dashboard: " + PMJC_TOKEN_HINT)

    if quiet:
        print(json.dumps({"ok": rep.ok, "checks": [{**asdict(c), "label": c.label} for c in rep.checks],
                          "services": [asdict(s) for s in rep.services], "config_written": written}, indent=2))
    else:
        print("\n" + format_table(rep))
        if written:
            print("\nWrote to " + str(config_path()) + ": " + "; ".join(written))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
