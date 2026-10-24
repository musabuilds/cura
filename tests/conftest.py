"""Shared fixtures: a throwaway registry with two hospitals, a private clinic and two doctors.

Amman Hospital (has a verified admin in most tests) and the doctor's own clinic are the scenario the
live layer exists for: one doctor, two workplaces, available at one and not the other.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from cura.api import db, store
from cura.api.settings import settings

NOW = "2026-10-24T00:00:00Z"
DAYTIME = [{"day": d, "opens": "08:00", "closes": "16:00"} for d in ("su", "mo", "tu", "we", "th")]
HOSPITAL = {
    "id": "JO-AM-HSP-00001",
    "type": "hospital",
    "name": {"en": "Amman Hospital", "ar": "مستشفى عمان", "aliases": []},
    "address": {"governorate": "JO-AM", "city": "Amman"},
    "location": {"lat": 31.9539, "lon": 35.9106, "precision": "exact"},
    "contact": {"phones": ["+96265000001"], "website": "https://ammanhospital.jo"},
    "hours": {"opening_hours": "24/7", "always_open": True},
    "emergency": True,
    "specialties": ["cardiology", "pediatrics"],
    "insurance": [{"insurer": "mednet"}],
    "quality": {"completeness": 0.5, "confidence": 0.5},
    "created_at": NOW,
    "updated_at": NOW,
}
CLINIC = {
    "id": "JO-AM-CLN-00001",
    "type": "clinic",
    "name": {"en": "Haddad Heart Clinic", "ar": "عيادة حداد للقلب"},
    "address": {"governorate": "JO-AM"},
    "location": {"lat": 31.97, "lon": 35.87, "precision": "exact"},
    "hours": {"weekly": DAYTIME},
    "specialties": ["cardiology"],
    "quality": {"completeness": 0.3, "confidence": 0.5},
    "created_at": NOW,
    "updated_at": NOW,
}
IRBID = {
    "id": "JO-IR-HSP-00001",
    "type": "hospital",
    "name": {"en": "Irbid Children's Hospital", "ar": "مستشفى إربد للأطفال"},
    "address": {"governorate": "JO-IR"},
    "location": {"lat": 32.5556, "lon": 35.85, "precision": "exact"},
    "specialties": ["pediatrics"],
    "quality": {"completeness": 0.3, "confidence": 0.5},
    "created_at": NOW,
    "updated_at": NOW,
}
DOCTOR = {
    "id": "JO-PRC-000001",
    "name": {"en": "Dr. Rami Khalil Haddad", "ar": "رامي خليل حداد"},
    "specialty": "cardiology",
    "quality": {"completeness": 0.3, "confidence": 0.5},
    "created_at": NOW,
    "updated_at": NOW,
}
PEDIATRICIAN = {
    "id": "JO-PRC-000002",
    "name": {"en": "Dr. Lina Samir Odeh", "ar": "لينا سمير عودة"},
    "specialty": "pediatrics",
    "quality": {"completeness": 0.3, "confidence": 0.5},
    "created_at": NOW,
    "updated_at": NOW,
}
ROLE_HOSPITAL = {
    "id": "JO-ROL-0000001",
    "practitioner_id": "JO-PRC-000001",
    "facility_id": "JO-AM-HSP-00001",
    "hours": {"weekly": DAYTIME},
    "updated_at": NOW,
}
ROLE_CLINIC = {
    "id": "JO-ROL-0000002",
    "practitioner_id": "JO-PRC-000001",
    "facility_id": "JO-AM-CLN-00001",
    "hours": {"weekly": DAYTIME},
    "updated_at": NOW,
}
ROLE_PEDS = {
    "id": "JO-ROL-0000003",
    "practitioner_id": "JO-PRC-000002",
    "facility_id": "JO-AM-HSP-00001",
    "updated_at": NOW,
}
SEED = (
    ("facility", HOSPITAL),
    ("facility", CLINIC),
    ("facility", IRBID),
    ("practitioner", DOCTOR),
    ("practitioner", PEDIATRICIAN),
    ("role", ROLE_HOSPITAL),
    ("role", ROLE_CLINIC),
    ("role", ROLE_PEDS),
)


# Set to run the suite on Postgres + pgvector instead of SQLite (CI does both). The schema is dropped
# and re-migrated for every test.
POSTGRES = os.environ.get("CURA_TEST_POSTGRES_URL")


def _fresh_postgres(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    if POSTGRES:
        _fresh_postgres(POSTGRES)
    monkeypatch.setenv("CURA_DATABASE_URL", POSTGRES or f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    monkeypatch.setenv("CURA_EMBED_PROVIDER", "hashing")
    monkeypatch.setenv("CURA_BACKGROUND_WORKERS", "0")  # change fan-out and webhooks run inline
    monkeypatch.setenv("CURA_DEMO_PHONES", "0790000001,0790000002,0790000003")
    settings.cache_clear()
    _reset_limits()
    db.reset_engine()
    db.init()
    with next(db.get_session()) as s:
        for kind, doc in SEED:
            store.put(s, kind, doc, dirty=False, origin="seed")
        s.commit()
    from cura.api.main import app
    from cura.api.routes import search

    search._embedder.cache_clear()
    with TestClient(app) as c:
        yield c
    db.reset_engine()
    settings.cache_clear()
    search._embedder.cache_clear()


def _reset_limits() -> None:
    from cura.api import deps, guard, messaging

    guard.windows.reset()
    deps.limiter._buckets.clear()
    messaging.sms.cache_clear()
    messaging.email.cache_clear()
    messaging.console().outbox.clear()


class Actors:
    """Accounts in the scenario, created on first use."""

    def __init__(self, client: TestClient):
        self.client = client
        self._mod = None

    def signup(self, email: str, password: str = "correct horse battery") -> dict:
        r = self.client.post("/v1/auth/register", json={"email": email, "password": password})
        assert r.status_code in (201, 409), r.text
        tok = self.client.post("/v1/auth/login", json={"email": email, "password": password}).json()
        return {"Authorization": f"Bearer {tok['access_token']}"}

    @property
    def moderator(self) -> dict:
        if self._mod is None:
            from cura.api import admin

            admin.create_admin("mod@cura.jo", "moderator password!")
            tok = self.client.post(
                "/v1/auth/login", json={"email": "mod@cura.jo", "password": "moderator password!"}
            ).json()
            self._mod = {"Authorization": f"Bearer {tok['access_token']}"}
        return self._mod

    def owner(self, email: str, subject_type: str, subject_id: str) -> dict:
        """An account with an approved claim on the record."""
        headers = self.signup(email)
        claim = self.client.post(
            "/v1/claims",
            json={"subject_type": subject_type, "subject_id": subject_id, "evidence": {"license_number": "x"}},
            headers=headers,
        )
        assert claim.status_code == 201, claim.text
        decided = self.client.post(f"/v1/claims/{claim.json()['id']}/approve", json={}, headers=self.moderator)
        assert decided.json()["status"] == "approved"
        return headers


@pytest.fixture()
def actors(registry) -> Actors:
    return Actors(registry)
