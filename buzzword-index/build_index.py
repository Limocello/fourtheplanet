"""Turn the 450 report texts into a positional token index, once.

Scanning 126 MB of text with a few hundred regexes takes minutes. Doing it once
and storing the result as integers takes milliseconds to query afterwards, which
is what makes an interactive buzzword editor possible.

What gets written into raw/reports/index/:

  vocab.tsv       one row per distinct token: id, token, corpus frequency
  tokens.u32      every token of every report, in order, as uint32 vocabulary
                  ids, all reports concatenated into one array
  postings.u32    the same positions sorted by token id, so every place a given
                  token occurs is one contiguous slice
  postings_off.u32  where each token's slice starts in postings.u32
  docs.csv        one row per report: where its tokens start and end, plus the
                  word count and the substance counters

Tokens are lower-cased. A token is a run of letters (apostrophes and hyphens
allowed inside) or a run of digits. The same text normalisation as
27_buzzword_index.py is used, so counts from the two agree.

Usage
-----
    python3 build_index.py --txt-dir ../raw/reports/txt --txt-dir data/pages_txt

--txt-dir is repeatable. Every .txt file found becomes one document, and its
name gives the ticker and the kind: AAPL__main_37.txt is Apple's main report,
IBM__page_0.txt is a captured web page.
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import os
import re
import sys
import time
import unicodedata
from collections import Counter

import numpy as np

from buzzword_engine import tokenise

ROOT = os.path.dirname(os.path.abspath(__file__))
TXT_DIR = os.path.join(ROOT, "data", "txt")
INDEX_DIR = os.path.join(ROOT, "data", "index")
MASTER = os.path.join(ROOT, "data", "companies.csv")

WORD_RX = re.compile(r"[a-z][a-z'-]*")
DEHYPHEN_RX = re.compile(r"(\w)-\s*\n\s*(\w)")
WS_RX = re.compile(r"\s+")

# Kept identical to 27_buzzword_index.py so the two agree.
UNIT = (
    r"(?:%|percent|pp|tco2e|tco2|co2e|mtco2e|ktco2e|mt|kt|tonnes?|tons?|kg|lbs?|pounds?"
    r"|mwh|gwh|twh|kwh|mw|gw|kw|gj|tj|mj|btu|m3|m³|ml|megalit(?:er|re)s?"
    r"|lit(?:er|re)s?|gallons?|acres?|hectares?|km|miles?|usd|eur|chf|gbp"
    r"|million|billion|trillion|thousand|bn|mn)"
)
NUMBER = r"\d[\d,]*(?:\.\d+)?"
QUANTITY_RX = re.compile(rf"\b{NUMBER}\s?{UNIT}\b|\$\s?{NUMBER}")
YEAR_RX = re.compile(r"\b(?:19|20)\d{2}\b")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = DEHYPHEN_RX.sub(r"\1\2", text)
    text = text.replace("’", "'")
    return WS_RX.sub(" ", text).lower()


def read_tokens(path: str) -> tuple[list[str], dict]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = normalise(fh.read())
    tokens = tokenise(text, barriers=True)
    stats = {
        "n_words": len(WORD_RX.findall(text)),
        "quantity_hits": len(QUANTITY_RX.findall(text)),
        "year_hits": len(YEAR_RX.findall(text)),
    }
    return tokens, stats


_VOCAB: dict[str, int] = {}


def _init(_unused=None) -> None:
    pass


def _init_encode(vocab: dict[str, int]) -> None:
    global _VOCAB
    _VOCAB = vocab


def _pass1(path: str) -> tuple[str, Counter, dict]:
    """Vocabulary pass: return only the token counts, never the token list."""
    tokens, stats = read_tokens(path)
    return path, Counter(tokens), stats


def _pass2(path: str) -> tuple[str, bytes]:
    """Encoding pass: return the document as packed uint32 vocabulary ids."""
    tokens, _ = read_tokens(path)
    return path, np.fromiter(
        (_VOCAB[t] for t in tokens), dtype=np.uint32, count=len(tokens)
    ).tobytes()


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--txt-dir", action="append", metavar="DIR",
                    help="a folder of .txt documents, repeatable")
    ap.add_argument("--index-dir", default=INDEX_DIR)
    ap.add_argument("--jobs", type=int, default=0)
    args = ap.parse_args()

    jobs = max(1, args.jobs or (os.cpu_count() or 1))
    dirs = args.txt_dir or [TXT_DIR]
    paths: list[str] = []
    for d in dirs:
        if not os.path.isdir(d):
            sys.exit(f"no such folder: {d}")
        found = sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".txt"))
        print(f"  {len(found):>4} documents in {d}")
        paths.extend(found)
    if not paths:
        sys.exit("no .txt files found")
    # One document per file name. If the same name turns up in two folders the
    # first one given wins, so put the better source first.
    seen: dict[str, str] = {}
    for path in paths:
        seen.setdefault(os.path.basename(path), path)
    files = sorted(seen)
    paths = [seen[f] for f in files]
    os.makedirs(args.index_dir, exist_ok=True)
    print(f"{len(files)} documents, {jobs} process(es)", flush=True)

    t0 = time.time()
    freq: Counter = Counter()
    stats: dict[str, dict] = {}
    with mp.Pool(jobs, initializer=_init) as pool:
        for n, (path, counts, st) in enumerate(
            pool.imap_unordered(_pass1, paths, chunksize=4), 1
        ):
            freq.update(counts)
            stats[os.path.basename(path)] = st
            if n % 100 == 0 or n == len(files):
                print(f"  vocabulary {n}/{len(files)}", flush=True)
    print(f"  {len(freq):,} distinct tokens in {time.time() - t0:.1f}s", flush=True)

    # Most frequent token gets id 0. Ids then roughly follow frequency, which
    # keeps the common-word slices of postings.u32 next to each other.
    ordered = [tok for tok, _ in freq.most_common()]
    vocab = {tok: i for i, tok in enumerate(ordered)}

    t1 = time.time()
    encoded: dict[str, bytes] = {}
    with mp.Pool(jobs, initializer=_init_encode, initargs=(vocab,)) as pool:
        for n, (path, blob) in enumerate(pool.imap_unordered(_pass2, paths, chunksize=4), 1):
            encoded[os.path.basename(path)] = blob
            if n % 100 == 0 or n == len(files):
                print(f"  encoding {n}/{len(files)}", flush=True)
    print(f"  encoded in {time.time() - t1:.1f}s", flush=True)

    names = load_company_names(MASTER)
    doc_rows: list[dict] = []
    offset = 0
    parts: list[np.ndarray] = []
    for doc_id, filename in enumerate(files):
        arr = np.frombuffer(encoded[filename], dtype=np.uint32)
        parts.append(arr)
        symbol, kind, num = parse_name(filename)
        st = stats[filename]
        doc_rows.append(
            {
                "doc_id": doc_id,
                "file": filename,
                "symbol": symbol,
                "company": names.get(symbol, ""),
                "doc_kind": kind,
                "doc_num": num,
                "token_offset": offset,
                "n_tokens": int(arr.size),
                "n_words": st["n_words"],
                "quantity_hits": st["quantity_hits"],
                "year_hits": st["year_hits"],
            }
        )
        offset += int(arr.size)

    tokens = np.concatenate(parts) if parts else np.zeros(0, dtype=np.uint32)
    del parts, encoded
    print(f"  {tokens.size:,} tokens total ({tokens.nbytes / 1e6:.0f} MB)", flush=True)

    # Positional postings: every position sorted by the token sitting there, so
    # all occurrences of one token are a single contiguous slice.
    t2 = time.time()
    postings = np.argsort(tokens, kind="stable").astype(np.uint32)
    counts = np.bincount(tokens, minlength=len(vocab)).astype(np.uint64)
    offsets = np.zeros(len(vocab) + 1, dtype=np.uint64)
    np.cumsum(counts, out=offsets[1:])
    print(f"  postings built in {time.time() - t2:.1f}s", flush=True)

    tokens.tofile(os.path.join(args.index_dir, "tokens.u32"))
    postings.tofile(os.path.join(args.index_dir, "postings.u32"))
    offsets.astype(np.uint64).tofile(os.path.join(args.index_dir, "postings_off.u64"))
    with open(os.path.join(args.index_dir, "vocab.tsv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["id", "token", "corpus_freq"])
        for i, tok in enumerate(ordered):
            w.writerow([i, tok, freq[tok]])
    with open(os.path.join(args.index_dir, "docs.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(doc_rows[0].keys()))
        w.writeheader()
        w.writerows(doc_rows)

    total_mb = sum(
        os.path.getsize(os.path.join(args.index_dir, f))
        for f in os.listdir(args.index_dir)
    ) / 1e6
    kinds: dict[str, int] = {}
    for row in doc_rows:
        kinds[row["doc_kind"]] = kinds.get(row["doc_kind"], 0) + 1
    print(f"\nindex in {args.index_dir}: {total_mb:.0f} MB, built in {time.time() - t0:.1f}s")
    print("  documents by kind: " + ", ".join(f"{k or '?'} {v}" for k, v in sorted(kinds.items())))


if __name__ == "__main__":
    main()
