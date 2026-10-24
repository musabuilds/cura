"""Patterns: features computed from history, for people and for models that predict.

Cura keeps every schedule version and every availability event; this module turns them into the
features a forecaster needs — when a doctor is usually away, how their hours shift with the season,
which weekdays a clinic is packed — without a model of its own. It describes the past; prediction is
left to whoever consumes it, with the evidence attached (`days_observed`, sample counts).

Pure functions: lists in, dicts out.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from cura.availability import AMMAN, DAYS, Event

SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring"}
SEASONS |= {6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(minutes: float) -> str:
    m = round(minutes)
    return f"{m // 60:02d}:{m % 60:02d}"


def _days(start: datetime, end: datetime):
    """Each local calendar day an interval touches."""
    day = start.astimezone(AMMAN).date()
    last = (end - timedelta(microseconds=1)).astimezone(AMMAN).date()
    while day <= last:
        yield day
        day += timedelta(days=1)


def _noon_days(start: datetime, end: datetime):
    """Local calendar days whose noon falls inside [start, end): each day belongs to one schedule."""
    for day in _days(start, end):
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=AMMAN)
        if start <= noon < end:
            yield day


def absences(events: list[Event], *, states: set[str], start: datetime, end: datetime) -> dict:
    """Days covered by events in `states`, by month of year, weekday and reason, inside [start, end)."""
    days: set[date] = set()
    reasons: Counter[str] = Counter()
    for e in events:
        if e.state not in states or (e.cancelled_at is not None and e.cancelled_at <= e.starts_at):
            continue
        s = max(e.starts_at, start)
        stop = min(e.ends_at or end, e.cancelled_at or end, end)
        if stop <= s:
            continue
        reasons[e.reason or "unspecified"] += 1
        days.update(_days(s, stop))
    observed = list(_days(start, end))
    per_weekday = Counter(DAYS[d.weekday()] for d in observed)
    by_month: dict[int, int] = defaultdict(int)
    for d in days:
        by_month[d.month] += 1
    away_weekday = Counter(DAYS[d.weekday()] for d in days)
    return {
        "days_observed": len(observed),
        "days": len(days),
        "share": round(len(days) / len(observed), 3) if observed else None,
        "by_month": {m: by_month.get(m, 0) for m in range(1, 13)},
        "by_weekday_share": {
            d: round(away_weekday.get(d, 0) / per_weekday[d], 3) if per_weekday.get(d) else None for d in DAYS
        },
        "events_by_reason": dict(reasons.most_common()),
    }


def _weekly(hours: dict | None) -> dict[str, tuple[int, int]] | None:
    """Earliest opening and latest closing per weekday, in minutes; None when unknown."""
    weekly = (hours or {}).get("weekly")
    if not weekly:
        return None
    out: dict[str, tuple[int, int]] = {}
    for span in weekly:
        o, c = _minutes(span["opens"]), _minutes(span["closes"])
        c = c + 24 * 60 if c <= o else c
        prev = out.get(span["day"])
        out[span["day"]] = (min(o, prev[0]), max(c, prev[1])) if prev else (o, c)
    return out


def schedules(versions: list[tuple[datetime, datetime | None, dict]], *, now: datetime) -> dict:
    """Schedule periods from record versions (valid_from, valid_to, doc), and per-season typical hours.

    Seasonal start/end are weighted by how many days each schedule was in force in that season, so a
    summer timetable used for three summers outweighs one tried for a week.
    """
    periods = []
    weights: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    last_key = None
    for valid_from, valid_to, doc in sorted(versions, key=lambda v: v[0]):
        week = _weekly(doc.get("hours"))
        key = tuple(sorted(week.items())) if week else None
        if key is not None and key == last_key and periods:
            periods[-1]["to"] = valid_to
        elif key is not None:
            periods.append({"from": valid_from, "to": valid_to, "week": week})
        last_key = key
    for p in periods:
        start, stop = p["from"], p["to"] or now
        season_days = Counter(SEASONS[d.month] for d in _noon_days(start, stop)) if stop > start else Counter()
        opens = sum(o for o, _ in p["week"].values()) / len(p["week"])
        closes = sum(c for _, c in p["week"].values()) / len(p["week"])
        hours = sum(c - o for o, c in p["week"].values()) / 60
        p["summary"] = {
            "days_per_week": len(p["week"]),
            "hours_per_week": round(hours, 1),
            "typical_start": _hhmm(opens),
            "typical_end": _hhmm(closes % (24 * 60)),
        }
        for season, n in season_days.items():
            weights[season].append((n, opens, closes))
    seasonal = {}
    for season in ("winter", "spring", "summer", "autumn"):
        rows = weights.get(season)
        if not rows:
            seasonal[season] = None
            continue
        total = sum(n for n, *_ in rows)
        seasonal[season] = {
            "typical_start": _hhmm(sum(n * o for n, o, _ in rows) / total),
            "typical_end": _hhmm((sum(n * c for n, _, c in rows) / total) % (24 * 60)),
            "days_observed": total,
        }
    return {
        "periods": [
            {
                "from": p["from"],
                "to": p["to"],
                "weekly": {d: f"{_hhmm(o)}-{_hhmm(c % (24 * 60))}" for d, (o, c) in sorted(p["week"].items())},
                **p["summary"],
            }
            for p in periods
        ],
        "changes": max(0, len(periods) - 1),
        "by_season": seasonal,
    }


def load_profile(samples: list[tuple[datetime, float]]) -> dict:
    """Mean reported load per weekday and hour (Amman time), with how many samples back each cell."""
    cells: dict[tuple[str, int], list[float]] = defaultdict(list)
    for at, level in samples:
        local = at.astimezone(AMMAN)
        cells[(DAYS[local.weekday()], local.hour)].append(level)
    return {
        "samples": len(samples),
        "cells": [
            {"day": d, "hour": h, "level": round(sum(v) / len(v), 3), "samples": len(v)}
            for (d, h), v in sorted(cells.items(), key=lambda kv: (DAYS.index(kv[0][0]), kv[0][1]))
        ],
    }
