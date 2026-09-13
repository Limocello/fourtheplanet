"""Bake the buzzword index into a JavaScript file the standalone page can load.

The page is a single HTML file opened straight from disk. It has no server to
ask, and `fetch` of a local JSON file is blocked under file://, so the data
arrives as a plain script that assigns one global.

    python3 make_page_data.py --out web/peacock-data.js

What goes in:

  companies   ticker, name, word count and hard-number count, one row each
  terms       the lexicon, each with its per-company counts. These are the
              overlap-resolved counts, the same numbers the CLI reports, so the
              page agrees with buzzword.py out of the box.
  peacocks    the five peacock drawings, inlined, least to most fanned out
  vocab       per-company counts for every ordinary word in the corpus that
              occurs at least --min-hits times, so a word typed into the page
              can be counted without a round trip. Each word carries two
              figures: every occurrence, and only those occurrences the lexicon
              does not already claim.

Counts are stored sparsely as "company:count" pairs in hexadecimal, which is
compact enough to keep the whole thing a few megabytes and trivial to decode.

A word typed into the page usually overlaps something the lexicon already
counts. "sustainability" is already inside the term "sustainable", and all
44 588 of its occurrences are spoken for. Adding it must therefore change the
score by nothing at all, so the page scores an added word on its unclaimed
occurrences only. Counting them again would count the same words twice and
inflate every company that uses them.

This is resolved against the full default lexicon at build time. Switching a
lexicon term off in the page does not hand its text back to an added word.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time

import numpy as np

import buzzword_engine as be


def encode(company_of: np.ndarray, positions: np.ndarray) -> str:
    """Positions -> "company:count,company:count" in hex, companies ascending."""
    if positions.size == 0:
        return ""
    who, how_many = np.unique(company_of[positions], return_counts=True)
    return ",".join(f"{a:x}:{b:x}" for a, b in zip(who.tolist(), how_many.tolist()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join("web", "peacock-data.js"))
    ap.add_argument("--min-hits", type=int, default=4,
                    help="an addable word must occur at least this often corpus-wide")
    ap.add_argument("--sources", default="auto", choices=["auto", "reports", "pages", "all"])
    args = ap.parse_args()

    t0 = time.time()
    index = be.Index()
    terms = be.build_terms()

    # Which documents count for each company, using the same rule as the CLI.
    rows = be.aggregate_by_symbol(index, terms, index.score(terms)["counts"], args.sources)
    used_files = {f for r in rows for f in r["files"]}
    companies = [r["symbol"] for r in sorted(rows, key=lambda r: r["symbol"])]
    slot = {s: i for i, s in enumerate(companies)}

    names: dict[str, str] = {}
    path = os.path.join(be.ROOT, "data", "companies.csv")
    if os.path.exists(path):
        import csv
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                names[row["symbol"]] = row.get("company") or ""

    # Map every token position to a company, or to -1 for a document this
    # company is not scored on, so unused duplicates cannot leak into a count.
    company_of = np.full(index.tokens.size, -1, dtype=np.int32)
    words = [0] * len(companies)
    substance = [0] * len(companies)
    for doc in index.docs:
        if doc["file"] not in used_files:
            continue
        i = slot[doc["symbol"]]
        lo, hi = doc["token_offset"], doc["token_offset"] + doc["n_tokens"]
        company_of[lo:hi] = i
        words[i] += doc["n_words"]
        substance[i] += doc["quantity_hits"] + doc["year_hits"]

    def usable(positions: np.ndarray) -> np.ndarray:
        return positions[company_of[positions] >= 0]

    # --- the lexicon, with overlaps already resolved --------------------------
    claimed = np.zeros(index.tokens.size, dtype=bool)
    spans: list[tuple[int, int, int]] = []
    for j, term in enumerate(terms):
        starts, lengths = index.find(term["pattern"], term.get("syntax", "regex"))
        spans.extend(zip(starts.tolist(), lengths.tolist(), [j] * starts.size))
    spans.sort(key=lambda s: (s[0], -s[1]))

    kept: list[list[int]] = [[] for _ in terms]
    cursor = -1
    for start, length, j in spans:
        if start < cursor:
            continue
        kept[j].append(start)
        claimed[start:start + length] = True
        cursor = start + length

    term_out = []
    for j, term in enumerate(terms):
        positions = usable(np.array(kept[j], dtype=np.int64))
        term_out.append({
            "label": term["label"], "tier": term["tier"],
            "weight": term["weight"], "c": encode(company_of, positions),
        })
    print(f"  {len(term_out)} lexicon terms", flush=True)

    # --- words the page may add ----------------------------------------------
    totals = np.bincount(index.tokens, minlength=len(index.vocab))
    vocab_out: dict[str, str] = {}
    overlap_out: dict[str, int] = {}
    for word, wid in index.vocab.items():
        if not (word.isalpha() and 3 <= len(word) <= 20 and totals[wid] >= args.min_hits):
            continue
        positions = usable(index.positions_of(wid).astype(np.int64))
        if positions.size == 0:
            continue
        free = positions[~claimed[positions]]
        entry = {"c": encode(company_of, positions)}
        taken = int(positions.size - free.size)
        if taken:
            # Only the unclaimed occurrences may be added to a score.
            entry["n"] = encode(company_of, free)
            overlap_out[word] = taken
        vocab_out[word] = entry
    overlapping = sum(1 for v in vocab_out.values() if "n" in v)
    print(f"  {len(vocab_out):,} addable words, {overlapping:,} of them overlapping the lexicon",
          flush=True)

    # The five drawings, inlined so the page stays a single thing to move
    # around. They are small: about 150 kB of SVG against 7 MB of counts.
    peacocks = []
    art = os.path.join(os.path.dirname(args.out) or ".", "peacocks")
    for n in range(1, 6):
        svg = os.path.join(art, f"peacock-{n}.svg")
        if os.path.exists(svg):
            with open(svg, "rb") as fh:
                peacocks.append("data:image/svg+xml;base64," +
                                base64.b64encode(fh.read()).decode())
    print(f"  {len(peacocks)} peacock drawings inlined", flush=True)

    payload = {
        "built": time.strftime("%Y-%m-%d"),
        "peacocks": peacocks,
        "stages": [
            {"name": "Tail down", "note": "plain speech"},
            {"name": "Mild plumage", "note": "a little display"},
            {"name": "Fanned", "note": "visibly performing"},
            {"name": "Full display", "note": "heavy on the language"},
            {"name": "Peak pomposity", "note": "all feathers, squawking"},
        ],
        "companies": companies,
        "names": [names.get(s, "") for s in companies],
        "words": words,
        "substance": substance,
        "tiers": be.TIER_ORDER,
        "terms": term_out,
        "vocab": vocab_out,
        "overlap": overlap_out,
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("// Generated by make_page_data.py. Do not edit by hand.\n")
        fh.write("window.PEACOCK = ")
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
        fh.write(";\n")

    size = os.path.getsize(args.out) / 1e6
    print(f"\nwrote {args.out}: {size:.1f} MB, {len(companies)} companies, in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
