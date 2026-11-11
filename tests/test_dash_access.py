import base64
import hashlib
import http.client
import json
import random
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from dashfix import home  # noqa: F401
from agentcontroller import access, dash, hub

TEAM, AUD, ORIGIN = "team.example.cloudflareaccess.com", "aud-1", "https://dash.example.com"
ADMIN = "boss@example.com"


# ---------- a pure-Python RSA test key (1024-bit modulus) ----------
def _prime(rng, bits):
    while True:
        c = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if all(c % p for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)) and \
                all(_mr(c, rng) for _ in range(1)):
            return c


def _mr(n, rng, rounds=20):
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(rounds):
        x = pow(rng.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def make_key(seed):
    rng = random.Random(seed)
    e = 65537
    while True:
        p, q = _prime(rng, 512), _prime(rng, 512)
        if p != q and (p - 1) % e and (q - 1) % e:
            n = p * q
            return n, e, pow(e, -1, (p - 1) * (q - 1))


N, E, D = make_key(1)
N2, E2, D2 = make_key(2)


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sign(n, d, signing_input):
    k = (n.bit_length() + 7) // 8
    t = access.SHA256_PREFIX + hashlib.sha256(signing_input).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def jwt(email="a@example.com", kid="k1", key=None, alg="RS256", **over):
    n, d = key or (N, D)
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "exp": time.time() + 600, "iat": time.time(), "type": "app",
              "email": email, **over}
    claims = {k: v for k, v in claims.items() if v is not None}
    h = b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode()) + "." + b64(json.dumps(claims).encode())
    return h + "." + b64(sign(n, d, h.encode()))


def jwks(keys):
    return {"keys": [{"kty": "RSA", "kid": kid, "n": b64(n.to_bytes((n.bit_length() + 7) // 8, "big")),
                      "e": b64(e.to_bytes(3, "big"))} for kid, (n, e) in keys.items()]}


# ---------- server ----------
@pytest.fixture
def acc(home, monkeypatch):
    monkeypatch.setattr(dash, "ALL_MODE", True)
    monkeypatch.setattr(dash, "PINNED", None)
    monkeypatch.setattr(dash, "PINNED_ENTRY", None)
    monkeypatch.setattr(dash, "TOKEN", "lan-secret")  # the LAN token exists but must mean nothing on the access listener
    state = {"keys": {"k1": (N, E)}, "fetches": 0}

    def fetch(team):
        assert team == TEAM
        state["fetches"] += 1
        return jwks(state["keys"])

    monkeypatch.setattr(access, "_fetch_jwks", fetch)
    grants = home / "grants.json"
    st = {"port": 0, "team_domain": TEAM, "aud": [AUD], "super_admin": ADMIN, "grants_file": grants,
          "public_origin": ORIGIN, "audit_log": home / "audit.jsonl"}
    a = access.Access(st)
    handler = type("AccessHandler", (dash.Handler,), {"access": a})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def write_grants(d, mtime_bump=0):
        grants.write_text(json.dumps({"version": 1, "updated": "x", "services": {"agentdash": d}}))
        if mtime_bump:
            import os
            t = time.time() + mtime_bump
            os.utime(grants, (t, t))

    write_grants({"v@example.com": "view", "o@example.com": "operate", "a@example.com": "admin"})
    yield type("A", (), {"port": srv.server_address[1], "state": state, "grants": grants, "write": staticmethod(write_grants),
                         "audit": st["audit_log"], "obj": a})
    srv.shutdown()


def req(port, path, method="GET", token=None, headers=None, body=None, cookie=None, origin=ORIGIN, host=None):
    h = {}
    if token:
        h["Cf-Access-Jwt-Assertion"] = token
    if cookie:
        h["Cookie"] = cookie
    if method == "POST":
        h.update({"Content-Type": "application/json", "X-AC": "1"})
        if origin:
            h["Origin"] = origin
    if host:
        h["Host"] = host
    h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request(method, path, body=json.dumps(body if body is not None else {}) if method == "POST" else None, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


# ---------- tests ----------
def test_levels_per_user(acc):
    p = acc.port
    assert req(p, "/api/me", token=jwt("v@example.com")) == (200, {"email": "v@example.com", "level": "view"})
    assert req(p, "/api/me", token=jwt("A@Example.com"))[1] == {"email": "a@example.com", "level": "admin"}
    assert req(p, "/api/projects", token=jwt("v@example.com"))[0] == 200
    assert req(p, "/api/billed?p=x", token=jwt("v@example.com"))[0] == 403  # operate
    assert req(p, "/api/billed?p=x", token=jwt("o@example.com"))[0] != 403
    body = {"p": "x", "run": "r"}
    assert req(p, "/api/inbox", "POST", jwt("v@example.com"), body=body)[0] == 403
    assert req(p, "/api/inbox", "POST", jwt("o@example.com"), body=body)[0] != 403  # passes the gate (404 unknown project)
    assert req(p, "/api/archive", "POST", jwt("o@example.com"), body=body)[0] == 403
    assert req(p, "/api/archive", "POST", jwt("a@example.com"), body=body)[0] != 403
    assert req(p, "/api/forget_project", "POST", jwt("o@example.com"), body={"p": "x"})[0] == 403
    assert req(p, "/api/forget_project", "POST", jwt("a@example.com"), body={"p": "x"})[0] == 404


def test_super_admin_from_config_and_no_grant(acc):
    assert req(acc.port, "/api/me", token=jwt(ADMIN))[1]["level"] == "admin"
    code, page = req(acc.port, "/", token=jwt("nobody@example.com"))
    assert code == 403 and b"no access to agentdash" in page
    assert req(acc.port, "/api/runs?p=*", token=jwt("nobody@example.com"))[0] == 403


def test_every_request_needs_a_jwt(acc):
    p = acc.port
    for path in ("/", "/api/health", "/api/projects", "/api/stream?p=x&run=r", "/api/me"):
        assert req(p, path)[0] == 401, path
        assert req(p, path, host="127.0.0.1:%d" % p)[0] == 401, path  # a spoofed local Host buys nothing
        assert req(p, path, host="localhost:%d" % p)[0] == 401, path
    assert req(p, "/api/inbox", "POST", body={})[0] == 401
    assert req(p, "/api/health", token=jwt("v@example.com"), host="evil.example")[0] == 200  # Host is irrelevant when the JWT is valid


def test_cookie_fallback(acc):
    assert req(acc.port, "/api/me", cookie="x=1; CF_Authorization=" + jwt("v@example.com"))[0] == 200
    assert req(acc.port, "/api/me", cookie="CF_Authorization=garbage")[0] == 401


def test_lan_token_is_ignored_on_access_listener(acc):
    p = acc.port
    assert req(p, "/api/me", cookie="ac_token=lan-secret")[0] == 401
    assert req(p, "/api/me?token=lan-secret")[0] == 401
    assert req(p, "/api/inbox", "POST", cookie="ac_token=lan-secret", body={})[0] == 401
    # ...and a valid JWT never receives the LAN token as a cookie
    h = http.client.HTTPConnection("127.0.0.1", p, timeout=10)
    h.request("GET", "/", headers={"Cf-Access-Jwt-Assertion": jwt("v@example.com")})
    r = h.getresponse()
    r.read()
    assert r.status == 200 and r.getheader("Set-Cookie") is None


@pytest.mark.parametrize("name,tok", [
    ("expired", lambda: jwt(exp=time.time() - 3600)),
    ("wrong aud", lambda: jwt(aud=["other"])),
    ("wrong iss", lambda: jwt(iss="https://evil.cloudflareaccess.com")),
    ("alg none", lambda: jwt(alg="none")),
    ("HS256", lambda: jwt(alg="HS256")),
    ("unknown kid", lambda: jwt(kid="zzz")),
    ("no email", lambda: jwt(email=None)),
    ("service token type", lambda: jwt(type="service")),
    ("wrong key", lambda: jwt(key=(N2, D2))),
    ("nbf in future", lambda: jwt(nbf=time.time() + 3600)),
])
def test_bad_tokens(acc, name, tok):
    assert req(acc.port, "/api/me", token=tok())[0] == 401, name


def test_tampered_payload_and_truncated_sig(acc):
    h, pl, sig = jwt("v@example.com").split(".")
    evil = b64(json.dumps({"iss": f"https://{TEAM}", "aud": [AUD], "exp": time.time() + 600, "type": "app",
                           "email": ADMIN}).encode())
    assert req(acc.port, "/api/me", token=f"{h}.{evil}.{sig}")[0] == 401
    assert req(acc.port, "/api/me", token=f"{h}.{pl}.{sig[:-4]}")[0] == 401
    assert req(acc.port, "/api/me", token="a.b")[0] == 401


def test_aud_string_and_skew(acc):
    assert req(acc.port, "/api/me", token=jwt("v@example.com", aud=AUD))[0] == 200
    assert req(acc.port, "/api/me", token=jwt("v@example.com", exp=time.time() - 30))[0] == 200  # within 60 s skew
    assert req(acc.port, "/api/me", token=jwt("v@example.com", aud=["x", AUD]))[0] == 200


def test_jwks_refetch_rate_limit_and_rotation(acc):
    p = acc.port
    assert req(p, "/api/me", token=jwt("v@example.com"))[0] == 200
    n0 = acc.state["fetches"]
    assert req(p, "/api/me", token=jwt("v@example.com", kid="k2", key=(N2, D2)))[0] == 401  # unknown, refetch blocked (<60 s)
    assert acc.state["fetches"] == n0
    acc.state["keys"]["k2"] = (N2, E2)
    acc.obj.keys.fetched -= 120  # the 60 s window has passed
    assert req(p, "/api/me", token=jwt("v@example.com", kid="k2", key=(N2, D2)))[0] == 200
    assert acc.state["fetches"] == n0 + 1


def test_post_csrf_rules(acc):
    t, body = jwt("o@example.com"), {"p": "x", "run": "r"}
    assert req(acc.port, "/api/inbox", "POST", t, body=body)[0] != 403
    assert req(acc.port, "/api/inbox", "POST", t, body=body, origin=None)[0] == 403
    assert req(acc.port, "/api/inbox", "POST", t, body=body, origin="https://evil.example")[0] == 403
    assert req(acc.port, "/api/inbox", "POST", t, body=body, origin=ORIGIN + ".evil.com")[0] == 403
    assert req(acc.port, "/api/inbox", "POST", t, body=body, headers={"X-AC": "0"})[0] == 403
    assert req(acc.port, "/api/inbox", "POST", t, body=body, headers={"Content-Type": "text/plain"})[0] == 403


def test_default_deny(acc):
    assert access.required_level("GET", "/api/whatever") == "admin"
    assert access.required_level("POST", "/api/new_thing") == "admin"
    assert access.required_level("DELETE", "/api/projects") == "admin"
    assert req(acc.port, "/api/whatever", token=jwt("o@example.com"))[0] == 403
    assert req(acc.port, "/api/whatever", token=jwt("a@example.com"))[0] in (400, 404)  # admin passes the gate


def test_grants_fail_closed_and_reload(acc):
    p = acc.port
    assert req(p, "/api/me", token=jwt("v@example.com"))[0] == 200
    acc.grants.write_text("{not json")
    import os
    t = time.time() + 5
    os.utime(acc.grants, (t, t))
    assert req(p, "/api/me", token=jwt("v@example.com"))[0] == 403  # invalid file: closed
    assert req(p, "/api/me", token=jwt(ADMIN))[0] == 200  # but the super admin still gets in
    acc.write({"v@example.com": "operate"}, mtime_bump=10)
    assert req(p, "/api/me", token=jwt("v@example.com"))[1]["level"] == "operate"  # picked up on mtime change
    acc.grants.unlink()
    assert req(p, "/api/me", token=jwt("v@example.com"))[0] == 403  # missing: closed
    acc.write({"v@example.com": "root"}, mtime_bump=20)
    assert req(p, "/api/me", token=jwt("v@example.com"))[0] == 403  # unknown level ignored


def test_scrub_for_non_admin(acc, monkeypatch):
    monkeypatch.setattr(hub, "pmjc", lambda: {"url": "http://127.0.0.1:8770", "error": "C:/secret/path", "jobs": []})
    assert req(acc.port, "/api/jobs", token=jwt("v@example.com"))[1] == {"jobs": []}
    assert "url" in req(acc.port, "/api/jobs", token=jwt("a@example.com"))[1]


def test_audit_log(acc):
    req(acc.port, "/api/inbox", "POST", jwt("o@example.com"), body={"p": "x", "run": "r", "text": "y" * 300})
    req(acc.port, "/api/archive", "POST", jwt("o@example.com"), body={"p": "x"})
    req(acc.port, "/api/me")
    recs = [json.loads(l) for l in acc.audit.read_text().splitlines()]
    by = {(r["path"], r["status"]): r for r in recs}
    assert by[("/api/inbox", 400)]["email"] == "o@example.com" and by[("/api/inbox", 400)]["level"] == "operate"
    assert by[("/api/inbox", 400)]["body"]["text"] == "<long text>"
    assert ("/api/archive", 403) in by and by[("/api/me", 401)]["email"] is None


def test_me_on_main_listener(home, monkeypatch):
    monkeypatch.setattr(dash, "ALL_MODE", True)
    monkeypatch.setattr(dash, "PINNED", None)
    monkeypatch.setattr(dash, "PINNED_ENTRY", None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        monkeypatch.setattr(dash, "TOKEN", None)
        assert req(port, "/api/me") == (200, {"local": True, "level": "admin"})
        monkeypatch.setattr(dash, "TOKEN", "lan-secret")
        assert req(port, "/api/me")[0] == 403
        assert req(port, "/api/me", cookie="ac_token=lan-secret") == (200, {"lan": True, "level": "admin"})
    finally:
        srv.shutdown()


def test_settings_parsing(tmp_path):
    base = {"dash": {"access": {"enabled": True, "team_domain": TEAM, "aud": ["a"], "super_admin": " Boss@X.com "}}}
    s = access.settings(base)
    assert s["port"] == 8766 and s["super_admin"] == "boss@x.com" and s["aud"] == ["a"]
    assert access.settings({}) is None
    assert access.settings({"dash": {"access": {"enabled": True, "team_domain": TEAM}}}) is None  # no aud
    assert access.settings({"dash": {"access": {"enabled": False, "team_domain": TEAM, "aud": ["a"]}}}) is None


def test_resolve_listen_flags_and_config():
    assert dash.resolve_listen(None, None, {}) == ("127.0.0.1", False)
    cfg = {"host": "0.0.0.0", "remote": True}
    assert dash.resolve_listen(None, None, cfg) == ("0.0.0.0", True)
    assert dash.resolve_listen("127.0.0.1", None, cfg) == ("127.0.0.1", True)
    assert dash.resolve_listen(None, False, cfg) == ("0.0.0.0", False)
