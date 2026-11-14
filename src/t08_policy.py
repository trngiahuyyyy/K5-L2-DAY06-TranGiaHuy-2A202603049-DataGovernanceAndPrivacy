"""T08 - Delete Request Simulator: pure policy logic (no repo imports, easy to unit-test).

Precedence for every artefact tied to a deletion request (first match wins):
    1. legal_hold set                         -> retain_legal_hold
    2. backup/copy of a held artefact         -> escalate (COPY_OF_HELD_ARTEFACT)
    3. actor group not in artefact ACL        -> escalate (NO_PERMISSION)
    4. origin unknown                         -> escalate (ORIGIN_UNKNOWN)
    5. artefact shared by >1 data subject     -> escalate (MULTI_SUBJECT / SHARED_EXPORT)
    6. request identity not verified          -> escalate (IDENTITY_NOT_VERIFIED)
    7. otherwise                              -> delete
Descendants (BFS over parent->child) that do NOT belong to the requesting subject are never
deleted; they are emitted as `escalate` (DERIVED_OTHER_SUBJECT) so a human sees the lineage.
Thresholds / precedence are frozen here BEFORE looking at any evaluation result.
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path

ACTIONS = ("delete", "retain_legal_hold", "escalate")

R_DELETE = "DELETE_SUBJECT_DATA"
R_HOLD = "LEGAL_HOLD"
R_HOLD_COPY = "COPY_OF_HELD_ARTEFACT"
R_NO_PERM = "NO_PERMISSION"
R_ORIGIN = "ORIGIN_UNKNOWN"
R_MULTI = "MULTI_SUBJECT"
R_SHARED = "SHARED_EXPORT"
R_DERIVED_OTHER = "DERIVED_OTHER_SUBJECT"
R_IDENTITY = "IDENTITY_NOT_VERIFIED"

# higher = reviewer should look first
REVIEW_SCORE = {
    R_ORIGIN: 0.90, R_NO_PERM: 0.90, R_SHARED: 0.80, R_MULTI: 0.70,
    R_HOLD_COPY: 0.60, R_IDENTITY: 0.55, R_DERIVED_OTHER: 0.50,
    R_HOLD: 0.0, R_DELETE: 0.0,
}


def load_graph(input_dir):
    d = Path(input_dir)
    g = json.loads((d / "data_graph.json").read_text(encoding="utf-8"))
    reqs = json.loads((d / "requests.json").read_text(encoding="utf-8"))
    return g["artefacts"], reqs


def validate_graph(artefacts) -> list[dict]:
    problems, seen = [], set()
    ids = {a["artefact_id"] for a in artefacts}
    for a in artefacts:
        if a["artefact_id"] in seen:
            problems.append({"problem": "duplicate_artefact_id", "artefact_id": a["artefact_id"]})
        seen.add(a["artefact_id"])
        for p in a["parent_ids"]:
            if p not in ids:
                problems.append({"problem": "dangling_parent", "artefact_id": a["artefact_id"], "parent": p})
    return problems


def build_index(artefacts):
    by_id = {a["artefact_id"]: a for a in artefacts}
    children = defaultdict(list)
    for a in artefacts:
        for p in a["parent_ids"]:
            children[p].append(a["artefact_id"])
    return by_id, children


def descendants(children, roots) -> set:
    """roots + everything reachable through children (cycle-safe BFS)."""
    seen, q = set(roots), deque(roots)
    while q:
        for c in children.get(q.popleft(), []):
            if c not in seen:
                seen.add(c)
                q.append(c)
    return seen


def classify(a, by_id, actor_group, identity_verified=True):
    """Action + reason for an artefact that belongs to the requesting subject."""
    if a.get("legal_hold"):
        return "retain_legal_hold", R_HOLD
    if a["type"] == "backup" and any(by_id[p].get("legal_hold") for p in a["parent_ids"] if p in by_id):
        return "escalate", R_HOLD_COPY
    if actor_group not in (a.get("acl") or []):
        return "escalate", R_NO_PERM
    if not a.get("origin_known", False):
        return "escalate", R_ORIGIN
    n = len(a["subject_ids"])
    if n > 1:
        return "escalate", (R_SHARED if n > 2 else R_MULTI)
    if not identity_verified:
        return "escalate", R_IDENTITY
    return "delete", R_DELETE


def plan_request(request, by_id, children, actor_group="data-platform", include_derived_other=True):
    sid = request["subject_id"]
    verified = bool(request.get("identity_verified", False))
    tagged = [i for i, a in by_id.items() if sid in a["subject_ids"]]
    rows = []
    for aid in sorted(descendants(children, tagged)):
        a = by_id[aid]
        if sid in a["subject_ids"]:
            action, reason = classify(a, by_id, actor_group, verified)
        elif include_derived_other:
            action, reason = "escalate", R_DERIVED_OTHER
        else:
            continue
        rows.append({
            "request_id": request["request_id"], "artefact_id": aid, "action": action,
            "reason_code": reason, "score": REVIEW_SCORE[reason], "type": a["type"],
            "uri": a.get("uri", ""), "subject_ids": ";".join(a["subject_ids"]),
        })
    return rows


def plan_all(requests, artefacts, actor_group="data-platform", include_derived_other=True) -> list[dict]:
    by_id, children = build_index(artefacts)
    rows = []
    for r in sorted(requests, key=lambda r: r["request_id"]):
        rows += plan_request(r, by_id, children, actor_group, include_derived_other)
    return sorted(rows, key=lambda x: (x["request_id"], x["artefact_id"]))


def item_id(row) -> str:
    return f"{row['request_id']}|{row['artefact_id']}"


def apply_review(rows, reviewed):
    """Apply reviewer decisions. `reviewed` = list of dicts with keys
    item_id, reviewer, reviewer_decision, override_value, review_note.
    Returns (new_rows, events). Safe-by-default: a reviewer can never delete a held artefact,
    and every delete override needs a note (evidence)."""
    rows = [dict(r) for r in rows]
    index = {item_id(r): r for r in rows}
    events = []
    for rv in reviewed:
        decision = (rv.get("reviewer_decision") or "").strip().lower()
        if not decision:
            continue
        reviewer = (rv.get("reviewer") or "").strip()
        if not reviewer:
            raise ValueError(f"review row {rv.get('item_id')!r} has a decision but no reviewer")
        iid = (rv.get("item_id") or "").strip()
        if iid not in index:
            raise ValueError(f"reviewed item {iid!r} is not in the plan")
        row, before = index[iid], index[iid]["action"]
        note = (rv.get("review_note") or "").strip()
        new, outcome, detail = before, "noted", ""
        if decision == "edit":
            val = (rv.get("override_value") or "").strip()
            if val not in ACTIONS:
                outcome, detail = "refused", f"invalid_override_value:{val!r}"
            elif before == "retain_legal_hold" and val != before:
                outcome, detail = "refused", "legal_hold_cannot_be_overridden"
            elif val == "delete" and not note:
                outcome, detail = "refused", "delete_override_requires_note"
            else:
                new, outcome = val, "applied"
        elif decision == "reject" and before == "delete":
            new, outcome = "escalate", "applied"
        elif decision not in ("approve", "reject", "escalate", "skip"):
            outcome, detail = "refused", f"unknown_decision:{decision}"
        row["action"] = new
        row["reviewed_by"], row["review_note"] = reviewer, note
        events.append({"item_id": iid, "reviewer": reviewer, "decision": decision,
                       "outcome": outcome, "detail": detail, "before": before, "after": new})
    return rows, events


def summarize(rows, n_integrity_problems=0):
    """Overall gate: (status, reason_code, reason)."""
    n_escalate = sum(r["action"] == "escalate" for r in rows)
    n_hold = sum(r["action"] == "retain_legal_hold" for r in rows)
    n_delete = sum(r["action"] == "delete" for r in rows)
    if n_integrity_problems:
        return "REWORK", "GRAPH_INTEGRITY", f"{n_integrity_problems} data-graph integrity problems"
    if n_escalate:
        return "HOLD", "PENDING_EXCEPTION_REVIEW", (
            f"{n_escalate} artefacts need human review before deletion ({n_delete} delete, {n_hold} legal hold)")
    return "GO", "DELETION_PLAN_CLEAR", f"{n_delete} delete, {n_hold} retained under legal hold, 0 pending"
