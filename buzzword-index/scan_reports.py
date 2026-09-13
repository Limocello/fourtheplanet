"""Buzzword / peacock index for the sustainability reports.

Reads the plain text already extracted from every report PDF (raw/reports/txt/,
produced alongside raw/reports/pdf/) and counts how much of each report is
corporate and sustainability boilerplate.

The term list lives in buzzword_lexicon.tsv, one row per term:

    label    tier    weight   pattern

label    the name that appears in the output
tier     puffery (unverifiable self-praise), hedge (commitment without
         commitment),
         corporate (management speak), esg_jargon (sustainability vocabulary)
weight   how heavily a hit of this tier counts in the weighted score
pattern  a regular expression fragment. Word boundaries are added around it.
         A space in the pattern matches one space, because the report text is
         whitespace-normalised before matching.

Edit the TSV to add, drop or reweight terms. No code change is needed.

Counting rules
--------------
Matches may overlap in the raw text, for example "sustainable" sits inside
"sustainable future". Every overlap is resolved in favour of the longest match,
so each stretch of text is counted exactly once and the total can never be
inflated by writing a more specific term next to a general one.

Output
------
raw/reports/buzzword_index.csv, one row per report, metrics in the columns.
Nothing is written back into the master table.

Scored reports are cached in raw/reports/.buzzword_cache.json, keyed by a hash
of the lexicon. Running the script again only scores reports that are not in the
cache, so an interrupted run continues where it stopped. Editing the lexicon
changes the hash and forces a full rescore, which is what you want.

Usage
-----
    python3 27_buzzword_index.py [--limit N] [--jobs N] [--max-seconds N]
                                 [--txt-dir DIR] [--out FILE] [--no-cache]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import multiprocessing as mp
import re
import sys
import time
import unicodedata
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
TXT_DIR = os.path.join(ROOT, "data", "txt")
PDF_DIR = os.path.join(ROOT, "data", "pdf")
LEXICON = os.path.join(ROOT, "buzzword_lexicon.tsv")
MASTER = os.path.join(ROOT, "data", "companies.csv")
OUT = os.path.join(ROOT, "data", "buzzword_index_scanned.csv")
CACHE = os.path.join(ROOT, "data", ".buzzword_cache.json")

TIER_ORDER = ["puffery", "hedge", "corporate", "esg_jargon"]

# All matching runs on a lower-cased copy of the text, so the patterns are
# lower-cased too and no expression needs the IGNORECASE flag. On a corpus this
# size that is worth several minutes.

# A word for the purposes of the denominator: a run of letters, so page numbers
# and table values do not dilute the density.
WORD_RX = re.compile(r"[a-z][a-z'-]*")

# Secondary "substance" metrics: how much hard reporting sits next to the prose.
UNIT = (
    r"(?:%|percent|pp|tCO2e|tCO2|CO2e|MtCO2e|ktCO2e|Mt|kt|tonnes?|tons?|kg|lbs?|pounds?"
    r"|MWh|GWh|TWh|kWh|MW|GW|kW|GJ|TJ|MJ|BTU|m3|m³|ML|megalit(?:er|re)s?"
    r"|lit(?:er|re)s?|gallons?|acres?|hectares?|km|miles?|USD|EUR|CHF|GBP"
    r"|million|billion|trillion|thousand|bn|mn)"
)
NUMBER = r"\d[\d,]*(?:\.\d+)?"
QUANTITY_RX = re.compile(rf"\b{NUMBER}\s?{UNIT.lower()}\b|\$\s?{NUMBER}")
YEAR_RX = re.compile(r"\b(?:19|20)\d{2}\b")

# Hyphen broken across a line break in the PDF text, e.g. "sustain-\nability".
DEHYPHEN_RX = re.compile(r"(\w)-\s*\n\s*(\w)")
WS_RX = re.compile(r"\s+")

GROUPS_PER_REGEX = 90


def load_lexicon(path: str) -> list[dict]:
    terms: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            if not row.get("label") or row["label"].startswith("#"):
                continue
            terms.append(
                {
                    "label": row["label"].strip(),
                    "tier": row["tier"].strip(),
                    "weight": float(row["weight"]),
                    "pattern": row["pattern"].strip(),
                }
            )
    if not terms:
        sys.exit(f"no terms found in {path}")
    for term in terms:
        if term["tier"] not in TIER_ORDER:
            sys.exit(f"unknown tier {term['tier']!r} for term {term['label']!r}")
        # Lower-casing a pattern must not turn \B into \b, \D into \d and so on.
        if re.search(r"\\[A-Z]", term["pattern"]):
            sys.exit(
                f"pattern for {term['label']!r} uses an upper-case escape class; "
                "write it in lower case, matching is case-insensitive anyway"
            )
        term["pattern"] = term["pattern"].lower()
    return terms


def build_matchers(terms: list[dict]) -> list[re.Pattern[str]]:
    """One combined regex per chunk of terms, each alternative a named group.

    Longer patterns go first so that inside a single regex the more specific
    alternative wins at a given position. Overlaps between chunks are resolved
    later, globally.
    """
    order = sorted(range(len(terms)), key=lambda i: -len(terms[i]["pattern"]))
    matchers: list[re.Pattern[str]] = []
    for start in range(0, len(order), GROUPS_PER_REGEX):
        chunk = order[start : start + GROUPS_PER_REGEX]
        parts = []
        for idx in chunk:
            try:
                re.compile(terms[idx]["pattern"])
            except re.error as exc:
                sys.exit(f"bad pattern for {terms[idx]['label']!r}: {exc}")
            parts.append(rf"(?P<t{idx}>\b(?:{terms[idx]['pattern']})\b)")
        matchers.append(re.compile("|".join(parts)))
    return matchers


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = DEHYPHEN_RX.sub(r"\1\2", text)
    text = text.replace("\u2019", "'")
    return WS_RX.sub(" ", text).lower()


def count_terms(text: str, matchers: list[re.Pattern[str]]) -> Counter:
    """Count non-overlapping hits, longest match wins wherever they collide."""
    spans: list[tuple[int, int, int]] = []
    for matcher in matchers:
        for match in matcher.finditer(text):
            name = match.lastgroup
            if name is None:
                continue
            spans.append((match.start(), match.end(), int(name[1:])))

    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    counts: Counter = Counter()
    cursor = -1
    for start, end, idx in spans:
        if start < cursor:
            continue
        counts[idx] += 1
        cursor = end
    return counts


_W: dict = {}


def _init_worker(txt_dir: str, terms: list[dict], names: dict[str, str]) -> None:
    _W["txt_dir"] = txt_dir
    _W["terms"] = terms
    _W["names"] = names
    _W["matchers"] = build_matchers(terms)


def _score_file(filename: str) -> dict:
    terms = _W["terms"]
    with open(os.path.join(_W["txt_dir"], filename), encoding="utf-8", errors="replace") as fh:
        text = normalise(fh.read())

    words = len(WORD_RX.findall(text))
    counts = count_terms(text, _W["matchers"])

    total = sum(counts.values())
    weighted = sum(count * terms[i]["weight"] for i, count in counts.items())
    tier_hits = {tier: 0 for tier in TIER_ORDER}
    for i, count in counts.items():
        tier_hits[terms[i]["tier"]] += count

    quantities = len(QUANTITY_RX.findall(text))
    years = len(YEAR_RX.findall(text))
    substance = quantities + years

    top = sorted(
        ((terms[i]["label"], c) for i, c in counts.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )[:10]

    symbol, kind, doc_id = parse_name(filename)
    return {
        "file": filename,
        "symbol": symbol,
        "company": _W["names"].get(symbol, ""),
        "doc_kind": kind,
        "doc_id": doc_id,
        "total_words": words,
        # Density on a very short document swings wildly: a single extra
        # "sustainable" in a 250-word data sheet moves it by 4 per 1000. Rank
        # short documents separately, or filter them out.
        "short_doc": 1 if words < 2000 else 0,
        "buzzword_hits": total,
        "buzzwords_per_1000_words": per_thousand(total, words),
        "buzzword_score_weighted": round(weighted, 1),
        "weighted_per_1000_words": per_thousand(weighted, words),
        "unique_buzzwords": len(counts),
        "puffery_hits": tier_hits["puffery"],
        "hedge_hits": tier_hits["hedge"],
        "corporate_hits": tier_hits["corporate"],
        "esg_jargon_hits": tier_hits["esg_jargon"],
        "puffery_per_1000_words": per_thousand(tier_hits["puffery"], words),
        "hedge_per_1000_words": per_thousand(tier_hits["hedge"], words),
        "corporate_per_1000_words": per_thousand(tier_hits["corporate"], words),
        "esg_jargon_per_1000_words": per_thousand(tier_hits["esg_jargon"], words),
        "quantity_hits": quantities,
        "year_hits": years,
        "substance_hits": substance,
        "substance_per_1000_words": per_thousand(substance, words),
        "buzzwords_per_substance_hit": round(total / substance, 3) if substance else "",
        "top_terms": "; ".join(f"{label}:{c}" for label, c in top),
    }


def parse_name(filename: str) -> tuple[str, str, str]:
    stem = os.path.splitext(filename)[0]
    symbol, _, rest = stem.partition("__")
    kind, _, doc_id = rest.partition("_")
    return symbol, kind or "", doc_id or ""


def load_company_names(path: str) -> dict[str, str]:
    names: dict[str, str] = {}
    if not os.path.exists(path):
        return names
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            symbol = (row.get("symbol") or row.get("Symbol") or "").strip()
            if symbol and symbol not in names:
                names[symbol] = (row.get("company") or "").strip()
    return names


def per_thousand(hits: float, words: int) -> float:
    return round(1000.0 * hits / words, 3) if words else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--txt-dir", default=TXT_DIR)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--lexicon", default=LEXICON)
    ap.add_argument("--limit", type=int, default=0, help="only the first N reports")
    ap.add_argument("--jobs", type=int, default=0, help="parallel processes (default: all cores)")
    ap.add_argument("--max-seconds", type=int, default=0, help="stop scoring after N seconds and save progress")
    ap.add_argument("--no-cache", action="store_true", help="ignore and do not write the cache")
    args = ap.parse_args()

    terms = load_lexicon(args.lexicon)
    matchers = build_matchers(terms)
    names = load_company_names(MASTER)
    print(f"{len(terms)} terms in {len(matchers)} combined patterns", flush=True)

    files = sorted(f for f in os.listdir(args.txt_dir) if f.endswith(".txt"))
    if args.limit:
        files = files[: args.limit]

    lexicon_key = hashlib.sha256(
        "\n".join(f"{t['label']}|{t['tier']}|{t['weight']}|{t['pattern']}" for t in terms).encode()
    ).hexdigest()[:16]

    cached: dict[str, dict] = {}
    if not args.no_cache and os.path.exists(CACHE):
        try:
            blob = json.load(open(CACHE, encoding="utf-8"))
            if blob.get("lexicon_key") == lexicon_key:
                cached = blob.get("rows", {})
            else:
                print("lexicon changed since the last run, rescoring everything", flush=True)
        except (json.JSONDecodeError, OSError):
            cached = {}

    todo = [f for f in files if f not in cached]
    jobs = max(1, args.jobs or (os.cpu_count() or 1))
    print(
        f"{len(files)} reports, {len(files) - len(todo)} already scored, "
        f"{len(todo)} to do on {jobs} process(es)",
        flush=True,
    )

    started = time.time()
    stopped_early = False

    def remember(row: dict) -> None:
        cached[row["file"]] = row

    def save_cache() -> None:
        if args.no_cache:
            return
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"lexicon_key": lexicon_key, "rows": cached}, fh)
        os.replace(tmp, CACHE)

    if todo:
        if jobs == 1:
            _init_worker(args.txt_dir, terms, names)
            for n, filename in enumerate(todo, 1):
                remember(_score_file(filename))
                if n % 25 == 0 or n == len(todo):
                    print(f"  {n}/{len(todo)}", flush=True)
                    save_cache()
                if args.max_seconds and time.time() - started > args.max_seconds:
                    stopped_early = True
                    break
        else:
            with mp.Pool(jobs, initializer=_init_worker, initargs=(args.txt_dir, terms, names)) as pool:
                results = pool.imap_unordered(_score_file, todo, chunksize=1)
                for n, row in enumerate(results, 1):
                    remember(row)
                    if n % 25 == 0 or n == len(todo):
                        print(f"  {n}/{len(todo)}", flush=True)
                        save_cache()
                    if args.max_seconds and time.time() - started > args.max_seconds:
                        stopped_early = True
                        pool.terminate()
                        break
        save_cache()

    rows = [cached[f] for f in files if f in cached]
    if not rows:
        sys.exit("nothing scored")

    # Percentile rank of the headline density within this corpus, 0 = least
    # buzzwordy report, 100 = most. Only meaningful relative to the other 449.
    ranked = sorted(rows, key=lambda r: r["buzzwords_per_1000_words"])
    last = len(ranked) - 1
    for position, row in enumerate(ranked):
        row["density_percentile"] = round(100.0 * position / last, 1) if last else 0.0

    rows.sort(key=lambda r: -r["buzzwords_per_1000_words"])

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    missing = 0
    if os.path.isdir(PDF_DIR) and not args.limit:
        have = {os.path.splitext(f)[0] for f in files}
        missing = sum(
            1 for f in os.listdir(PDF_DIR)
            if f.endswith(".pdf") and os.path.splitext(f)[0] not in have
        )

    print(f"\nwrote {args.out}: {len(rows)} reports")
    if stopped_early or len(rows) < len(files):
        print(
            f"stopped with {len(files) - len(rows)} report(s) left; "
            "run the same command again to continue"
        )
    if missing:
        print(f"note: {missing} PDF(s) in raw/reports/pdf have no extracted text and were skipped")


if __name__ == "__main__":
    main()
