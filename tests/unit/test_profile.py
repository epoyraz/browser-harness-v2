from harness.ops.profile import ApplicantProfile, ProfileValue, load_answer_file


def test_answer_file_preserves_values_choices_and_known_absence(tmp_path):
    path = tmp_path / "required.txt"
    path.write_text("salary=105000\nportfolio=none\nsource_priority=A,B,C\nconsent=yes\n")
    profile = load_answer_file(path)
    assert profile.answer("salary") == "105000"
    assert profile.get("portfolio").known_absent is True
    assert profile.get("source_priority").candidates == ("A", "B", "C")
    assert profile.answer("consent") is True


def test_merge_keeps_provenance_and_later_answers_win():
    cv = ApplicantProfile.from_mapping({"name": "Enes", "salary": None}, source="CV")
    answers = ApplicantProfile({"salary": ProfileValue("105000", "required.txt")})
    merged = cv.merged(answers)
    assert merged.answer("name") == "Enes"
    assert merged.answer("salary") == "105000"
    assert merged.get("salary").source == "required.txt"
