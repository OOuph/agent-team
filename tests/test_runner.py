from team.runner import denial_hint


def test_edit_denial_hint_says_write():
    h = denial_hint({"tool_name": "Edit", "tool_input": {"file_path": "x"}})
    assert "Write" in h and "rewrite the whole file" in h


def test_big_prompt_goes_through_stdin_not_argv(tmp_path):
    import json, stat
    from team.runner import Runner
    fake = tmp_path / "fakeclaude"
    fake.write_text('#!/usr/bin/env python3\nimport sys, json\n'
                    'data = sys.stdin.read()\n'
                    'argv_len = sum(len(a) for a in sys.argv)\n'
                    'print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "n=%d argv=%d" % (len(data), argv_len), "total_cost_usd": 0, "num_turns": 1}))\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    r = Runner({"binary": str(fake), "auth": "login"}, {"dev": {"description": "d", "prompt": "p", "model": "sonnet"}})
    prompt = "x" * 300_000
    res = r.run("dev", prompt, None, max_usd=1.0, cwd=tmp_path / "w", timeout_s=60)
    out = json.dumps(res.events)
    assert "n=300000" in out
    argv_len = int(out.split("argv=")[1].split('"')[0].split("\\")[0])
    assert argv_len < 5000
