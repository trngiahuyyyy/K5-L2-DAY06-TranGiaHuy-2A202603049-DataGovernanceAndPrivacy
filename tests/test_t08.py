"""T08 tests: pure-policy unit tests (synthetic graph) + end-to-end invariants on the real fixture."""
import csv
import json
from pathlib import Path

import pandas as pd
import pytest

from src import t08_policy as P

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "t08_deletion"


def art(aid, typ, subs, parents=(), hold=None, origin=True, acl=("data-platform",)):
    return {"artefact_id": aid, "type": typ, "subject_ids": list(subs), "parent_ids": list(parents),
            "uri": f"mock://{aid}", "owner": "data-platform", "acl": list(acl),
            "legal_hold": hold, "origin_known": origin, "created_at": "2026-01-01"}


def mini_graph():
    return [
        # subj_a: clean chain  raw -> backup, raw -> frame -> crop -> emb
        art("r_a", "raw_video", ["subj_a"]), art("b_a", "backup", ["subj_a"], ["r_a"]),
        art("f_a", "frame", ["subj_a"], ["r_a"]), art("c_a", "face_crop", ["subj_a"], ["f_a"]),
        art("e_a", "embedding", ["subj_a"], ["c_a"]),
        # shared frame subj_a + subj_x, with a child belonging only to subj_x
        art("f_s", "frame", ["subj_a", "subj_x"], ["r_a"]), art("c_x", "face_crop", ["subj_x"], ["f_s"]),
        # subj_h: legal hold on raw, backup is a copy of it, frame derived
        art("r_h", "raw_video", ["subj_h"], hold="LH-1"), art("b_h", "backup", ["subj_h"], ["r_h"]),
        art("f_h", "frame", ["subj_h"], ["r_h"]),
        # odd cases
        art("e_o", "embedding", ["subj_a"], ["c_a"], origin=False),
        art("v_p", "annotation", ["subj_a"], ["f_a"], acl=("vendor-x",)),
        art("x_exp", "training_export", ["subj_a", "subj_h", "subj_x"], ["e_a"]),
    ]


def req(rid, sid, verified=True):
    return {"request_id": rid, "subject_id": sid, "identity_verified": verified}


def acts(rows, rid):
    return {r["artefact_id"]: (r["action"], r["reason_code"]) for r in rows if r["request_id"] == rid}


def test_cascade_and_exceptions_for_clean_subject():
    a = acts(P.plan_all([req("DR-1", "subj_a")], mini_graph()), "DR-1")
    for aid in ("r_a", "b_a", "f_a", "c_a", "e_a"):          # whole lineage incl. backup + embedding
        assert a[aid] == ("delete", P.R_DELETE), aid
    assert a["f_s"] == ("escalate", P.R_MULTI)               # frame shared with another subject
    assert a["x_exp"] == ("escalate", P.R_SHARED)            # export of 3 subjects
    assert a["e_o"] == ("escalate", P.R_ORIGIN)
    assert a["v_p"] == ("escalate", P.R_NO_PERM)
    assert a["c_x"] == ("escalate", P.R_DERIVED_OTHER)       # other subject's child never deleted
    assert not any(v[0] == "delete" for k, v in a.items() if k in ("f_s", "c_x", "x_exp", "e_o", "v_p"))


def test_legal_hold_wins_and_copy_of_held_is_escalated():
    a = acts(P.plan_all([req("DR-2", "subj_h")], mini_graph()), "DR-2")
    assert a["r_h"] == ("retain_legal_hold", P.R_HOLD)
    assert a["b_h"] == ("escalate", P.R_HOLD_COPY)           # deleting the backup would bypass the hold
    assert a["f_h"][0] == "delete"


def test_unverified_identity_blocks_every_delete():
    rows = P.plan_all([req("DR-3", "subj_a", verified=False)], mini_graph())
    assert all(r["action"] != "delete" for r in rows)


def test_plan_is_idempotent():
    g, r = mini_graph(), [req("DR-1", "subj_a"), req("DR-2", "subj_h")]
    assert P.plan_all(r, g) == P.plan_all(r, g)


def test_cyclic_graph_terminates():
    g = [art("a", "frame", ["s"], ["b"]), art("b", "frame", ["s"], ["a"])]
    assert len(P.plan_all([req("DR-1", "s")], g)) == 2


def test_reviewer_cannot_override_legal_hold_or_delete_without_note():
    rows = P.plan_all([req("DR-2", "subj_h")], mini_graph())
    rv = [
        {"item_id": "DR-2|r_h", "reviewer": "huy", "reviewer_decision": "edit", "override_value": "delete", "review_note": "pls"},
        {"item_id": "DR-2|b_h", "reviewer": "huy", "reviewer_decision": "edit", "override_value": "delete", "review_note": ""},
    ]
    new, events = P.apply_review(rows, rv)
    got = {r["artefact_id"]: r["action"] for r in new}
    assert got["r_h"] == "retain_legal_hold"                 # refused: hold is final
    assert got["b_h"] == "escalate"                          # refused: no evidence note
    assert [e["outcome"] for e in events] == ["refused", "refused"]


def test_reviewer_with_note_can_release_copy_and_missing_reviewer_fails():
    rows = P.plan_all([req("DR-2", "subj_h")], mini_graph())
    ok = [{"item_id": "DR-2|b_h", "reviewer": "huy", "reviewer_decision": "edit",
           "override_value": "delete", "review_note": "legal confirms hold covers raw only"}]
    new, ev = P.apply_review(rows, ok)
    assert {r["artefact_id"]: r["action"] for r in new}["b_h"] == "delete" and ev[0]["outcome"] == "applied"
    with pytest.raises(ValueError):
        P.apply_review(rows, [{"item_id": "DR-2|b_h", "reviewer": "", "reviewer_decision": "approve"}])


def test_gate_status():
    assert P.summarize([{"action": "escalate"}])[0] == "HOLD"
    assert P.summarize([{"action": "delete"}, {"action": "retain_legal_hold"}])[0] == "GO"
    assert P.summarize([{"action": "delete"}], n_integrity_problems=1)[0] == "REWORK"


# ------------------------------------------------ end-to-end on the real fixture
def _run(out):
    import src.main as runner
    return runner.main(["--track", "t08", "--output", str(out), "--offline", "--seed", "42"])


def test_real_fixture_never_deletes_protected_artefacts(tmp_path):
    assert _run(tmp_path) == 0
    acts_df = pd.read_csv(tmp_path / "actions.csv")
    graph = {a["artefact_id"]: a for a in json.loads((FIX / "data_graph.json").read_text(encoding="utf-8"))["artefacts"]}
    reqs = {r["request_id"]: r["subject_id"] for r in json.loads((FIX / "requests.json").read_text(encoding="utf-8"))}
    assert set(acts_df["request_id"]) <= set(reqs)
    for r in acts_df[acts_df["action"] == "delete"].itertuples():
        a = graph[r.artefact_id]
        assert not a["legal_hold"] and a["origin_known"] and a["acl"] == ["data-platform"]
        assert a["subject_ids"] == [reqs[r.request_id]]       # single-subject, the requester's own data
    assert (tmp_path / "decision.json").exists()
    # audit trace completeness: the evaluator matches audit `item_id` to actions.csv `artefact_id`
    audit = tmp_path / "audit_log.jsonl"
    assert audit.exists()
    logged = {json.loads(l).get("item_id") for l in audit.read_text(encoding="utf-8").splitlines() if l.strip()}
    assert set(acts_df["artefact_id"]) <= logged


def test_real_fixture_is_idempotent(tmp_path):
    assert _run(tmp_path / "r1") == 0 and _run(tmp_path / "r2") == 0
    assert (tmp_path / "r1" / "actions.csv").read_bytes() == (tmp_path / "r2" / "actions.csv").read_bytes()


def test_reviewed_queue_in_output_dir_is_applied(tmp_path):
    """Regression: the reviewed file placed next to review_queue.csv must be picked up (not skipped silently)."""
    hdr = ["rank", "item_id", "score", "reason", "suggested_action", "slice", "evidence_path",
           "reviewer", "reviewer_decision", "override_value", "review_note", "reviewed_at"]
    def row(iid, dec, val, note):
        r = dict.fromkeys(hdr, "")
        r.update(item_id=iid, reviewer="tester", reviewer_decision=dec, override_value=val, review_note=note)
        return r
    rows = [row("DR-003|a0067", "edit", "delete", "both co-subjects requested deletion"),
            row("DR-002|a0043", "edit", "delete", "TEST: must be refused")]
    with open(tmp_path / "review_queue_reviewed.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=hdr)
        w.writeheader()
        w.writerows(rows)
    assert _run(tmp_path) == 0
    a = pd.read_csv(tmp_path / "actions.csv").set_index(["request_id", "artefact_id"]).action
    assert a[("DR-003", "a0067")] == "delete"                 # reviewer override applied
    assert a[("DR-002", "a0043")] == "retain_legal_hold"      # legal hold cannot be overridden
    info = json.loads((tmp_path / "run_info.json").read_text(encoding="utf-8"))
    assert info["stats"]["review_file_found"] is True and info["stats"]["n_override_refused"] == 1
