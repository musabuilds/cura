# ADR 0004 — Linking accounts to existing records, and never creating a second copy

**Status:** accepted · 2026-10-24

## Context

The data was collected before anyone signed up. A doctor opening an app must find *their* record
among 15,000, not create a new one; a hospital must not register itself twice; a doctor can be
onboarded by the hospital or by themselves.

## Decision

**Find before create.**
`GET /v1/practitioners/match` and `GET /v1/facilities/match` return ranked candidates with reasons.
Facilities use the pipeline's own entity-resolution rules (`pipeline.merge.is_match`), so the API and
the nightly merge agree on "the same place". People are matched on Arabic-folded, title-free names,
including dropped middle names in Arabic name chains, then ranked by specialty and shared workplace;
two-part names are flagged `ambiguous`.

**Create is duplicate-checked.** `POST /v1/facilities` / `practitioners` returns **409** with the
candidates when one exists. Overriding (`confirm_new: true`) always goes to a moderator, with the
candidates attached. A branch of a chain is a separate place with `parent` set.

**Two onboarding paths, both ending in an approved claim:**

| path | proof | `claims.method` |
|---|---|---|
| self | licence / JMA number / work email, checked by a moderator | `evidence` |
| facility | a one-time, 14-day invitation link from a verified facility admin, for a doctor on its roster | `facility_invite` |

The invitation token is stored hashed and shown once; Cura sends no email — the facility's app
delivers it. One account owns at most one doctor profile.

**Rosters are two-sided.** A role added by one side, when the other side has a verified owner, waits
for them (`confirmation: pending_facility | pending_practitioner`) and is hidden from public rosters
until confirmed. Either side can end a role.

**Duplicates that slip through are merged, not deleted.** A moderator merge tombstones the duplicate
and moves roles, claims, reviews and ongoing availability to the survivor. The decision is exported
to `sources/curation/merges.jsonl`, which the pipeline replays, so the next build makes the same merge.

## Consequences

* The matcher is advisory: people and moderators decide, the API never merges on its own.
* Claims remain the only source of edit rights; invitations are just a second way to approve one.
