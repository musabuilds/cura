from cura import normalize as nz


def test_phone_e164_jordan_formats():
    assert nz.phone_e164("06 560 8080") == "+96265608080"
    assert nz.phone_e164("+962 79 123 4567") == "+962791234567"
    assert nz.phone_e164("0791234567") == "+962791234567"
    assert nz.phone_e164("٠٧٩١٢٣٤٥٦٧") == "+962791234567"  # Arabic-Indic digits
    assert nz.phone_e164("911") is None
    assert nz.phones("06-5608080 / 079-1234567") == ["+96265608080", "+962791234567"]


def test_arabic_match_key_ignores_spelling_variants_and_boilerplate():
    assert nz.match_key("مستشفى الإسراء") == nz.match_key("مستشفي الاسراء")
    assert nz.match_key("المستشفى الإسلامي") == nz.match_key("مستشفى اسلامي")
    assert nz.match_key("Al Khalidi Hospital & Medical Center") == nz.match_key("Khalidi Medical Center")


def test_person_key_strips_titles():
    assert nz.person_key("Dr. Ahmad Saleh") == nz.person_key("Ahmad Saleh")
    assert nz.person_key("د. أحمد صالح") == nz.person_key("الدكتور احمد صالح")


def test_parse_opening_hours_subset():
    h = nz.parse_opening_hours("Sa-Th 08:00-20:00; Fr off")
    assert h["weekly"][0] == {"day": "mo", "opens": "08:00", "closes": "20:00"}
    assert not any(w["day"] == "fr" for w in h["weekly"])
    assert nz.parse_opening_hours("24/7")["always_open"] is True
    unparsed = nz.parse_opening_hours("Mo-Fr 08:00-16:00; PH off")
    assert "weekly" not in unparsed and unparsed["opening_hours"]


def test_governorate_aliases():
    assert nz.governorate("Amman") == "JO-AM"
    assert nz.governorate("إربد") == "JO-IR"
    assert nz.governorate("Zarka") == "JO-AZ"
    assert nz.governorate("JO-AQ") == "JO-AQ"
    assert nz.governorate("Narnia") is None


def test_website_and_domain():
    assert nz.website("www.Example.jo/") == "https://www.example.jo"
    assert nz.domain("http://www.example.jo/doctors") == "example.jo"
    assert nz.domain("https://facebook.com/somehospital") is None


def test_strip_title_and_caps():
    assert nz.strip_title("DR. ALI ABU AWWAD") == ("Ali Abu Awwad", "Dr.")
    assert nz.strip_title("أ.د. كامل العجلوني") == ("كامل العجلوني", "Prof.")
    assert nz.strip_title("Drew Smith") == ("Drew Smith", None)


def test_arabic_name_chain_and_abd_spacing():
    assert nz.person_key("د. عبد الله وليد") == nz.person_key("عبدالله وليد")


def test_landline_area_code_completed_from_governorate():
    assert nz.phones("5608080", "JO-AM") == ["+96265608080"]
    assert nz.phones("5608080") == []  # without a governorate we do not guess


def test_osm_type_correction_and_placeholders():
    assert nz.type_declared_by_name("صيدليه البيروني") == "pharmacy"
    assert nz.type_declared_by_name("مركز صحي عرجان") == "primary_care_center"
    assert nz.type_declared_by_name("Jordan Hospital") == "hospital"
    assert nz.is_placeholder_name("Health Center") and not nz.is_placeholder_name("Jordan Hospital")
    assert nz.names_non_health("مدرسة زوبيا الثانوية")
