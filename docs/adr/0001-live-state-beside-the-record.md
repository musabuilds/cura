# ADR 0001 — Live state lives beside the record, not inside it

**Status:** accepted · 2026-10-24

## Context

Doctors and facilities now keep Cura current themselves. A doctor goes on leave for a week, works at
their own clinic but not the hospital on Thursday, runs late; a hospital closes for a storm, or for
six months of construction, regardless of its published hours. These changes are frequent, short-lived
and must apply instantly — "I'm off today" is useless after a moderator approves it tomorrow.

The canonical record (`Facility`, `Practitioner`, `Role`) is versioned, merged nightly into the open
dataset and joined on by other apps. Writing "busy until 14:00" into it would mean a new version, a
dataset diff and a re-embed for every status toggle, and `status: temporarily_closed` is an identity
field that sits behind review.

## Decision

Two layers:

1. **Schedule** — `hours` on the facility and on each role. Part of the record, versioned, reviewed by
   the same policy as before (owners apply directly).
2. **Events** — `availability_events`: a subject (`practitioner` = everywhere, `role` = one doctor at
   one place, `facility`), a state, an optional reason and public note, and a `[starts_at, ends_at)`
   window (`ends_at` empty = until changed). Owners write them; they apply immediately.

Every read resolves the two into what is true at a moment (`cura.availability`):

```
role ended ─▶ facility closed (status or event) ─▶ newest event on the doctor or this role
          ─▶ the role's weekly hours ─▶ unknown (never guessed as available)
```

"Newest event wins" between the doctor-wide and per-place layers is deliberate: it is what a person
toggling switches expects ("off everywhere" then "but on at my clinic").

## Consequences

* Availability changes never touch the dataset, its history or the semantic index.
* Events are append-only: ending early sets `ends_at`, withdrawing sets `cancelled_at`. Absence
  patterns stay learnable (ADR 0002).
* `open_now` and every availability field are computed, not stored; apps cannot read stale state.
* Reasons are a closed vocabulary with no medical causes. Notes are public.
* Writes carry an `Idempotency-Key`, because the main writers will be phone apps and AI assistants
  that retry.
