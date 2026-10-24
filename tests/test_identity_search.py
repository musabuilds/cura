"""No doubled data: matching before creating, merging what slipped through. And search over it all."""

import json

from cura.api import admin, db
from cura.pipeline import merge as pipeline

DOC, PEDS, HOSP, CLINIC = "JO-PRC-000001", "JO-PRC-000002", "JO-AM-HSP-00001", "JO-AM-CLN-00001"


def test_doctor_finds_themselves_in_either_language(registry):
    for name in ("Dr. Rami Khalil Haddad", "د. رامي خليل حداد", "RAMI KHALEEL HADDAD"):
        top = registry.get("/v1/practitioners/match", params={"name": name}).json()["data"]
        assert top and top[0]["id"] == DOC, name
    scoped = registry.get(
        "/v1/practitioners/match", params={"name": "Rami Haddad", "facility_id": HOSP, "specialty": "cardiology"}
    ).json()["data"][0]
    assert scoped["ambiguous"] is True  # two names: many people
    assert {"same specialty", "works at the same facility"} <= set(scoped["reasons"])


def test_creating_a_facility_that_exists_returns_candidates(registry, actors):
    user = actors.signup("new@example.com")
    record = {
        "type": "hospital",
        "name": {"en": "Amman Hospital"},
        "address": {"governorate": "JO-AM"},
        "location": {"lat": 31.9541, "lon": 35.9109},
    }
    r = registry.post("/v1/facilities", json={"record": record}, headers=user)
    assert r.status_code == 409
    top = r.json()["candidates"][0]
    assert top["id"] == HOSP and top["same_place"] and top["distance_m"] < 100
    # The person insists it is different: it goes to a moderator with the candidates attached.
    forced = registry.post("/v1/facilities", json={"record": record, "confirm_new": True}, headers=user)
    assert forced.status_code == 202
    queued = registry.get("/v1/proposals", headers=actors.moderator).json()["data"][0]
    assert queued["patch"]["_candidates"] == [HOSP]
    found = registry.get(
        "/v1/facilities/match", params={"name": "مستشفى عمان", "type": "hospital", "governorate": "AM"}
    )
    assert found.json()["data"][0]["id"] == HOSP


def test_moderator_merge_moves_everything_and_leaves_a_tombstone(registry, actors, tmp_path, monkeypatch):
    # A duplicate of the clinic, with its own doctor link, a review and an owner.
    mod = actors.moderator
    dup = registry.post(
        "/v1/facilities",
        json={
            "record": {"type": "clinic", "name": {"en": "Dr Haddad Cardiology"}, "address": {"governorate": "JO-AM"}},
            "confirm_new": True,
        },
        headers=mod,
    ).json()["record"]["id"]
    registry.post("/v1/roles", json={"record": {"practitioner_id": PEDS, "facility_id": dup}}, headers=mod)
    registry.post("/v1/roles", json={"record": {"practitioner_id": DOC, "facility_id": dup}}, headers=mod)
    patient = actors.signup("patient@example.com")
    registry.post(f"/v1/facilities/{dup}/reviews", json={"ratings": {"overall": 2}}, headers=patient)

    r = registry.post(f"/v1/facilities/{dup}/merge", json={"into": CLINIC, "reason": "same clinic"}, headers=mod)
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == {"roles": 1, "roles_ended": 1, "claims": 0, "reviews": 1, "events": 0}
    tomb = registry.get(f"/v1/facilities/{dup}").json()
    assert tomb["status"] == "merged" and tomb["replaced_by"] == CLINIC
    survivor = registry.get(f"/v1/facilities/{CLINIC}").json()
    assert "Dr Haddad Cardiology" in survivor["name"]["aliases"] and survivor["rating"]["count"] == 1
    assert PEDS in {
        x["practitioner"]["id"] for x in registry.get(f"/v1/facilities/{CLINIC}/practitioners").json()["data"]
    }
    # Writes to a tombstone are refused with a pointer.
    edit = registry.patch(f"/v1/facilities/{dup}", json={"patch": {"beds": 3}}, headers=mod)
    assert edit.status_code == 409 and CLINIC in edit.json()["detail"]
    assert (
        registry.post(f"/v1/facilities/{dup}/merge", json={"into": CLINIC, "reason": "dup"}, headers=mod).status_code
        == 409
    )

    # The decision is exported for the pipeline, which follows chains.
    monkeypatch.setattr(admin, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(admin, "CURATION_DIR", tmp_path / "curation")
    out = admin.export_changes()
    assert out["merges"] == 1
    decision = json.loads((tmp_path / "curation" / "merges.jsonl").read_text(encoding="utf-8"))
    assert (decision["kind"], decision["from"], decision["into"]) == ("facility", dup, CLINIC)
    staged = [json.loads(x) for x in (tmp_path / "staging" / "cura_api.jsonl").read_text(encoding="utf-8").splitlines()]
    ended = [s for s in staged if s["data"].get("facility_id") == dup]
    assert ended and ended[0]["data"]["role_active"] is False


def test_pipeline_follows_merge_chains(tmp_path, monkeypatch):
    (tmp_path / "merges.jsonl").write_text(
        "\n".join(
            json.dumps(d)
            for d in (
                {"kind": "facility", "from": "A", "into": "B"},
                {"kind": "facility", "from": "B", "into": "C"},
                {"kind": "practitioner", "from": "P1", "into": "P2"},
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pipeline, "CURATION_DIR", tmp_path)
    decisions = pipeline.load_merge_decisions()
    assert decisions["facility"] == {"A": "C", "B": "C"}
    roles = [
        {"id": "R1", "practitioner_id": "P1", "facility_id": "F"},
        {"id": "R2", "practitioner_id": "P2", "facility_id": "F"},
        {"id": "R3", "practitioner_id": "P1", "facility_id": "G"},
    ]
    kept, superseded = pipeline._rekey_roles(roles, decisions["practitioner"], {})
    assert kept[("P2", "F")]["id"] == "R2" and kept[("P2", "G")]["id"] == "R3"
    assert superseded == [roles[0] | {"active": False}]


def test_hybrid_search_after_embedding(registry):
    from cura.search import embedder, index

    with next(db.get_session()) as s:
        first = index.sync(s, embedder.HashingEmbedder())
        assert first["embedded"] == 5
        assert index.sync(s, embedder.HashingEmbedder()) == {"embedded": 0, "unchanged": 0, "removed": 0}
    res = registry.get("/v1/search", params={"q": "قلب حداد"}).json()
    assert res["mode"] == "hybrid" and res["semantic"] is False  # hashing model: spelling, not meaning
    assert res["data"][0]["id"] in (DOC, CLINIC)
    kids = registry.get("/v1/search", params={"q": "children hospital irbid", "kind": "facility"}).json()["data"]
    assert kids[0]["id"] == "JO-IR-HSP-00001" and kids[0]["ranks"]["semantic"] == 1
    filtered = registry.get("/v1/search", params={"q": "hospital", "governorate": "IR"}).json()["data"]
    assert {d["id"] for d in filtered} <= {"JO-IR-HSP-00001"}


def test_search_reembeds_only_what_changed_in_meaning(registry, actors):
    from cura.search import embedder, index

    model = embedder.HashingEmbedder()
    with next(db.get_session()) as s:
        index.sync(s, model)
    admin_h = actors.owner("admin@ammanhospital.jo", "facility", "JO-AM-HSP-00001")
    registry.patch(
        "/v1/facilities/JO-AM-HSP-00001",
        json={"patch": {"hours": {"opening_hours": "Mo-Fr 08:00-20:00"}}},
        headers=admin_h,
    )
    registry.patch("/v1/facilities/JO-IR-HSP-00001", json={"patch": {"beds": 40}}, headers=actors.moderator)
    registry.patch(
        "/v1/facilities/JO-AM-CLN-00001", json={"patch": {"services": ["echocardiography"]}}, headers=actors.moderator
    )
    with next(db.get_session()) as s:
        again = index.sync(s, model)
    assert again == {"embedded": 1, "unchanged": 2, "removed": 0}


def test_search_without_a_model_is_lexical(registry, monkeypatch):
    from cura.api.routes import search
    from cura.api.settings import settings

    monkeypatch.setenv("CURA_EMBED_PROVIDER", "none")
    settings.cache_clear()
    search._embedder.cache_clear()
    res = registry.get("/v1/search", params={"q": "Amman Hospital"}).json()
    assert res["mode"] == "lexical" and res["data"][0]["id"] == HOSP
    assert registry.get("/v1/search", params={"q": "x y", "mode": "semantic"}).status_code == 503


def test_owners_report_reviews_they_cannot_edit(registry, actors):
    patient = actors.signup("patient@example.com")
    review = registry.post(
        f"/v1/facilities/{HOSP}/reviews", json={"ratings": {"overall": 1}, "text": "spam spam"}, headers=patient
    ).json()
    hospital = actors.owner("admin@ammanhospital.jo", "facility", HOSP)
    # There is no way to edit a review that is not yours: no PATCH route, and rating is system-owned.
    assert (
        registry.patch(
            f"/v1/facilities/{HOSP}", json={"patch": {"rating": {"overall": 5}}}, headers=hospital
        ).status_code
        == 422
    )
    rep = registry.post(f"/v1/reviews/{review['id']}/reports", json={"reason": "spam"}, headers=hospital)
    assert rep.status_code == 201
    assert (
        registry.post(f"/v1/reviews/{review['id']}/reports", json={"reason": "spam"}, headers=hospital).status_code
        == 409
    )
    assert (
        registry.post(f"/v1/reviews/{review['id']}/reports", json={"reason": "spam"}, headers=patient).status_code
        == 422
    )
    queue = registry.get("/v1/review-reports", headers=actors.moderator).json()["data"]
    assert queue[0]["review"]["text"] == "spam spam"
    registry.post(f"/v1/review-reports/{rep.json()['id']}/uphold", json={}, headers=actors.moderator)
    assert registry.get(f"/v1/facilities/{HOSP}").json()["rating"]["count"] == 0
