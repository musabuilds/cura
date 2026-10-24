# Using the API

Interactive reference: `GET /docs` (Swagger UI) and `GET /openapi.json` on any running instance.

## Read — no sign-up needed

```bash
curl "$CURA/v1/facilities?type=hospital&governorate=AM&emergency=true"
curl "$CURA/v1/facilities?near=31.9539,35.9106&radius_km=3&type=pharmacy&open_now=true"
curl "$CURA/v1/facilities?q=مستشفى الأردن"           # Arabic or English, any spelling
curl "$CURA/v1/facilities?specialty=cardiology&insurer=mednet"
curl "$CURA/v1/facilities/JO-AM-HSP-00042?expand=practitioners"
curl "$CURA/v1/practitioners?specialty=pediatrics&q=khaled"
curl "$CURA/v1/practitioners/JO-PRC-000317"          # includes every facility they work at
curl "$CURA/v1/reference/specialties"                # governorates, facility_types, insurers, holidays …
curl "$CURA/v1/prices?category=consultation"
```

List and record endpoints accept `fields=name,location` to return only what a screen draws.
Lists return `{data, next_cursor, count}`; pass `cursor=<next_cursor>` for the next page (`limit` ≤ 200).
Geo queries (`near=`) return `distance_m` on each item and sort nearest first.

Anonymous callers get 60 requests/minute per IP. Register an app for 600/minute and send
`X-API-Key: cura_…`.

## Keep a local copy in sync

```bash
curl "$CURA/v1/changes?since=2026-10-01T00:00:00Z"
# → {data: [{kind, id, version, updated_at, doc}], next_since}
```

Store `next_since` and poll with it. Availability has its own feed:
`GET /v1/availability/changes?since=`. Every record's past versions: `GET /v1/{collection}/{id}/history`. Or download the full release (`facilities.jsonl`, CSV, GeoJSON,
SQLite) and then poll changes.

## Register an app

```bash
# 1. a developer account
curl -X POST $CURA/v1/auth/register -H 'content-type: application/json' \
     -d '{"email":"dev@example.com","password":"a long passphrase"}'
TOKEN=$(curl -s -X POST $CURA/v1/auth/login -H 'content-type: application/json' \
     -d '{"email":"dev@example.com","password":"a long passphrase"}' | jq -r .access_token)

# 2. the app — api_key (and client_secret for confidential apps) are shown ONCE
curl -X POST $CURA/v1/apps -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
     -d '{"name":"Clinic Finder","redirect_uris":["clinicfinder://auth"]}'
```

## Let your users (doctors, patients) write — OAuth 2.0 + PKCE

Your app never handles Cura passwords.

1. Create a PKCE pair: `verifier` = 43–128 random URL-safe chars, `challenge` = base64url(SHA-256(verifier)).
2. Open in a browser / ASWebAuthenticationSession / Custom Tab:
   ```
   $CURA/oauth/authorize?response_type=code&client_id=cura_app_…&redirect_uri=clinicfinder://auth
       &scope=profile%20proposals:write%20practitioner:manage%20reviews:write
       &state=<random>&code_challenge=<challenge>&code_challenge_method=S256
   ```
3. The user signs in and approves; you receive `clinicfinder://auth?code=…&state=…`. Check `state`.
4. Exchange:
   ```bash
   curl -X POST $CURA/oauth/token -d grant_type=authorization_code -d client_id=cura_app_… \
        -d code=… -d redirect_uri=clinicfinder://auth -d code_verifier=…
   # → {access_token (JWT, 15 min), refresh_token (30 days, rotating), scope}
   ```
5. Call the API with `Authorization: Bearer <access_token>` (plus your `X-API-Key`).
6. Refresh with `grant_type=refresh_token`. Each refresh returns a new refresh token; re-using an old
   one revokes the whole session (theft detection). Sign out with `POST /oauth/revoke`.

Metadata: `GET /.well-known/oauth-authorization-server` (RFC 8414).

| scope | grants |
|---|---|
| `profile` | `GET /v1/me` |
| `reviews:write` | post reviews |
| `proposals:write` | suggest changes / new places |
| `practitioner:manage` | claim records; edit the ones you own directly |

## Doctors updating their own data

```bash
# claim the profile once (a moderator verifies the licence)
curl -X POST $CURA/v1/claims -H "authorization: Bearer $AT" -H 'content-type: application/json' \
  -d '{"subject_type":"practitioner","subject_id":"JO-PRC-000317","evidence":{"license_number":"…"}}'

# after approval: edits to their own schedule/fees apply immediately
curl -X PATCH $CURA/v1/roles/JO-ROL-0001288 -H "authorization: Bearer $AT" -H 'If-Match: "3"' \
  -H 'content-type: application/json' \
  -d '{"patch":{"hours":{"opening_hours":"Su-Th 16:00-20:00"},"consultation_fee":{"amount":25,"currency":"JOD"}}}'
# → 200 {"status":"applied", "version":4, …}

# a doctor joining a new hospital
curl -X POST $CURA/v1/roles -H "authorization: Bearer $AT" -H 'content-type: application/json' \
  -d '{"record":{"practitioner_id":"JO-PRC-000317","facility_id":"JO-AM-HSP-00042"}}'
```

Availability, rosters, invitations and onboarding: **[live.md](live.md)**. Search: **[search.md](search.md)**.

`PATCH` bodies are RFC 7396 merge patches. A `200` means applied; a `202` means queued for a
moderator (`GET /v1/proposals` shows your own). Errors are RFC 9457 `application/problem+json`.

## Reviews

```bash
curl -X POST $CURA/v1/facilities/JO-AM-HSP-00042/reviews -H "authorization: Bearer $AT" \
  -H 'content-type: application/json' \
  -d '{"ratings":{"overall":4,"cleanliness":5,"wait_time":2,"value":3},"text":"…","visit_month":"2026-10"}'
```

Dimensions: `GET /v1/reference/review_dimensions`. Aggregates appear on the record as `rating`.
