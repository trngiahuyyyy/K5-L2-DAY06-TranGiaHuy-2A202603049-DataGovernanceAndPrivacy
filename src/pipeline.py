"""T08 PoC pipeline - Retention & Delete Request Simulator.

load    : manifest provenance check + data_graph.json + requests.json (+ graph integrity check)
process : per request, BFS lineage (raw -> frame -> crop -> embedding, backup, export) and a frozen
          precedence policy (see src/t08_policy.py) -> one action per (request, artefact)
rank    : every `escalate` row goes to the review queue (priority by risk)
review  : apply out/review_queue_reviewed.csv (hold can never be overridden, delete needs a note)
decide  : GO / HOLD / REWORK with reason_code + owner
Nothing is really deleted (mock data); `dry_run` only controls the `executed` column.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import t08_policy as P
from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.manifest import verify_manifest
from .common.review import load_review_decisions, make_review_queue, write_review_queue
from .metrics.tracks import get_track

_COLS = ["request_id", "artefact_id", "action", "reason_code", "score", "type", "uri", "subject_ids"]


# ---------------------------------------------------------------- 01 load
def load(ctx: RunContext) -> dict:
    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")
    data = {"manifest": pd.read_csv(manifest) if manifest.exists() else None}
    if ctx.track == "t08":
        artefacts, requests = P.load_graph(ctx.input_dir)
        data["artefacts"], data["requests"] = artefacts, requests
        data["integrity"] = P.validate_graph(artefacts)
        if data["integrity"]:
            ctx.audit.log(actor="pipeline", action="graph_integrity_problems", item_id="data_graph.json",
                          after=data["integrity"][:20])
    return data


# ---------------------------------------------------------------- 02 process
def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    if ctx.track != "t08":
        spec = get_track(ctx.track)
        raise NotImplementedError(f"This pipeline implements T08 only, not {spec.track} ({spec.title}).")
    dry_run = bool(ctx.param("dry_run", True))
    rows = P.plan_all(data["requests"], data["artefacts"],
                      actor_group=str(ctx.param("actor_group", "data-platform")),
                      include_derived_other=bool(ctx.param("emit_derived_other", True)))
    for r in rows:
        # item_id = bare artefact_id: the evaluator matches audit entries to actions.csv by artefact_id
        ctx.audit.log(actor="pipeline", action=f"plan_{r['action']}", item_id=r["artefact_id"],
                      after={"request_id": r["request_id"], "reason_code": r["reason_code"], "type": r["type"],
                             "uri": r["uri"], "subjects": r["subject_ids"]})
    df = pd.DataFrame(rows, columns=_COLS)
    df["executed"] = (df["action"] == "delete") & (not dry_run)
    ctx.stats["dry_run"] = dry_run
    for a in P.ACTIONS:
        ctx.stats[f"n_{a}"] = int((df["action"] == a).sum())
    return df


# ---------------------------------------------------------------- 03 rank
def rank(ctx: RunContext, scored: pd.DataFrame) -> pd.DataFrame:
    """Review queue = all `escalate` rows, riskiest first (deterministic order)."""
    esc = scored[scored["action"] == "escalate"].copy()
    esc = esc.sort_values(["score", "request_id", "artefact_id"], ascending=[False, True, True])
    esc["item_id"] = esc["request_id"] + "|" + esc["artefact_id"]
    esc["reason"] = esc["reason_code"] + " | " + esc["type"] + " | " + esc["subject_ids"]
    if esc.empty:
        return pd.DataFrame(columns=["item_id", "score", "reason"])
    budget = int(ctx.param("budget", max(len(esc), 1)))
    q = make_review_queue(esc[["item_id", "score", "reason"]], id_col="item_id", score_col="score",
                          budget=budget, reason_col="reason")
    write_review_queue(q, ctx.out("review_queue.csv"))
    return q


# ---------------------------------------------------------------- 04 review
def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame) -> pd.DataFrame:
    # look next to review_queue.csv first (ctx.out), then ctx.output_dir; never skip silently
    candidates = [Path(ctx.out("review_queue_reviewed.csv")), Path(ctx.output_dir) / "review_queue_reviewed.csv"]
    reviewed_path = next((c for c in candidates if c.exists()), None)
    ctx.stats["review_file_found"] = reviewed_path is not None
    if reviewed_path is None:
        ctx.stats["review_file_searched"] = [str(c) for c in candidates]
        return scored
    ctx.stats["review_file"] = str(reviewed_path)
    ctx.stats["n_reviewed"] = len(load_review_decisions(reviewed_path))   # validates reviewer column
    rv = pd.read_csv(reviewed_path, dtype=str).fillna("")
    id_col = "item_id" if "item_id" in rv.columns else rv.columns[0]
    rv = rv.rename(columns={id_col: "item_id"})
    for col in ("reviewer", "reviewer_decision", "override_value", "review_note"):
        if col not in rv.columns:
            rv[col] = ""
    rows, events = P.apply_review(scored.to_dict("records"), rv.to_dict("records"))
    for e in events:
        req_id, art_id = e["item_id"].split("|", 1)
        ctx.audit.log(actor=e["reviewer"], action=f"review_{e['decision']}_{e['outcome']}", item_id=art_id,
                      after={"request_id": req_id, "before": e["before"], "after": e["after"], "detail": e["detail"]})
    ctx.stats["n_override_refused"] = sum(e["outcome"] == "refused" for e in events)
    out = pd.DataFrame(rows)
    dry_run = bool(ctx.param("dry_run", True))
    out["executed"] = (out["action"] == "delete") & (not dry_run)
    return out


# ---------------------------------------------------------------- 05 decide
def decide(ctx: RunContext, final: pd.DataFrame) -> Decision:
    n_bad = ctx.stats["n_integrity_problems"] if "n_integrity_problems" in ctx.stats else 0
    status, code, reason = P.summarize(final.to_dict("records"), n_bad)
    return Decision(status=status, reason_code=code, reason=reason,
                    owner=ctx.config.get("owner", "TODO-owner"), scope=ctx.track)


def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    data = load(ctx)
    ctx.stats["n_integrity_problems"] = len(data.get("integrity", []))
    scored = process(ctx, data)
    ctx.stats["n_items"] = len(scored)
    queue = rank(ctx, scored)
    final = review(ctx, scored, queue)
    final[spec.pred_columns + [c for c in final.columns if c not in spec.pred_columns]].to_csv(ctx.out(spec.pred_file), index=False)
    write_decision(decide(ctx, final), ctx.out("decision.json"), ctx.out("decision_log.csv"))
