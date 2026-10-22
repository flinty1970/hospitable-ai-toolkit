from hosting.worker import needs_safety_review

BENIGN = ["We enjoyed the fire, was lovely", "Can I smoke outside?", "Where is the carbon monoxide detector?"]
REVIEW = ["the smoke alarm is going off", "I smell smoke", "there is a fire", "the boiler has a fault", "Can we extend?"]


def test_benign_wording_not_reviewed():
    assert not any(needs_safety_review(m) for m in BENIGN)


def test_hazards_and_requests_still_reviewed():
    assert all(needs_safety_review(m) for m in REVIEW)
