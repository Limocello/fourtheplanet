#!/usr/bin/env python3
"""Buzzword index — command line.

Scores 450 sustainability reports from 369 S&P 500 companies on how much of the
text is corporate and sustainability boilerplate.

    python3 buzzword.py board                       the leaderboard
    python3 buzzword.py board --add "steward*"      with an extra word of your own
    python3 buzzword.py lookup AAPL                 one company in detail
    python3 buzzword.py lookup nike.com             ticker, name or website all work
    python3 buzzword.py find "net zero"             where a term actually occurs
    python3 buzzword.py terms                       the 252-term lexicon
    python3 buzzword.py export out.csv              write the full table

Add --json to any command to get machine-readable output instead of a table,
which is the simplest way to feed this into a front end on another platform.
Every command re-scores the whole corpus, which takes about 0.2 seconds.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

try:
    import numpy as np
except ImportError:
    sys.exit("numpy is required:  pip3 install numpy")

import buzzword_engine as be


# --------------------------------------------------------------------------- #
def load(args) -> tuple[be.Index, list[dict], np.ndarray]:
    try:
        index = be.Index()
    except FileNotFoundError as exc:
        sys.exit(f"the index is missing ({exc}).\nBuild it with:  python3 build_index.py --txt-dir DIR")
    terms = be.build_terms(added=getattr(args, "add", None) or [],
                           only_added=getattr(args, "only_added", False),
                           tier=getattr(args, "tier", "puffery"))
    if not terms:
        sys.exit("no terms to score with")
    result = index.score(terms)
    for label, message in (result["errors"] or {}).items():
        print(f"warning: term {label!r} was skipped — {message}", file=sys.stderr)
    return index, terms, result["counts"]


def company_table() -> dict[str, dict]:
    """Ticker -> name, website and sector, for display and for the lookup box."""
    import os
    path = os.path.join(be.ROOT, "data", "companies.csv")
    out: dict[str, dict] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("symbol"):
                    out[row["symbol"]] = row
    return out


def decorate(rows: list[dict]) -> dict[str, dict]:
    table = company_table()
    for r in rows:
        info = table.get(r["symbol"], {})
        r["company"] = info.get("company") or r.get("company") or ""
        r["website"] = info.get("website", "")
        r["sector"] = info.get("sector", "")
    return {s: (t.get("website") or "") for s, t in table.items()}


def resolve(query: str, rows: list[dict], sites: dict[str, str]) -> dict | None:
    """Find a company by ticker, by name, or by website."""
    q = query.strip().lower()
    def rank(r):
        sym, name, site = r["symbol"].lower(), (r["company"] or "").lower(), sites.get(r["symbol"], "").lower()
        if sym == q: return 0
        if site == q: return 1
        if name == q: return 2
        if sym.startswith(q): return 3
        if name.startswith(q) or site.startswith(q): return 4
        if q in name or q in site: return 5
        return 99
    best = sorted(((rank(r), r) for r in rows), key=lambda x: (x[0], x[1]["symbol"]))
    return best[0][1] if best and best[0][0] < 99 else None


def emit(args, payload, table_fn) -> None:
    if args.json:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        table_fn(payload)


def show_added(stats: list[dict]) -> None:
    if not stats:
        return
    print("\nadded words")
    for s in stats:
        if s.get("error"):
            print(f"  {s['term']:24s} skipped: {s['error']}")
        elif s["already_counted"]:
            print(f"  {s['term']:24s} {s['corpus_hits']:>7,d} occurrences, of which "
                  f"{s['new_hits']:,d} are new and {s['already_counted']:,d} were already "
                  f"counted by a term the lexicon has")
        else:
            print(f"  {s['term']:24s} {s['corpus_hits']:>7,d} occurrences, all new")


# --------------------------------------------------------------------------- #
def cmd_board(args) -> None:
    index, terms, counts = load(args)
    rows = be.aggregate_by_symbol(index, terms, counts, args.sources)
    decorate(rows)
    rows = [r for r in rows if r["total_words"] >= args.min_words]
    rows.sort(key=lambda r: -(r[args.metric] if r[args.metric] != "" else -1))
    payload = {"metric": args.metric, "companies": len(rows),
               "added": be.added_word_stats(index, terms, counts),
               "rows": rows[: args.top]}

    def table(p):
        print(f"{len(rows)} companies over {args.min_words:,} words, ranked by {args.metric}\n")
        print(f"{'#':>3} {'TICKER':<7} {'COMPANY':<30} {'WORDS':>9} {'BUZZ':>7} "
              f"{'PER 1k':>7} {'PUFFERY':>8} {'ADDED':>7}")
        for i, r in enumerate(p["rows"], 1):
            extra = f"{r['extra_per_1000_words']:.2f}" if p["added"] else "–"
            print(f"{i:>3} {r['symbol']:<7} {(r['company'] or r['website'])[:30]:<30} "
                  f"{r['total_words']:>9,d} {r['buzzword_hits']:>7,d} "
                  f"{r['buzzwords_per_1000_words']:>7.1f} {r['puffery_per_1000_words']:>8.2f} {extra:>7}")
        show_added(p["added"])
    emit(args, payload, table)


def cmd_lookup(args) -> None:
    index, terms, counts = load(args)
    rows = be.aggregate_by_symbol(index, terms, counts, args.sources)
    sites = decorate(rows)
    hit = resolve(args.company, rows, sites)
    if hit is None:
        sys.exit(f"no company matches {args.company!r}")

    # Rank only against companies with enough text to be comparable. A 350-word
    # landing page can out-score any real report and mean nothing by it.
    ranked = sorted((r for r in rows if r["total_words"] >= args.min_words),
                    key=lambda r: -r["buzzwords_per_1000_words"])
    rank = ranked.index(hit) + 1 if hit in ranked else None
    payload = {"rank": rank, "of": len(ranked), "min_words": args.min_words,
               "company": hit, "added": be.added_word_stats(index, terms, counts)}

    def table(p):
        c = p["company"]
        print(f"\n{c['symbol']} — {c['company']}" + (f"  ({c['website']})" if c["website"] else ""))
        print(f"{'-' * 64}")
        print(f"  {c['buzzwords_per_1000_words']:>8.1f}  buzzwords per 1 000 words")
        print(f"  {c['buzzword_hits']:>8,d}  buzzwords")
        kinds = "/".join(c.get("doc_kinds") or [])
        print(f"  {c['total_words']:>8,d}  words across {c['n_docs']} {kinds} document(s)")
        if p["rank"]:
            print(f"  {('#' + str(p['rank'])):>8}  of {p['of']} companies")
        else:
            print(f"  {'unranked':>8}  under {p['min_words']:,} words, too short to compare fairly")
        sub = c["buzzwords_per_substance_hit"]
        print(f"  {sub:>8}  buzzwords per hard number" if sub != "" else "")
        print("\n  by tier, per 1 000 words")
        for tier in be.TIER_ORDER:
            print(f"    {tier.replace('_', ' '):<12} {c[tier + '_per_1000_words']:>7.2f}")
        print("\n  terms doing the work")
        for t in c["top_terms"][:15]:
            mark = " *" if t["extra"] else ""
            print(f"    {t['label']:<28} {t['hits']:>6,d}  [{t['tier']}]{mark}")
        print(f"\n  source: {', '.join(c['files'])}")
        show_added(p["added"])
    emit(args, payload, table)


def cmd_find(args) -> None:
    index = be.Index()
    syntax = "regex" if args.regex else "plain"
    try:
        starts, _ = index.find(args.term, syntax)
        examples = be.concordance(index, args.term, syntax, limit=args.examples)
    except (be.PatternTooBig, ValueError) as exc:
        sys.exit(f"cannot use {args.term!r}: {exc}")
    by_symbol: dict[str, int] = {}
    for pos in starts.tolist():
        doc = index.docs[int(np.searchsorted(index.doc_start, pos, "right") - 1)]
        by_symbol[doc["symbol"]] = by_symbol.get(doc["symbol"], 0) + 1
    top = sorted(by_symbol.items(), key=lambda kv: -kv[1])[: args.top]
    payload = {"term": args.term, "total_hits": int(starts.size),
               "companies": len(by_symbol), "top": [{"symbol": s, "hits": n} for s, n in top],
               "examples": examples}

    def table(p):
        print(f"\n{p['term']!r}: {p['total_hits']:,} occurrences across {p['companies']} companies\n")
        for row in p["top"]:
            print(f"  {row['symbol']:<7} {row['hits']:>6,d}")
        if p["examples"]:
            print("\nin context")
            for e in p["examples"]:
                print(f"  {e['symbol']:<7} …{e['before'][-52:]} [{e['hit']}] {e['after'][:52]}…")
    emit(args, payload, table)


def cmd_terms(args) -> None:
    lex = be.load_lexicon()
    payload = {"terms": lex, "tiers": {t: sum(1 for x in lex if x["tier"] == t) for t in be.TIER_ORDER}}

    def table(p):
        for tier in be.TIER_ORDER:
            group = [t for t in p["terms"] if t["tier"] == tier]
            print(f"\n{tier.replace('_', ' ')}  ({len(group)} terms, weight {group[0]['weight']:g})")
            print("  " + ", ".join(t["label"] for t in group))
    emit(args, payload, table)


def cmd_export(args) -> None:
    index, terms, counts = load(args)
    if args.by == "company":
        rows = be.aggregate_by_symbol(index, terms, counts, args.sources)
        decorate(rows)
        for r in rows:
            r["files"] = "; ".join(r["files"])
            r["doc_kinds"] = "; ".join(r["doc_kinds"])
            r["top_terms"] = "; ".join(f"{t['label']}:{t['hits']}" for t in r["top_terms"])
            r.pop("all_terms", None)
    else:
        rows = be.build_rows(index, terms, counts)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {args.out}: {len(rows)} rows, one per {args.by}")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def scoring(p):
        p.add_argument("--add", action="append", metavar="TERM",
                       help="an extra buzzword, repeatable. * is any ending, | is alternatives")
        p.add_argument("--only-added", action="store_true",
                       help="score with the added words alone, ignoring the lexicon")
        p.add_argument("--tier", default="puffery", choices=be.TIER_ORDER,
                       help="which category added words belong to (default puffery)")
        p.add_argument("--sources", default="auto", choices=["auto", "reports", "pages", "all"],
                       help="auto uses a company's report, or its captured web page when it "
                            "published no report (default)")
        p.add_argument("--json", action="store_true")

    b = sub.add_parser("board", help="rank companies")
    scoring(b)
    b.add_argument("--top", type=int, default=25)
    b.add_argument("--min-words", type=int, default=2000,
                   help="skip short ESG data sheets, whose density is unstable")
    b.add_argument("--metric", default="buzzwords_per_1000_words",
                   choices=["buzzwords_per_1000_words", "buzzword_hits", "weighted_per_1000_words",
                            "puffery_per_1000_words", "extra_per_1000_words",
                            "buzzwords_per_substance_hit"])
    b.set_defaults(func=cmd_board)

    l = sub.add_parser("lookup", help="one company in detail")
    scoring(l)
    l.add_argument("company", help="ticker, company name or website")
    l.add_argument("--min-words", type=int, default=2000,
                   help="documents shorter than this are not ranked")
    l.set_defaults(func=cmd_lookup)

    f = sub.add_parser("find", help="where a term occurs, with examples")
    f.add_argument("term")
    f.add_argument("--regex", action="store_true", help="treat the term as a regular expression")
    f.add_argument("--top", type=int, default=15)
    f.add_argument("--examples", type=int, default=10)
    f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_find)

    t = sub.add_parser("terms", help="show the lexicon")
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_terms)

    e = sub.add_parser("export", help="write the full table to CSV")
    scoring(e)
    e.add_argument("out")
    e.add_argument("--by", default="report", choices=["report", "company"])
    e.set_defaults(func=cmd_export)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
