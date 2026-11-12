"""Cloudflare Access sign-in and per-user permission levels for agentdash (stdlib only).

The second listener (127.0.0.1:<listen_port>, used only by the Cloudflare tunnel) requires on EVERY request a
valid `Cf-Access-Jwt-Assertion` header (or the `CF_Authorization` cookie). This module verifies that JWT
(RS256 against the team's JWKS), maps the e-mail to a level (view < operate < admin) and holds the endpoint
table, the CSRF rules and the audit log. Config: `[dash.access]` in ~/.agentcontroller/config.toml (hub.py).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

LEVELS = {"view": 1, "operate": 2, "admin": 3}
SKEW = 60
JWKS_MIN_INTERVAL = 60
# DER DigestInfo prefix for SHA-256 (RFC 8017 section 9.2 note 1)
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")

# (method, path) -> minimum level. Anything not listed needs admin (default-deny).
ENDPOINTS: dict[tuple[str, str], str] = {
    **{("GET", p): "view" for p in ("/", "/api/health", "/api/me", "/api/projects", "/api/jobs", "/api/lmstudio",
                                    "/api/attention", "/api/runs", "/api/spend", "/api/config", "/api/roadmap",
                                    "/api/worktree", "/api/stream")},
    ("GET", "/api/billed"): "operate",
    **{("POST", p): "operate" for p in ("/api/inbox", "/api/local_approve", "/api/primary", "/api/launch", "/api/kill")},
    **{("POST", p): "admin" for p in ("/api/archive", "/api/worktree_delete", "/api/forget_project")},
}


def required_level(method: str, path: str) -> str:
    if path == "/rmui" or path.startswith("/rmui/"):  # WebRoadmap's own page: viewing is view, editing is operate
        return "view" if method == "GET" else "operate"
    return ENDPOINTS.get((method, path), "admin")


def allows(level: str | None, need: str) -> bool:
    return LEVELS.get(level or "", 0) >= LEVELS[need]


# ---------- settings ----------
def settings(gcfg: dict) -> dict | None:
    """The [dash.access] section when enabled and usable (aud set), else None. Paths expanded."""
    a = (gcfg.get("dash") or {}).get("access") or {}
    aud = a.get("aud")
    aud = [aud] if isinstance(aud, str) else [x for x in (aud or []) if isinstance(x, str)]
    if not a.get("enabled") or not aud:
        return None
    from . import registry
    exp = lambda v, d: Path(os.path.expandvars(str(v or registry.home() / d))).expanduser()
    team = str(a.get("team_domain") or "").strip().lower()
    if not team:
        return None
    return {"port": int(a.get("listen_port") or 8766), "team_domain": team, "aud": aud,
            "super_admin": str(a.get("super_admin") or "").strip().lower(),
            "grants_file": exp(a.get("grants_file"), "grants.json"),
            "public_origin": str(a.get("public_origin") or "").rstrip("/"),
            "audit_log": exp(a.get("audit_log"), "access-audit.jsonl")}


# ---------- JWT ----------
class AuthError(Exception):
    pass


def _b64d(s: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError) as e:
        raise AuthError("bad base64") from e


def _fetch_jwks(team_domain: str) -> dict:
    with urllib.request.urlopen(f"https://{team_domain}/cdn-cgi/access/certs", timeout=10) as f:
        return json.loads(f.read())


class Keys:
    """JWKS cache: kid -> (n, e). An unknown kid refetches at most once per 60 s."""

    def __init__(self, team_domain: str):
        self.team, self.keys, self.fetched, self.lock = team_domain, {}, 0.0, threading.Lock()

    def get(self, kid: str):
        with self.lock:
            if kid not in self.keys and time.time() - self.fetched >= JWKS_MIN_INTERVAL:
                self.fetched = time.time()
                try:
                    doc = _fetch_jwks(self.team)
                    keys = {}
                    for k in doc.get("keys", []):
                        if k.get("kty") == "RSA" and k.get("kid"):
                            keys[k["kid"]] = (int.from_bytes(_b64d(k["n"]), "big"), int.from_bytes(_b64d(k["e"]), "big"))
                    if keys:
                        self.keys = keys
                except (OSError, ValueError, KeyError, AttributeError, AuthError):
                    pass
            return self.keys.get(kid)


def rs256_ok(signing_input: bytes, sig: bytes, n: int, e: int) -> bool:
    """Compare the full expected EMSA-PKCS1-v1_5 block with pow(sig, e, n); the decrypted block is never parsed."""
    k = (n.bit_length() + 7) // 8
    if len(sig) != k or int.from_bytes(sig, "big") >= n or k < len(SHA256_PREFIX) + 32 + 11:
        return False
    t = SHA256_PREFIX + hashlib.sha256(signing_input).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big"), expected)


def verify_jwt(token: str, keys: Keys, team_domain: str, aud: list[str], now: float | None = None) -> dict:
    """Returns the claims (with a lowercased `email`) or raises AuthError."""
    now = time.time() if now is None else now
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("malformed token")
    try:
        header, claims = json.loads(_b64d(parts[0])), json.loads(_b64d(parts[1]))
    except ValueError as e:
        raise AuthError("malformed token") from e
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise AuthError("malformed token")
    if header.get("alg") != "RS256":
        raise AuthError("alg not allowed")
    kid = header.get("kid")
    key = keys.get(kid) if isinstance(kid, str) else None
    if not key:
        raise AuthError("unknown key")
    if not rs256_ok((parts[0] + "." + parts[1]).encode("ascii", "replace"), _b64d(parts[2]), *key):
        raise AuthError("bad signature")
    if claims.get("iss") != f"https://{team_domain}":
        raise AuthError("bad issuer")
    ca = claims.get("aud")
    ca = [ca] if isinstance(ca, str) else ca if isinstance(ca, list) else []
    if not set(ca) & set(aud):
        raise AuthError("bad audience")
    exp, nbf = claims.get("exp"), claims.get("nbf")
    if not isinstance(exp, (int, float)) or now > exp + SKEW:
        raise AuthError("expired")
    if nbf is not None and (not isinstance(nbf, (int, float)) or now < nbf - SKEW):
        raise AuthError("not yet valid")
    if claims.get("type", "app") != "app":
        raise AuthError("not a user token")
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        raise AuthError("no email (service token?)")
    claims["email"] = email.strip().lower()
    return claims


def token_from(headers) -> str:
    t = headers.get("Cf-Access-Jwt-Assertion", "")
    if t:
        return t.strip()
    for c in headers.get("Cookie", "").split(";"):
        k, _, v = c.strip().partition("=")
        if k == "CF_Authorization":
            return v
    return ""


# ---------- grants ----------
class Grants:
    """grants.json re-read when its mtime changes; missing or invalid means nobody but the super admin."""

    def __init__(self, path: Path, service: str = "agentdash"):
        self.path, self.service, self.mt, self.map = path, service, None, {}
        self.lock = threading.Lock()

    def _load(self) -> dict:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            svc = doc["services"][self.service]
            if doc.get("version") != 1 or not isinstance(svc, dict):
                return {}
            return {str(k).strip().lower(): v for k, v in svc.items() if v in LEVELS}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def level(self, email: str) -> str | None:
        with self.lock:
            try:
                mt = self.path.stat().st_mtime_ns
            except OSError:
                mt = None
            if mt != self.mt or mt is None:
                self.mt, self.map = mt, (self._load() if mt is not None else {})
            return self.map.get(email)


class Access:
    def __init__(self, st: dict):
        self.st = st
        self.keys = Keys(st["team_domain"])
        self.grants = Grants(st["grants_file"])
        self._alock = threading.Lock()

    def authenticate(self, headers) -> tuple[str, str | None]:
        """(email, level or None). Raises AuthError for a missing or invalid token."""
        tok = token_from(headers)
        if not tok:
            raise AuthError("no Access token")
        email = verify_jwt(tok, self.keys, self.st["team_domain"], self.st["aud"])["email"]
        if self.st["super_admin"] and email == self.st["super_admin"]:
            return email, "admin"
        return email, self.grants.level(email)

    def csrf_ok(self, headers) -> bool:
        origin = headers.get("Origin")
        return (headers.get("Content-Type", "").startswith("application/json") and headers.get("X-AC") == "1"
                and bool(origin) and origin == self.st["public_origin"] and bool(self.st["public_origin"]))

    def audit(self, email, level, method, path, query, body, status) -> None:
        keys = {}
        for k, v in (body or {}).items():
            keys[k] = "<long text>" if isinstance(v, str) and len(v) > 200 else (v if isinstance(v, (str, int, float, bool)) or v is None else "<" + type(v).__name__ + ">")
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "email": email, "level": level, "method": method,
               "path": path, "query": query, "body": keys, "status": status}
        try:
            with self._alock:
                self.st["audit_log"].parent.mkdir(parents=True, exist_ok=True)
                with self.st["audit_log"].open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
