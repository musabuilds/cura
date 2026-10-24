# Cura for the mobile app, and for hospital websites

What the first-party app (`cura-mobile`) calls, how a doctor or facility proves who they are, and how
a hospital website stays in sync with the registry. The contract the app is built against lives in
`cura-mobile/docs/API-CONTRACT.md`; this is the server's side of it. Why it is shaped this way:
[ADR 0005](adr/0005-phone-identity-and-instant-onboarding.md). Interactive reference: `GET /docs`.

## Run it

```powershell
./scripts/serve-demo.ps1                                   # or: uv run cura serve-demo
./scripts/serve-demo.ps1 -PublicUrl https://<your-tunnel>  # what the phone reaches
```

`serve-demo` migrates the database, loads `data/*.jsonl` when the database is behind the dataset,
seeds the demo (`cura demo seed`, idempotent) and serves on `0.0.0.0:8000`, trusting `X-Forwarded-*`
from a local tunnel (cloudflared, ngrok). The JWT secret is generated once into `var/.secret`
(gitignored) unless `CURA_JWT_SECRET` is set. It refuses to run with `CURA_ENVIRONMENT=production`.

| phone        | code     | who                                                                  |
|--------------|----------|----------------------------------------------------------------------|
| `0790000001` | `246810` | Dr. Musab Al-Jabiri (`JO-PRC-999901`), admin of his clinic           |
| `0790000002` | `246810` | Admin of Amman Crest Hospital (`JO-AM-HSP-99901`)                    |
| `0790000003` | `246810` | A new user who owns nothing                                          |

Demo site: `/demo/site/JO-AM-HSP-99901` (works for any facility id). Al-Jabiri Sports Medicine
Clinic is `JO-AM-CLN-99901`; the near-duplicate "Mosab Jabri" is `JO-PRC-999902`; Musab's pending
invitation from a real hospital is role `JO-ROL-9999920`. `cura demo reset` removes every demo record
and everything the three accounts did.

Settings (environment, prefix `CURA_`): `DEMO_PHONES`, `DEMO_OTP` (ignored in production),
`SMS_PROVIDER=console|twilio` + `TWILIO_*`, `EMAIL_PROVIDER=console|smtp` + `SMTP_*`,
`ENCRYPTION_KEY` (Fernet key for webhook secrets; derived from the JWT secret when unset),
`CORS_ORIGINS` (a wildcard is dropped in production), `MAX_BODY_BYTES`, `PUBLIC_BASE_URL`.

## Endpoints added

| | |
|---|---|
| `POST /v1/auth/phone/start` · `POST /v1/auth/phone/verify` | code by SMS → tokens + `user` + `is_new` |
| `POST /v1/auth/refresh` · `POST /v1/auth/logout` | rotating refresh (JSON body); reuse revokes the session |
| `GET /v1/me` · `PATCH /v1/me` · `GET /v1/me/roles` | the account, what it owns, every workplace |
| `GET /v1/onboarding/practitioner/candidates` | phone match first, then names (transliteration-aware) |
| `POST /v1/claims` | instant on a unique phone match; otherwise evidence → moderator; `dispute: true` |
| `POST /v1/practitioners` · `POST /v1/facilities` | 409 with candidates, or `confirm_new` → created, owned, `unverified`, merge case |
| `GET /v1/onboarding/facility/candidates` | facilities you may run, each with its `verification_methods` |
| `POST /v1/facilities/{id}/claims` · `POST /v1/facility-claims/{id}/verify` | prove you run it |
| `GET /v1/facilities/{id}/activity` | the dashboard feed (admins) |
| `POST/GET /v1/facilities/{id}/webhooks` · `DELETE /v1/webhooks/{id}` · `GET /v1/webhooks/{id}/deliveries` | signed push |
| `POST/GET /v1/facilities/{id}/api-keys` · `POST /v1/api-keys/{id}/revoke` | read-only website keys |
| `GET /v1/stream?facility_id=` | Server-Sent Events |
| `GET /v1/merge-cases` · `POST /v1/merge-cases/{id}/dismiss` | moderators |
| `GET /demo/site/{facility_id}` | the hospital website demo |

Changed: `PATCH /v1/practitioners/{id}` accepts `bio`, `photo_url` and `contact` from the owner;
facility and practitioner reads carry `verification` (and `version`); the practitioner read shows the
full contact only to its owner; a 202 edit answers `{status: "proposed", proposal_id}`; `POST /v1/claims`
answers 201 with the claim.

## Proving who you are

The registry was collected before anyone signed up, so it already knows things only the real doctor or
facility controls. Every method uses one of those, and the target always comes from the record:

| method | proof | level |
|---|---|---|
| `phone_match` | your verified phone is on this record and on no other of its kind | `verified`, instant |
| `registered_phone` | a code to the facility's listed phone: SMS to a mobile, voice call to a landline; shown masked, not editable | `verified` |
| `work_email` | a code to an address on the record's own website domain; free mail and shared platforms refused | `verified` |
| `website_token` | `cura-verify=…` at `https://<record host>/.well-known/cura-verification.txt` or TXT `_cura-verification.<domain>` | `verified` |
| `documents` | MoH licence + commercial registration, checked by a moderator | `pending` → `reviewed` |
| `evidence` (doctors) | licence / JMC number, checked by a moderator | `pending` → `reviewed` |
| `created` | you made the record ("none of these is me") | `unverified` |

Why a number on two records proves nothing: role phones are often a clinic's reception line, listed for
every doctor there. Why `unverified` is contained: a created record is live at once (nobody is
blocked), but it cannot list a real doctor on its roster, or a doctor on a real hospital's roster,
without the other side; and an unverified facility cannot send invitations, which hand over a profile.

**Website fetches** cannot be aimed at Cura's own network: https only on port 443, only the record's
host, every resolved address must be public (private, loopback, link-local, CGNAT, reserved, IPv4-mapped
IPv6 refused), the connection is pinned to the checked address (TLS still verifies the hostname),
redirects to another host are refused, 5 s, 4 KB. Webhook deliveries use the same guard on every attempt,
so a DNS change after registration cannot redirect them inward.

## Security notes

* Codes: 6 random digits, HMAC-SHA256 with a server key over (challenge id, code), constant-time
  compare, 5 attempts then locked, 5 minutes (10 for facility codes), one live sign-in code per number.
* Limits: sign-in codes 3 per number per 10 minutes and 30 s apart, 20 per IP; verification 30 per IP
  per 10 minutes; facility proofs 6 per account per 10 minutes and 12 per facility per hour; password
  login 20 per IP and 10 per account per 10 minutes. `429` carries `Retry-After`.
* Tokens: HS256 pinned, `iss`, `aud`, `exp`, `iat`, `sub` required; access 15 minutes; refresh tokens
  rotate and a replayed one revokes its whole session.
* Every write checks ownership (an approved claim) or moderator rights; ids of another facility's
  webhooks, keys or claims answer 404. New request bodies reject unknown fields. Bodies over 1 MB are
  refused (413). Errors are problem+json without stack traces. Responses carry `nosniff`,
  `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, and HSTS behind https.
* Private contact details live outside the record (`practitioner_private`), so no read, history entry,
  change feed or dataset export can contain them. The owner's own read is `Cache-Control: private, no-store`.
* `audit_log` records sign-ins, failed and locked codes, refresh reuse, claims granted, records created,
  webhooks and keys created or revoked; it never stores a code, token, secret or full phone number.

## A hospital website on Cura

The facility admin creates a read-only key and (optionally) a webhook in the app, then the website:

```html
<ul id="doctors"></ul>
<script>
const CURA = "https://api.cura.jo", FACILITY = "JO-AM-HSP-99901", KEY = "curafk_…";  // read-only key

async function render() {
  const r = await fetch(`${CURA}/v1/facilities/${FACILITY}?expand=practitioners`, { headers: { "X-API-Key": KEY } });
  const facility = await r.json();
  const list = document.getElementById("doctors");
  list.replaceChildren(...facility.practitioners.map(({ practitioner, role }) => {
    const li = document.createElement("li");                       // textContent: never innerHTML with data
    li.textContent = `${practitioner.name.en} · ${role.hours?.opening_hours ?? "see hours"} · ${role.availability.state}`;
    return li;
  }));
}

render();
// Live: one event per change touching this facility; re-read and redraw.
new EventSource(`${CURA}/v1/stream?facility_id=${FACILITY}`).addEventListener("change", render);
</script>
```

Browsers cannot send headers with `EventSource`; the stream is public and read-only, the same data as
the public reads. A website on another origin needs that origin in `CURA_CORS_ORIGINS` in production. A server-side integration can instead (or also) take webhooks and verify them:

```js
// Express: app.post("/cura", express.raw({ type: "application/json" }), handler)
import crypto from "node:crypto";
function verifyCura(rawBody, header, secret, toleranceS = 300) {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=")));
  if (Math.abs(Date.now() / 1000 - Number(parts.t)) > toleranceS) return false;          // replay
  const mac = crypto.createHmac("sha256", secret).update(`${parts.t}.`).update(rawBody).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(mac), Buffer.from(parts.v1 ?? ""));
}
```

Events: `practitioner.updated`, `role.updated`, `role.ended`, `availability.changed`, `facility.updated`.
Payload: `{id, type, created_at, facility_id, data: {kind, id, version, summary, practitioner_id}}`; fetch
the record for its new state. Deliveries retry after 10 s, 1 min, 5 min, 30 min and 2 h, then fail;
`GET /v1/webhooks/{id}/deliveries` shows each attempt. Polling `GET /v1/changes?since=` still works.

## Deviations from the contract

* `POST /v1/practitioners` / `POST /v1/facilities` behave as the contract says for accounts with a
  verified phone; email/password accounts keep the moderated create (202).
* Roles created by an **unverified** owner wait for the other side (`pending_facility` /
  `pending_practitioner`) even when that side has no owner, and unverified facilities cannot invite.
* Extra fields (superset): `level` on claims, `demo` on summaries and records, `verification_methods`
  and `possible_duplicates` on `POST /v1/facilities`, `instructions` beside `website_token`, `can_dispute`
  on 409s, `role` / `owns` / `claims` / `scopes` kept on `/v1/me`.
* `/v1/auth/refresh` and `/v1/auth/logout` answer 401/204 (never reveal whether a token existed).
