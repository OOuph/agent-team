from team.runner import denial_hint


def test_edit_denial_hint_says_write():
    h = denial_hint({"tool_name": "Edit", "tool_input": {"file_path": "x"}})
    assert "Write" in h and "rewrite the whole file" in h
