#!/usr/bin/env python3
"""AITranscribe's stress clip labelling (label_server.py, Label mode) as an On the Record review, with its labels.

The same clips in the same seeded order, minus excluded videos and clips marked bad (as the page skipped them), no
title or repetition number on screen, and WhiStress's pick revealed only after answering (or first, recorded as
seen). Labels already in clip_labels.csv come along. One counts as seen only if WhiStress had a pick to show at the
time (whistress_seen 1 and whistress_index set), as score_corpus_labels.py decides; a label given to a clip's earlier
cut (another sentence) is left behind.

    python scripts/review_stress_clips.py [--aitranscribe ~/src/AITranscribe] [--manifest stress_search/clip_manifest_v3.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from ontherecord import reviews
from ontherecord.config import load_config


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aitranscribe", default=str(Path.home() / "src" / "AITranscribe"))
    ap.add_argument("--manifest", default="stress_search/clip_manifest_v3.csv")
    ap.add_argument("--whistress", default="stress_search/whistress_reps_v3.csv")
    ap.add_argument("--labels", default="stress_search/labels")
    args = ap.parse_args()
    here = Path(args.aitranscribe)
    sys.path.insert(0, str(here))
    import label_server as ls  # its manifest reading and its seeded order, so the clips are exactly the page's
    from verify_stress import latest_rows

    labels = here / args.labels
    videos, paths = ls.video_info(here / args.manifest)
    by_id = {v["id"]: v for v in videos}
    excluded = {v for (v,), r in latest_rows(labels / "video_review.csv", "video_id").items() if r["decision"] == "exclude"}
    bad = {k for k, r in latest_rows(labels / "clip_review.csv", "video_id", "condition").items() if r["bad"] == "1"}
    picks = {}
    if (here / args.whistress).exists():
        with open(here / args.whistress, newline="", encoding="utf-8") as fh:
            picks = {(r["video_id"], r["condition"]): int(r["top_index"]) for r in csv.DictReader(fh) if r["top_index"] != ""}
    order = [(v, c) for v, c in ls.blind_order(videos) if v not in excluded and (v, c) not in bad]
    items = []
    for v, c in order:
        info = by_id[v]
        item = {"id": f"{v}/{c}", "text": info["phrase"], "words": info["words"], "clip": {"file": str(paths[(v, c)])},
                "hidden": {"video_id": v, "condition": c, "bucket": info["bucket"], "title": info["title"]}}
        if (v, c) in picks:
            item["machine"] = picks[(v, c)]
        items.append(item)
    sid = reviews.create(ws := load_config().workspace, {
        "name": "stress clips: which word is stressed", "order": "given", "machine_label": "WhiStress",
        "question": "Which word or words does the speaker stress? Pick them, or say that no word stands out, that you "
                    "can't tell, or that the clip is bad. There is no title and no repetition number, so the order a "
                    "demo said things in can't steer the answer.",
        "source": {"kind": "AITranscribe stress clips", "manifest": args.manifest, "seed": ls.SEED,
                   "excluded_videos": len(excluded), "bad_clips": len(bad)},
        "answer": {"kind": "words", "options": [
            {"value": "none", "label": "No word stands out", "key": "x", "score": False},
            {"value": "unsure", "label": "Can't tell", "key": "u", "score": False},
            {"value": "bad", "label": "Bad", "score": False, "reasons": [
                {"value": "cut_off", "label": "cut off", "key": "c"},
                {"value": "wrong_words", "label": "wrong or extra words", "key": "w"},
                {"value": "two_readings", "label": "two readings", "key": "t"}]}]},
        "items": items})
    ids, brought = {i["id"] for i in items}, 0
    if (labels / "clip_labels.csv").exists():
        with open(labels / "clip_labels.csv", newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                key = f"{r['video_id']}/{r['condition']}"
                if key not in ids or r["phrase"] != by_id[r["video_id"]]["phrase"]:
                    continue  # excluded since, or labelled before a re-cut changed the sentence
                value = json.loads(r["stressed_indices"]) if r["status"] == "stressed" else r["status"]
                reviews._append(ws, sid, {"item": key, "value": value, "reason": r.get("reason", ""), "flags": {},
                                          "note": r.get("note", ""), "seconds": None,
                                          # seen only if a pick existed to be shown (score_corpus_labels.is_blind)
                                          "machine_seen": int(r.get("whistress_seen") == "1" and r.get("whistress_index", "") != ""),
                                          "by": "me",
                                          "utc": r["ts"] + "Z", "imported_from": "clip_labels.csv"})
                brought += 1
    print(f"{sid}: {len(items)} clips ({len(excluded)} videos excluded, {len(bad)} clips bad), {len(picks)} WhiStress "
          f"picks, {brought} labels brought over. Open http://127.0.0.1:8765/#review/{sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
