# Internet access + website-managed permissions: security review (Opus subagent, on the plan), 2026-11-10

Proposal: website (fwogg3r.com, Firebase) admin page writes per-service grants to Firestore, with the owner email hard-coded
as super-admin; a sync on SHEILA writes Cloudflare Access policies from it; agentdash published at agentdash.fwogg3r.com
behind Access, verifying the Access JWT itself and enforcing view / operate / admin.

## Verdicts

1. **Website grants: agree, with changes.** The site's Firebase sign-in is email/password only, so a password account
   without MFA becomes the Cloudflare gate: enable TOTP MFA on the owner account (or move it to Google sign-in); never
   enable custom OIDC/SAML providers (they can assert `email_verified`). Validate doc shape in the rules (lowercase doc id,
   service-key allow-list, level enums, `updatedAt == request.time`, `updatedBy == request.auth.uid`). Any XSS on the site
   becomes an infrastructure compromise: strict CSP on the admin page, notifications on every applied change, and
   high-risk grants (ssh, ha, agentdash operate/admin) gated to the owner.
2. **Sync: agree, with changes.** Keep the fixed "tbryant only" policy on every app and add `svc-<id>` beside it, so the
   owner never depends on the sync; only touch `svc-*` policies; refuse Bypass/Everyone/empty includes. Keep last state
   when Firestore is unreachable; skip malformed docs; atomic grants.json. Secrets in local DPAPI files, not on Z:
   (the NAS is published by files.fwogg3r.com). Firestore service account `roles/datastore.viewer` only (org policy may
   block key creation). Cloudflare token: Access Apps and Policies Edit + Organizations Revoke. 2-min polling is fine.
3. **Revocation:** removing a policy entry doesn't end sessions: call `revoke_user` on every removal, keep the agentdash
   app session short, and have agentdash re-read grants on every request.
4. **agentdash: agree, but "local Host = full access" is unsafe** behind a tunnel (`httpHostHeader` rewrites). Use a
   second listener (127.0.0.1:8766) only for the tunnel that always requires the JWT. POSTs: JSON + exact Origin; default
   deny; audit every POST; fail closed when grants are missing; scrub secrets from GETs for non-admins. Stdlib RS256 is
   fine if the full EMSA-PKCS1-v1_5 block is compared (never parsed), alg allow-listed, kid lookup with rate-limited
   refetch, iss/aud/exp/nbf checked; reject service tokens without `email`. Also enable cloudflared "Protect with Access"
   on the ingress.
5. **Chrome changes with approval: agree.** Create the Access app before the route; no device-client auth on this app.

## Outcome

All adopted, except: `--remote` (LAN token) and access mode coexist (the access listener ignores the LAN token), at the
owner's request to view on the phone over the LAN. The owner email is hard-coded in the website's rules at the owner's
request; AgentController keeps it in local config. The Access app session is 6 h (the dashboard offers no 2 h preset).
