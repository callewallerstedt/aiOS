from prompt_clarifier import normalize_questions, question_limit


def test_normalize_questions_caps_at_ten_dedupes_and_coerces_answered():
    result = normalize_questions({"questions": [
        {"id": "Target App", "question": "  Which app should I use?  ", "answered": 0},
        {"id": "target_app", "question": "Duplicate", "answered": False},
        {"id": "scope", "question": "Which files?", "answered": True},
        {"question": "Where should I save it?", "answered": 1},
        *({"id": f"question_{index}", "question": f"Question {index}?", "answered": False}
          for index in range(4, 13)),
    ]})

    assert result[:3] == [
        {"id": "target_app", "question": "Which app should I use?", "answered": False},
        {"id": "scope", "question": "Which files?", "answered": True},
        {"id": "where_should_i_save_it", "question": "Where should I save it?", "answered": True},
    ]
    assert len(result) == 10
    assert result[-1]["id"] == "question_10"


def test_normalize_questions_ignores_invalid_rows():
    assert normalize_questions({"questions": [None, {}, {"question": "   "}]}) == []


def test_question_limit_grows_with_draft_and_prior_questions():
    assert question_limit("Rename the file") == 2
    assert question_limit("Do this. Then do that. Finally save a copy.") == 4
    assert question_limit("Step. " * 8) == 10
    assert question_limit("Rename the file", [{"id": str(index)} for index in range(5)]) == 7
    assert question_limit("Large request " * 100) == 10
