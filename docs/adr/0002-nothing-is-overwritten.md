# ADR 0002 — Nothing is overwritten

**Status:** accepted · 2026-10-24

## Context

Cura should support prediction: "this doctor leaves early in summer", "this clinic closes the last
week of Ramadan", "this ER is packed on Sunday mornings". That needs the past, and the API used to
replace a record's document on every edit.

## Decision

* **Record versions.** Every applied write appends the full new document to `record_versions` with
  a validity interval `[valid_from, valid_to)` and the `Change` that produced it. The row in
  `facilities` / `practitioners` / `roles` is the present; the history table is every past.
  Exposed as `GET /v1/{collection}/{id}/history` (with changed fields per version) and `?as_of=`.
* **Events are append-only** (ADR 0001); **load samples** are append-only.
* **Leaving is not deleting.** A doctor who leaves a facility ends the role (`active: false`,
  `period.end`); a duplicate is merged into a tombstone (`status: merged`, `replaced_by`). Reviews
  hidden by moderation keep their row.
* **Patterns, not forecasts.** `GET /v1/{practitioners|facilities}/{id}/patterns` derives the
  features a forecaster needs (absence by month and weekday, schedule changes, typical hours per
  season, busy profile) with the evidence size attached. Cura publishes the evidence; models are
  the consumers' business.

## Consequences

* Storage grows with edits, not with reads; versions are a few KB of JSONB each.
* A record that predates history gets its pre-edit version backfilled on its first change.
* Privacy: the history is of professional facts the doctor or facility published. Per-doctor
  availability timelines are public at the same granularity as the live state (no medical reasons).
  If that ever needs narrowing, it is a read-side filter on `GET /v1/availability` — the data model
  does not change.
