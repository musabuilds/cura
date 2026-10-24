from cura.agent.runner import grounded


def test_grounded_keeps_only_values_with_verbatim_evidence():
    text = "Welcome to Alpha Hospital. Emergency 24/7. Call us: 06 500 0000. Departments: Cardiology, Pediatrics."
    raw = {
        "phones": ["06 500 0000"],
        "phones_evidence": "Call us: 06 500 0000",
        "emergency": True,
        "emergency_evidence": "Emergency 24/7",
        "beds": 400,
        "beds_evidence": "400 beds",  # hallucinated: not in the page
        "insurance_accepted": ["MedNet"],  # no evidence at all
    }
    assert grounded(raw, text) == {"phones": ["06 500 0000"], "emergency": True}


def test_grounded_tolerates_arabic_spelling_and_whitespace():
    text = "مستشفى ألفا   يعمل قسم الطوارئ على مدار الساعة"
    raw = {"emergency": True, "emergency_evidence": "يعمل قسم الطوارئ  على مدار الساعة"}
    assert grounded(raw, text) == {"emergency": True}
