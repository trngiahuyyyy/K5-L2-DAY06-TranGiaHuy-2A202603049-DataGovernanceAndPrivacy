"""Label-free self-check for T08 (no gt/ needed). Counts safety violations computed straight from the
input graph, so it does not depend on our own policy code.
Usage: python tools/t08_selfcheck.py [fixtures/t08_deletion] [out/baseline/actions.csv] [out/poc/actions.csv]"""
import json
import sys
from pathlib import Path

import pandas as pd

fix = Path(sys.argv[1] if len(sys.argv) > 1 else "fixtures/t08_deletion")
paths = {"baseline": sys.argv[2] if len(sys.argv) > 2 else "out/baseline/actions.csv",
         "poc": sys.argv[3] if len(sys.argv) > 3 else "out/poc/actions.csv"}
g = {a["artefact_id"]: a for a in json.loads((fix / "data_graph.json").read_text(encoding="utf-8"))["artefacts"]}
req = {r["request_id"]: r["subject_id"] for r in json.loads((fix / "requests.json").read_text(encoding="utf-8"))}
tagged = {(rid, aid) for rid, sid in req.items() for aid, a in g.items() if sid in a["subject_ids"]}
identifying = {k for k in tagged if g[k[1]]["type"] in ("face_crop", "embedding", "frame", "backup")}


def violations(rid, aid):
    a, v = g[aid], []
    if a["legal_hold"]: v.append("legal_hold")
    if "data-platform" not in a["acl"]: v.append("no_permission")
    if not a["origin_known"]: v.append("origin_unknown")
    if len(a["subject_ids"]) > 1: v.append("multi_subject")
    if req[rid] not in a["subject_ids"]: v.append("other_subject")
    if a["type"] == "backup" and any(g[p]["legal_hold"] for p in a["parent_ids"]): v.append("copy_of_held")
    return v


rows = []
for name, p in paths.items():
    d = pd.read_csv(p)
    pairs = set(zip(d.request_id, d.artefact_id))
    dele = d[d.action == "delete"]
    bad = [(r.request_id, r.artefact_id, violations(r.request_id, r.artefact_id)) for r in dele.itertuples()]
    bad = [b for b in bad if b[2]]
    rows.append({
        "run": name, "rows": len(d), "delete": len(dele), "escalate": int((d.action == "escalate").sum()),
        "retain_legal_hold": int((d.action == "retain_legal_hold").sum()),
        "UNSAFE_deletes": len(bad),
        "tagged_total": len(tagged), "tagged_silently_left": len(tagged - pairs),
        "identifying_left (frame/crop/emb/backup)": len(identifying - pairs),
    })
    for rid, aid, v in bad[:5]:
        print(f"  [{name}] unsafe delete {rid} {aid} {g[aid]['type']}: {','.join(v)}")
print(pd.DataFrame(rows).set_index("run").T.to_string())
