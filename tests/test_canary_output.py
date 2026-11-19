"""T-0165: the canary passes only if the cycle ended AND produced a non-empty result artefact."""
from pathlib import Path

from team import friction, gitops
from team.cycle import Cycle
from team.store import Store

from test_store_cycle import FakeRunner, _product, _settings

REPO = Path(__file__).resolve().parent.parent


def _setup(tmp_path):
    st = _settings(tmp_path)
    (st.root / "team.yaml").write_text("paths:\n  products_dir: products\n  data_dir: data\n  repos_dir: data/repos\n"
        "cycle:\n  digest_hours_utc: [0]   # T-0170: digest always due, so the dry-run canary result does not depend on the UTC hour\n", encoding="utf-8")
    p = _product(st)
    return st, p, Cycle(st, p, FakeRunner(), "c9")


def test_canary_with_real_output_passes(tmp_path: Path):
    st, p, cy = _setup(tmp_path)
    ok, note = cy.canary(REPO, p.slug)
    assert ok, note
    assert "result artefact" in note


def test_canary_empty_result_fails_records_friction_and_blocks_merge(tmp_path: Path, monkeypatch):
    st, p, cy = _setup(tmp_path)
    monkeypatch.setattr(Cycle, "_canary_artefacts", staticmethod(lambda pdir, before: []))
    ok, note = cy.canary(REPO, p.slug)
    assert not ok and "canary empty result" in note
    recs = friction.collect_product(p, cy.store)
    assert any("canary empty result" in r["summary"] for r in recs)

    # the real canary verdict (not a stub) must keep the PR from merging
    st.raw.setdefault("framework", {})["auto_merge"] = True
    st.raw["framework"]["canary_product"] = p.slug
    t = cy.store.create_task(title="t", type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="test")
    cy.store.transition(t["id"], "ready", "po")
    merged = []
    monkeypatch.setattr(gitops, "push_branch", lambda repo, tid: "abc")
    monkeypatch.setattr(gitops, "changed_files", lambda repo, base: ["team/x.py"])
    monkeypatch.setattr(gitops, "tests_modified_or_deleted", lambda repo, base: [])
    monkeypatch.setattr(gitops, "open_pull_request", lambda *a, **k: "https://github.com/o/r/pull/1")
    monkeypatch.setattr(gitops, "update_pull_request_body", lambda *a, **k: None)
    monkeypatch.setattr(gitops, "comment_pull_request", lambda *a, **k: None)
    monkeypatch.setattr(gitops, "merge_pull_request", lambda url, **kw: merged.append(url) or "deadbeef00")
    monkeypatch.setattr(gitops, "update_running_checkout", lambda *a, **k: None)
    cy.release_as_pull_request(cy.store.task(t["id"]), tmp_path, {"summary": "s"})
    assert cy.store.task(t["id"])["state"] == "waiting_owner" and not merged
