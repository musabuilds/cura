# Building a doctor or facility app on Cura

Everything an app needs to let doctors and facilities run their own records: onboarding, rosters,
availability, history. Why it is shaped this way: [ADRs](adr/). Endpoint reference: `GET /docs`.

All writes need a user token (OAuth 2.0 + PKCE, see [api.md](api.md)) with `practitioner:manage`, and
the user must **own** the record — an approved claim.

## 1. Onboarding: connect an account to the record that already exists

```
doctor signs in ─▶ GET /v1/practitioners/match?name=رامي خليل حداد&specialty=cardiology
                     ├─ one is them ─▶ POST /v1/claims {subject_type: practitioner, subject_id, evidence}
                     │                  (moderator checks licence → owns it)
                     └─ none is them ─▶ POST /v1/practitioners {record, claim: {...}}   409 if similar exist
hospital admin ─▶ POST /v1/facilities/{id}/invitations {practitioner_id}  →  {token, url}  (shown once)
doctor opens url, signs in ─▶ POST /v1/invitations/accept {token}  →  owns it, vouched by the hospital
```

Facilities do the same: `GET /v1/facilities/match?name=…&near=lat,lon` → claim the match, or create
(409 with `candidates` when it probably exists; `same_place: true` means Cura's own merge rules agree).

`ambiguous: true` on a doctor candidate means a two-part name like "Ahmad Ali": ask for a workplace or
specialty before trusting the top result.

## 2. Rosters (facility dashboard)

| action | call |
|---|---|
| see everyone, including who left and pending | `GET /v1/facilities/{id}/practitioners?include=all` |
| add an existing doctor | `POST /v1/facilities/{id}/practitioners {practitioner_id, role: {hours, …}}` |
| add a doctor not in Cura | `POST /v1/facilities/{id}/practitioners {practitioner: {name, specialty}, role}` (409 → pick a candidate or `confirm_new`) |
| doctor left | `POST /v1/roles/{role_id}/end {end_date, reason}` — kept, hidden from the public roster |
| doctor came back | add them again: the old role is reactivated, not duplicated |
| edit a doctor's hours / fee at your facility | `PATCH /v1/roles/{role_id}` with `If-Match` |
| confirm a doctor who added your facility | `POST /v1/roles/{role_id}/confirmation {accept: true}` |

A doctor adding a workplace (`POST /v1/roles`) that has a verified admin waits as `pending_facility`;
a facility adding a doctor who has their own account waits as `pending_practitioner`.

Reviews are the patients'. Owners cannot edit them; they can report one:
`POST /v1/reviews/{id}/reports {reason: spam | abusive | conflict_of_interest | personal_data | off_topic}`.

## 3. Availability: what is happening now

The schedule (`hours`) says what normally happens. **Events** say what is actually happening and
override it. Every read resolves both.

```bash
# Off everywhere until Sunday
POST /v1/availability  Idempotency-Key: 6f1c…
{"subject_type": "practitioner", "subject_id": "JO-PRC-000317",
 "state": "unavailable", "reason": "leave", "ends_at": "2026-11-01T08:00:00+03:00", "note": "Back Sunday"}

# …but seeing patients at my own clinic today
{"subject_type": "role", "subject_id": "JO-ROL-0001288", "state": "available",
 "ends_at": "2026-10-27T20:00:00+03:00"}

# Hospital: closed for renovation until further notice, ER diverting
{"subject_type": "facility", "subject_id": "JO-AM-HSP-00042", "state": "closed",
 "reason": "construction", "details": {"er": "diverting"}}

# Back early / set by mistake
POST /v1/availability/{id}/end      POST /v1/availability/{id}/cancel
```

| subject | states | reasons |
|---|---|---|
| `practitioner` (everywhere), `role` (one place) | `available`, `busy`, `unavailable` | leave, conference, training, procedure, on_call, personal, other |
| `facility` | `open`, `limited`, `closed` | holiday, maintenance, construction, power_outage, weather, staffing, capacity, emergency, other |

`GET /v1/reference/availability` returns the lists. There are no medical reasons; notes are public.
Times without an offset are read as Amman time.

**Reading it**

| question | call |
|---|---|
| is Dr X available, and where? | `GET /v1/practitioners/{id}/availability[?at=]` → `available_at`, per-place state |
| what is happening at this hospital, and which doctors are in? | `GET /v1/facilities/{id}/live[?at=]` |
| on the normal record pages | `GET /v1/facilities/{id}` → `live`, `open_now`; `GET /v1/practitioners/{id}` → `roles[].availability` |
| open now, honouring closures | `GET /v1/facilities?open_now=true&near=…` |
| the timeline | `GET /v1/availability?practitioner_id=…&from=&to=` |
| mirror it | `GET /v1/availability/changes?since=` |

Each resolved state says **why**: `source` is `event`, `schedule`, `facility` or `role`, with
`reason`, `note`, `until`. Precedence: role ended → facility closed → newest event on the doctor or
this role → the role's hours → `unknown`. `unknown` is never shown as available.

Staff can report how busy the place is: `POST /v1/facilities/{id}/load {level: 0..1}`. A facility can
also publish its own typical busy times as `busy_hours` (owner-editable).

## 4. History and patterns

Nothing is overwritten.

```
GET /v1/roles/{id}/history                 versions, newest first, with the fields each changed
GET /v1/roles/{id}/history?as_of=2026-07-01T09:00:00Z   the record as it was then
GET /v1/practitioners/{id}/patterns?days=365            absence by month/weekday, hours per season
GET /v1/facilities/{id}/patterns                        closures, schedule history, busy profile
```

Patterns are features with their evidence (`days_observed`, sample counts), not forecasts.

## 5. Letting an AI assistant manage a schedule

The app's assistant should act **as the signed-in doctor** (their OAuth token), so the same ownership
rules apply. The endpoints above are designed to be tool calls:

* closed vocabularies for every enum (`/v1/reference/availability`), problem+json errors that say
  what to fix;
* `Idempotency-Key` on `POST /v1/availability`, so a retried tool call does not create two events;
* `If-Match` on `PATCH`, so the assistant never overwrites a change it did not see;
* resolved reads that explain themselves, so it can answer "why does it say I'm off?".

Expose to the model a small toolset — `set_availability`, `end_availability`, `get_my_availability`,
`update_role_hours`, `search` — rather than the whole OpenAPI document.
