import pytest

from job_matcher.gate import Rules, check, check_extracted, required_years

R = Rules()


@pytest.mark.parametrize("text, years", [
    ("You have 3+ years of professional experience in Python", 3),
    ("2-4 years experience with React", 2),
    ("minimum 3 lata doświadczenia komercyjnego", 3),
    ("Doświadczenie min. 2 lata w pracy z SQL", 2),
    ("nearly 60 years of expertise in consulting", None),  # company age, not a requirement
    ("Od ponad 15 lat tworzymy oprogramowanie", None),
    ("No experience required", None),
])
def test_required_years(text, years):
    assert required_years(text) == years


@pytest.mark.parametrize("title, ex, passes", [
    # a range hires at its lowest level
    ("Fullstack Developer with Mobile", {"seniority": "mid", "seniority_quote": "Junior / Mid"}, True),
    # "junior" inside a duty is not a range
    ("QA Automation Engineer", {"seniority": "senior", "seniority_quote": "Mentor a junior QA engineer"}, False),
    # mid with at most two years is early-career
    ("C# Developer", {"seniority": "mid", "seniority_quote": "2+ years", "min_years": 2}, True),
    ("Software Engineer II", {"seniority": "mid", "seniority_quote": "Software Engineer II", "min_years": None}, False),
    ("Junior Product Manager", {"seniority": "mid", "seniority_quote": ""}, True),
    ("Backend Engineer", {"seniority": "unknown", "min_years": 5, "years_quote": "5+ years"}, False),
])
def test_extracted_gate(title, ex, passes):
    assert (check_extracted({"title": title}, ex, R) is None) == passes


def test_location_folds_diacritics():
    rules = Rules(cities=["Kraków"])
    assert check({"title": "Intern", "seniority": "intern", "locations": ["Krakow"], "remote": "office"}, rules) is None


def test_regex_gate_title_words():
    assert check({"title": "Senior Python Developer", "seniority": None, "description": ""}, R)
    assert check({"title": "Python Developer", "seniority": None, "description": "3+ years of experience"}, R)
    assert check({"title": "Junior Python Developer", "seniority": "junior",
                  "description": "1-2 years of experience"}, R) is None
