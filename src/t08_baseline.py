"""T08 baseline: delete the raw video of the subject by filename only.
No lineage tracing (frames/crops/embeddings/backups/exports stay), no legal-hold check, no review."""
from __future__ import annotations

import pandas as pd

from .common.context import RunContext
from .common.decision import Decision, write_decision
from .pipeline import load


def run(ctx: RunContext) -> None:
    data = load(ctx)
    rows = []
    for r in sorted(data["requests"], key=lambda r: r["request_id"]):
        for a in data["artefacts"]:
            fn = a.get("filename", "")
            if a["type"] == "raw_video" and fn.endswith(".mp4") and r["subject_id"] in fn:
                rows.append({"request_id": r["request_id"], "artefact_id": a["artefact_id"], "action": "delete"})
    ctx.stats["n_items"] = len(rows)
    pd.DataFrame(rows, columns=["request_id", "artefact_id", "action"]).to_csv(ctx.out("actions.csv"), index=False)
    write_decision(
        Decision(status="GO", reason_code="BASELINE_NO_REVIEW", reason="Baseline: raw file deleted by filename, no review",
                 owner=ctx.config.get("owner", "TODO-owner"), scope=ctx.track),
        ctx.out("decision.json"), ctx.out("decision_log.csv"))
