from prompt_clarifier import normalize_questions


def test_normalize_questions_caps_dedupes_and_coerces_answered():
    result = normalize_questions({"questions": [
        {"id": "Target App", "question": "  Which app should I use?  ", "answered": 0},
        {"id": "target_app", "question": "Duplicate", "answered": False},
        {"id": "scope", "question": "Which files?", "answered": True},
        {"question": "Where should I save it?", "answered": 1},
        {"id": "fourth", "question": "This must be dropped", "answered": False},
    ]})

    assert result == [
        {"id": "target_app", "question": "Which app should I use?", "answered": False},
        {"id": "scope", "question": "Which files?", "answered": True},
        {"id": "where_should_i_save_it", "question": "Where should I save it?", "answered": True},
    ]


def test_normalize_questions_ignores_invalid_rows():
    assert normalize_questions({"questions": [None, {}, {"question": "   "}]}) == []
