import pytest
from pydantic import ValidationError

from app.agent.schemas import Evidence, Investigation, Review


def investigation(**changes):
    return Investigation(
        **{
            "ata_chapter": "32",
            "finding": "합성 조사",
            "severity": "MEDIUM",
            "evidence": [Evidence(source="SDR", ref_id="SDR-TEST", excerpt="original", why="비교")],
            "proposed_department": "HYD",
            "rationale": "합성 근거",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"ata_chapter": "3200"},
        {"ata_chapter": "３２"},
        {"evidence": []},
        {"finding": " "},
        {"severity": "UNKNOWN"},
        {"assign_now": True},
    ],
)
def test_invalid_investigation_is_not_repaired_by_inventing_values(changes):
    with pytest.raises(ValidationError):
        investigation(**changes)


@pytest.mark.parametrize(
    "decision,department,question",
    [
        ("PASS", None, None),
        ("SUPPLEMENT", None, None),
        ("SUPPLEMENT", None, " "),
    ],
)
def test_conditional_review_fields(decision, department, question):
    with pytest.raises(ValidationError):
        Review(
            decision=decision, department=department, comments="이유", supplement_request=question
        )


def test_human_does_not_require_an_invented_department():
    result = Review(
        decision="HUMAN", department=None, comments="현장 확인 필요", supplement_request=None
    )
    assert result.department is None


def test_excerpt_maximum():
    with pytest.raises(ValidationError):
        Evidence(source="SDR", ref_id="SDR-TEST", excerpt="a" * 301, why="비교")


def test_investigation_review_and_evidence_fields():
    assert set(Investigation.model_fields) == {
        "ata_chapter",
        "finding",
        "severity",
        "evidence",
        "proposed_department",
        "rationale",
    }
    assert set(Review.model_fields) == {"decision", "department", "comments", "supplement_request"}
