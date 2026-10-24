# Governance: who can change what

Cura grows from two directions — scripted sources and the people who run the places: doctors and
facility admins using apps built on the API. Both are untrusted until proven otherwise, and every change is attributable.

## Accounts and roles

| who | how they get there | can |
|---|---|---|
| anyone | — | read everything |
| user | registers (`/v1/auth/register`) or signs in through an app | post reviews; **propose** changes |
| verified practitioner | user + approved **claim** on a practitioner record (moderator-checked evidence, or a facility's invitation) | edit own profile's non-identity fields and own roles (schedule, fee, insurance, clinic phone) **directly**; set own availability; add and end own workplaces |
| facility admin | user + approved claim on a facility | edit the facility's operational fields directly; set closures, limited service and load; run the roster (add, confirm, end roles; edit doctors' hours there); invite doctors to own their profiles; report reviews |
| moderator / admin | assigned by an admin | approve/reject proposals and claims; edit anything |

### Claims

A claim asks "I am this doctor" or "I run this facility" and must carry evidence a moderator can
check: Jordan Medical Association membership or MoH licence number, a work email on the facility's
domain, an official letter. Approval sets `license_verified: true` on a practitioner.

## The write policy (`src/cura/api/policy.py`)

| field group | owner | others |
|---|---|---|
| system — `id`, `identifiers`, `sources`, `quality`, `rating`, timestamps, `license_verified` | ✗ | ✗ |
| identity — name, type, ownership, address, location, status, specialty, grade, gender, accreditations | proposal | proposal |
| operational — contact, hours, services, departments, equipment, insurance, prices, amenities, fees, schedule | **applies** | proposal |

Identity fields go through review even for owners: other apps join on them, and a typo there
breaks every consumer.

Every write is a `Change` (RFC 7396 merge patch, author, app, reason, evidence URL, base version).
Applied changes bump the record's `version` (exposed as `ETag`; send `If-Match` to avoid lost
updates) and appear in `GET /v1/changes`.

## Back to the open dataset

```
cura db export-changes  →  sources/staging/cura_api.jsonl   (only the edited fields)
cura merge              →  data/*.jsonl                     (cura_api has the highest trust prior)
cura db seed            →  database                         (rows edited since the export are kept)
```

So a doctor fixing their hours in a third-party app reaches the published ODbL dataset in the next
release, with provenance.

## Live state

Availability events and load samples are not proposals: only owners can write them, and they apply
at once. They are append-only and attributed (author, app). See [live.md](live.md).

## Rosters and duplicates

A role added by one side waits for the other side's verified owner, when there is one. A role ends,
it is never deleted. Creating a facility or practitioner that probably exists returns the candidates
(409); overriding goes to a moderator. Moderators merge duplicates into a tombstone; the decision is
written to `sources/curation/merges.jsonl` so the pipeline repeats it. See
[ADR 0004](adr/0004-identity-onboarding-and-duplicates.md).

## Reviews

One review per user per facility/practitioner (re-posting replaces it), 1–5 on `overall` plus
optional dimensions (cleanliness, wait time, staff, communication, value, accessibility). Reviewer
identity is never exposed. Visit dates are stored to the month only. Doctors and facilities cannot
edit or remove reviews about them; anyone can report one (`POST /v1/reviews/{id}/reports`), and a
moderator upholding a report hides it.

## Privacy

Cura stores professional facts about practitioners as published by them or their employer — Jordan's
Personal Data Protection Law (No. 24 of 2023) is the frame. Practitioners can claim their record to
correct it, and moderators can withdraw a record on request.
