from team.runner import denial_hint, render_transcript


def test_heredoc_denial_names_allowed_form():
    h = denial_hint({"tool_name": "Bash", "tool_input": {"command": "cat > x <<E\nhi\nE"}})
    assert "Write tool" in h and "team-fetch --json-path" in h


def test_transcript_lists_denials():
    res = {"permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "curl x | jq ."}}]}
    assert "team-fetch --json-path" in render_transcript("marketer", "m", [], res)


def test_team_fetch_angle_bracket_denial_explains():
    h = denial_hint({"tool_name": "Bash", "tool_input": {"command": "team-fetch http://x --grep '<item' --around 0"}})
    assert "--around" in h and "redirect" in h
