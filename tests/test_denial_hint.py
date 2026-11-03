from team.runner import denial_hint, render_transcript


def test_heredoc_denial_names_allowed_form():
    h = denial_hint({"tool_name": "Bash", "tool_input": {"command": "cat > x <<E\nhi\nE"}})
    assert "Write tool" in h and "team-fetch --json-path" in h


def test_transcript_lists_denials():
    res = {"permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "curl x | jq ."}}]}
    assert "team-fetch --json-path" in render_transcript("marketer", "m", [], res)
