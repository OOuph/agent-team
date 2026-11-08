from team import prfacts

OUT = "........\n=== 2 failed, 118 passed, 1 skipped in 4.20s ==="


def test_counts():
    assert prfacts.parse_test_counts(OUT) == {"passed": 118, "failed": 2, "error": 0, "skipped": 1}
    assert prfacts.parse_test_counts("nothing") is None


def test_block_lists_diff_and_counts_not_claims():
    md, bad = prfacts.build(" a.py | 2 +-", ["team/a.py", "SCHEME.md"], OUT,
                            {"summary": "Edited team/a.py and SCHEME.md", "changes": ["team/a.py"]})
    assert "## Verified by dispatcher" in md and "`team/a.py`" in md and "118 passed, 2 failed" in md
    assert "**Canary:** pending" in md and "MISMATCH" not in md and bad == []


def test_mismatch_when_summary_names_file_not_in_diff():
    md, bad = prfacts.build("", ["team/a.py"], OUT, {"summary": "Changed team/a.py and team/ghost.py (see https://x.io/z/other.py)", "changes": []})
    assert bad == ["team/ghost.py"]
    assert "MISMATCH" in md and "`team/ghost.py`" in md


def test_canary_result_replaces_pending():
    md, _ = prfacts.build("", [], None, {})
    assert "not run" in md
    assert "**Canary:** passed: ok" in prfacts.with_canary(md, "passed: ok")
