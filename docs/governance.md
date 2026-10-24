# Governance: who can change what

Cura grows from three directions — scripted sources, the refresh agent, and people using apps built on
the API. All three are untrusted until proven otherwise, and every change is attributable.

## Accounts and roles

| who | how they get there | can |
|---|---|---|
| anyone | — | read everything |
| user | registers (`/v1/auth/register`) or signs in through an app | post reviews; **propose** changes |
| verified practitioner | user + approved **claim** on a practitioner record | edit own profile's non-identity fields and own roles (schedule, fee, insurance, clinic phone) **directly** |
| facility admin | user + approved claim on a facility | edit the facility's operational fields directly; its roles |
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

## Reviews

One review per user per facility/practitioner (re-posting replaces it), 1–5 on `overall` plus
optional dimensions (cleanliness, wait time, staff, communication, value, accessibility). Reviewer
identity is never exposed. Visit dates are stored to the month only. Moderators can hide reviews.

## Privacy

Cura stores professional facts about practitioners as published by them or their employer — Jordan's
Personal Data Protection Law (No. 24 of 2023) is the frame. Practitioners can claim their record to
correct it, and moderators can withdraw a record on request.
