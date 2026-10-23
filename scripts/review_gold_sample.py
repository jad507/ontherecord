#!/usr/bin/env python3
"""hands_on_dl's gold sample (gold_label_server.py) as an On the Record review, and its answers back again.

The same 180 blocks in the sheet's own (pre-registered, already shuffled) order, with their neighbors and audio cut
from the local videos. Blind as before, and more strictly: the set holds no model output at all (reveal: never), so
nothing from gold_sample_KEY.json can reach the page. Answers already in gold_labels_log.csv come along.

    python scripts/review_gold_sample.py [--hands-on-dl ~/src/hands_on_dl]            # make the set
    python scripts/review_gold_sample.py --write-back SET_ID                         # answers -> gold_coding.csv

--write-back fills the pre-registered sheet from the review's latest answers, the way gold_label_server.py did, so
hands_on_dl's analysis reads them unchanged ("[fused]" at the start of a fused block's note).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from ontherecord import reviews
from ontherecord.config import load_config


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hands-on-dl", default=str(Path.home() / "src" / "hands_on_dl"))
    ap.add_argument("--write-back", metavar="SET_ID")
    args = ap.parse_args()
    sys.path.insert(0, args.hands_on_dl)
    import gold_label_server as g  # its parsing, so the items are exactly the page's

    ws = load_config().workspace
    if args.write_back:
        latest = reviews.latest(reviews.rows(ws, args.write_back))
        g.write_coding({int(k): {"public_comment": r["value"], "fused": "1" if r["flags"].get("fused") else "0",
                                 "note": r["note"]} for k, r in latest.items()})
        print(f"{len(latest)} answers -> {g.CODING}")
        return 0

    sheet = g.parse_blind(g.BLIND)
    where = g.locate(sheet)
    items = []
    for it in sheet:
        n = it["item"]
        item = {"id": str(n), "title": it["title"], "speaker": it["speaker"], "start": it["start"], "end": it["end"],
                "text": it["text"], "context": []}
        if n in where:
            data, i = where[n]
            bl = data["blocks"]
            item["text"] = bl[i]["text"]  # the sheet cuts long blocks off; the meeting file has all of it
            item["context"] = [{"offset": j - i, "speaker": b["speaker"], "start": b["start"], "end": b["end"],
                                "text": b["text"]} for j, b in enumerate(bl) if abs(j - i) <= 12]
            vid = data.get("video_id")
            video = g.video_for(vid) if vid else None
            if video:
                item["clip"] = {"path": str(video), "start": it["start"], "end": it["end"]}
            if vid:
                item["link"] = f"https://www.youtube.com/watch?v={vid}&t={int(it['start'])}s"
        items.append(item)
    question = ("Is the TARGET block a member of the public addressing the body? Council members, staff, presenters "
                "and procedural speech are no. If the block runs two speakers together (say a public comment and the "
                "chair's reply), answer unsure, tick fused, and say what is fused in the note: unit boundaries are "
                "part of what is being studied. Grey blocks are context only; don't code them.")
    sid = reviews.create(ws, {
        "name": "gold sample: public comment", "question": question, "order": "given", "reveal": "never",
        "source": {"kind": "hands_on_dl gold sample", "sheet": str(g.BLIND), "preregistration": str(g.GOLD / "PREREGISTRATION.md")},
        "answer": {"kind": "choice", "options": [{"value": "yes", "label": "Yes", "key": "y"},
                                                 {"value": "no", "label": "No", "key": "n"},
                                                 {"value": "unsure", "label": "Unsure", "key": "u"}],
                   "flags": [{"name": "fused", "label": "fuses two speakers", "key": "f"}]},
        "items": items})
    brought = 0
    if g.LOG.exists():
        with open(g.LOG, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                reviews.answer(ws, sid, r["item"], r["public_comment"], flags={"fused": r["fused"] == "1"},
                               note=r["note"], seconds=float(r["seconds"]) if r["seconds"] else None)
                brought += 1
    print(f"{sid}: {len(items)} items, {sum('clip' in i for i in items)} with audio, {brought} answers brought over. "
          f"Open http://127.0.0.1:8765/#review/{sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
