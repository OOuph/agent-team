from team.runner import denial_hint


def test_edit_denial_hint_says_write():
    h = denial_hint({"tool_name": "Edit", "tool_input": {"file_path": "x"}})
    assert "Write" in h and "rewrite the whole file" in h


def test_prompt_goes_through_stdin_not_argv(tmp_path):
    """#147/#149: a long prompt must not hit the OS argv limit; the fake CLI echoes what it read from stdin."""
    import json, stat
    from team.runner import Runner
    fake = tmp_path / "fakeclaude"
    fake.write_text("#!/usr/bin/env python3\nimport sys, json\nargv = sys.argv[1:]\ndata = sys.stdin.read()\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '',"
                    " 'structured_output': {'n': len(data), 'in_argv': any(len(a) > 1000 for a in argv)},"
                    " 'total_cost_usd': 0.0, 'num_turns': 1}))\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    r = Runner({"binary": str(fake)}, {"dev": {"description": "d", "prompt": "p"}})
    big = "x" * 3_000_000                       # well above the 128 KiB single-argument limit on Linux
    res = r.run("dev", big, {"type": "object"}, max_usd=0.1, cwd=tmp_path / "w")
    assert res.ok, res.error
    assert res.structured == {"n": len(big), "in_argv": False}


def test_closes_lines_only_same_repo_issues():
    from team.gitops import closes_lines
    links = ["https://github.com/OOuph/agent-team/issues/149", "https://github.com/OOuph/agent-team/issues/149",
             "https://github.com/other/repo/issues/5", "https://agentteam.formachines.ai/", "https://github.com/OOuph/agent-team/pull/7"]
    assert closes_lines(links, "https://github.com/OOuph/agent-team.git") == ["Closes #149"]
    assert closes_lines(None, "https://github.com/OOuph/agent-team.git") == []


def test_huge_agent_def_does_not_hit_argv_limit(tmp_path):
    """T-0105: a 300 KB role prompt must not go into --agents (Errno 7); it is prepended to the stdin prompt."""
    import json, stat
    from team.runner import Runner
    fake = tmp_path / "fakeclaude"
    fake.write_text("#!/usr/bin/env python3\nimport sys, json\nargv = sys.argv[1:]\ndata = sys.stdin.read()\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '',"
                    " 'structured_output': {'max_arg': max(len(a) for a in argv), 'has_role': data.startswith('# Role') and 'R' * 300000 in data and data.endswith('task')},"
                    " 'total_cost_usd': 0.0, 'num_turns': 1}))\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    r = Runner({"binary": str(fake)}, {"dev": {"description": "d", "prompt": "R" * 300_000}})
    res = r.run("dev", "task", {"type": "object"}, max_usd=0.1, cwd=tmp_path / "w")
    assert res.ok, res.error
    assert res.structured["max_arg"] < 100_000 and res.structured["has_role"]
