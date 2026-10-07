from rag.pages import build_page_index, locate_pages, shingles

P1 = "Annual leave entitlement is twenty one working days per leave year for all permanent employees."
P2 = "Maternity leave is granted for three months with full pay to every eligible female employee."
P3 = "Overtime worked on a Sunday is paid at double the normal hourly rate after approval."


def test_locates_single_page():
    idx = build_page_index([P1, P2, P3])
    assert locate_pages(P2, idx) == (2, 2)


def test_tolerates_light_rewriting():
    idx = build_page_index([P1, P2, P3])
    rewritten = "Maternity leave is granted for three months with full pay to every eligible employee."
    assert locate_pages(rewritten, idx) == (2, 2)


def test_multi_page_span():
    idx = build_page_index([P1, P2, P3])
    assert locate_pages(P1 + " " + P2, idx) == (1, 2)


def test_unrelated_text_returns_none():
    assert locate_pages("quantum chromodynamics gluon confinement lattice", build_page_index([P1, P2])) is None


def test_empty_inputs():
    assert locate_pages("", build_page_index([P1])) is None
    assert locate_pages(P1, []) is None
    assert shingles("") == set()


def test_text_shorter_than_ngram():
    assert shingles("two words") == {"two words"}
