from datetime import UTC, datetime

import pytest

from cura import geo
from cura.pipeline import merge

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def rec(source, ref, **data):
    return {
        "kind": "facility",
        "source": source,
        "source_ref": ref,
        "retrieved_at": "2026-10-24T00:00:00Z",
        "data": data,
    }


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setattr(merge, "DATA_DIR", tmp_path)
    return tmp_path


def test_reverse_geocode():
    assert geo.governorate_at(31.9539, 35.9106) == "JO-AM"  # downtown Amman
    assert geo.governorate_at(32.5556, 35.85) == "JO-IR"  # Irbid
    assert geo.governorate_at(29.53, 35.0) == "JO-AQ"  # Aqaba


def test_same_hospital_from_two_sources_merges_and_fuses():
    staged = [
        rec("osm", "way/1", name="Jordan Hospital", type="hospital", lat=31.9686, lon=35.8984, phones=["065608080"]),
        rec(
            "jordanhospital_doctors",
            None,
            name_en="Jordan Hospital",
            name_ar="المستشفى الأردني",
            type="hospital",
            ownership="private",
            governorate="Amman",
            website="https://www.jordan-hospital.com",
            beds=300,
            insurance_accepted=["MedNet"],
        ),
    ]
    facilities, _ = merge.merge_facilities(staged, {}, NOW)
    assert len(facilities) == 1
    f = facilities[0]
    assert f["id"] == "JO-AM-HSP-00001"
    assert f["name"] == {"en": "Jordan Hospital", "ar": "المستشفى الأردني", "aliases": []}
    assert f["ownership"] == "private" and f["beds"] == 300
    assert f["location"]["source"] == "osm" and f["location"]["precision"] == "exact"
    assert f["contact"]["phones"] == ["+96265608080"]
    assert f["quality"]["confidence"] > 0.5


def test_different_places_with_similar_names_stay_apart():
    staged = [
        rec("osm", "node/1", name="Al Shifa Pharmacy", type="pharmacy", lat=31.95, lon=35.91),
        rec("osm", "node/2", name="Al Shifa Pharmacy", type="pharmacy", lat=32.07, lon=36.09),  # Zarqa, ~25 km away
        rec("osm", "node/3", name="Al Shifa Hospital", type="hospital", lat=31.9501, lon=35.9101),
    ]
    facilities, _ = merge.merge_facilities(staged, {}, NOW)
    assert len(facilities) == 3


def test_ids_are_stable_across_rebuilds(isolated_data):
    staged = [rec("osm", "node/9", name="Alpha Clinic", type="clinic", lat=31.95, lon=35.91)]
    first, _ = merge.merge_facilities(staged, {}, NOW)
    merge.write_canonical("facilities", first)
    # A new place is added that sorts *before* the old one; the old one must keep its id.
    staged.insert(0, rec("osm", "node/1", name="Aaa Clinic", type="clinic", lat=31.99, lon=35.95))
    second, _ = merge.merge_facilities(staged, {}, NOW)
    by_ref = {f["identifiers"]["osm"]: f["id"] for f in second}
    assert by_ref["node/9"] == first[0]["id"]
    assert by_ref["node/1"] != first[0]["id"]


def test_unknown_stays_null_not_false():
    facilities, _ = merge.merge_facilities(
        [rec("osm", "n/1", name="X Clinic", type="clinic", lat=31.95, lon=35.91)], {}, NOW
    )
    f = facilities[0]
    assert f["emergency"] is None and f["insurance"] is None and f["contact"]["phones"] is None


def test_practitioner_in_two_hospitals_is_one_person_with_two_roles():
    staged = [
        rec("osm", "w/1", name="Alpha Hospital", type="hospital", lat=31.95, lon=35.91, website="https://alpha.jo"),
        rec("osm", "w/2", name="Beta Hospital", type="hospital", lat=31.99, lon=35.87, website="https://beta.jo"),
    ]
    facilities, ident = merge.merge_facilities(staged, {}, NOW)
    people = [
        {
            "kind": "practitioner",
            "source": "alpha_doctors",
            "data": {
                "name_en": "Dr. Rami Khaled Haddad",
                "specialty": "Cardiology",
                "facility_website": "https://alpha.jo",
            },
        },
        {
            "kind": "practitioner",
            "source": "beta_doctors",
            "data": {
                "name_en": "Rami Khaled Haddad",
                "specialty": "cardiologist",
                "facility_name": "Beta Hospital",
                "consultation_fee": {"amount": 30, "currency": "JOD"},
            },
        },
    ]
    practitioners, roles = merge.merge_practitioners(people, {}, facilities, ident, NOW)
    assert len(practitioners) == 1 and practitioners[0]["specialty"] == "cardiology"
    assert len(roles) == 2
    assert {r["facility_id"] for r in roles} == {f["id"] for f in facilities}


def test_short_common_names_do_not_merge_across_facilities():
    staged = [
        rec("osm", "w/1", name="Alpha Hospital", type="hospital", lat=31.95, lon=35.91, website="https://alpha.jo"),
        rec("osm", "w/2", name="Beta Hospital", type="hospital", lat=31.99, lon=35.87, website="https://beta.jo"),
    ]
    facilities, ident = merge.merge_facilities(staged, {}, NOW)
    people = [
        {
            "kind": "practitioner",
            "source": "alpha_doctors",
            "data": {"name_en": "Ahmad Ali", "facility_website": "https://alpha.jo"},
        },
        {
            "kind": "practitioner",
            "source": "beta_doctors",
            "data": {"name_en": "Ahmad Ali", "facility_website": "https://beta.jo"},
        },
    ]
    practitioners, _ = merge.merge_practitioners(people, {}, facilities, ident, NOW)
    assert len(practitioners) == 2
