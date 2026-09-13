"""Score the whole report corpus from the token index, in well under a second.

27_buzzword_index.py scans the text. This scans nothing: it looks up where each
word already occurs. The index (28_build_token_index.py) stores every report as
a sequence of vocabulary ids plus, for every word in the vocabulary, the list of
every position it occupies. Counting a phrase is then: take the rarest word in
it, look up its positions, and check the neighbouring ids. That is a handful of
array operations over a few thousand positions instead of a regex walk over
126 MB, which is why adding a word in the browser feels instant.

Patterns
--------
The lexicon keeps the same regex column as before. Every pattern in it is
finite: literals, alternations, optional groups, and small character classes.
Such a pattern describes a limited set of concrete strings, so at load time each
pattern is expanded into that set, and each string is run through the same
tokeniser as the corpus. A term therefore becomes a handful of token sequences.

Negative lookarounds survive the trip: (?<!for the )purposes?(?! of) becomes the
token sequence ["purpose"] carrying "must not be preceded by for the" and "must
not be followed by of".

Unbounded repeats (*, +, {2,}) and . are rejected in the regex column, because
they describe infinitely many strings. Write the alternatives out instead.

The second syntax is "plain", what someone types in the browser. It is not a
regex. Words are separated by spaces, "*" stands for any ending, and "|" offers
alternatives for one word:

    steward*            steward, stewards, stewardship, stewarding …
    strive|aspire to    "strive to" and "aspire to"
    net zero            "net zero" and, because a hyphen is a separator, "net-zero"

A "*" is answered from the vocabulary, not by scanning: the index already knows
every distinct word in the corpus, so the endings are looked up once.

Overlap
-------
As in the scanning version, wherever two terms match overlapping text the longer
match wins, so each stretch of a report is counted exactly once.
"""

from __future__ import annotations

import csv
import itertools
import os
import re
import unicodedata

import numpy as np

try:  # Python 3.11+
    from re import _constants as sre_constants
    from re import _parser as sre_parse
except ImportError:  # pragma: no cover
    import sre_constants  # type: ignore
    import sre_parse  # type: ignore

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX_DIR = os.path.join(ROOT, "data", "index")
LEXICON = os.path.join(ROOT, "buzzword_lexicon.tsv")

TIER_ORDER = ["peacock", "hedge", "corporate", "esg_jargon"]
DEFAULT_WEIGHTS = {"peacock": 3.0, "hedge": 2.0, "corporate": 2.0, "esg_jargon": 1.0}

# A token is a maximal run of letters and digits, which is exactly what \b in a
# regular expression treats as one word. That matters for the running headers
# PDF extraction leaves behind: "FY24ESG" stays a single token, so a search for
# "esg" does not find one inside it, just as \besg\b would not.
TOKEN_RX = re.compile(r"[a-z0-9_]+")

# Characters that merely join two words: whitespace, every kind of dash, the
# apostrophe and the ampersand. "net-zero" and "net zero" become the same pair
# of tokens, "we're" becomes we + re, "DE&I" becomes de + i.
GAP_OK = set(" \t\n\r\f\v-'\u2010\u2011\u2012\u2013\u2014\u2015\u2019&")

# Anything else between two tokens — a full stop, comma, slash, bracket, bullet,
# table cell edge — is a barrier, and gets a token of its own so that a phrase
# can never be matched across it. Without it the line "positive. Impact on
# water" would be counted as a hit for "positive impact".
BARRIER = "@@break@@"  # cannot collide: the tokeniser never produces "@"

MAX_VARIANTS = 4000


class PatternTooBig(ValueError):
    pass


# --------------------------------------------------------------------------- #
# regex -> concrete strings
# --------------------------------------------------------------------------- #
def _expand_nodes(nodes) -> tuple[list[str], list[str], list[str]]:
    """Return (strings, negative-lookbehinds, negative-lookaheads)."""
    pieces: list[list[str]] = []
    before: list[str] = []
    after: list[str] = []

    for op, av in nodes:
        name = str(op)
        if op is sre_constants.LITERAL:
            pieces.append([chr(av)])
        elif op is sre_constants.IN:
            options: list[str] = []
            for sub_op, sub_av in av:
                if sub_op is sre_constants.LITERAL:
                    options.append(chr(sub_av))
                elif sub_op is sre_constants.RANGE:
                    lo, hi = sub_av
                    if hi - lo > 64:
                        raise PatternTooBig(f"character range [{chr(lo)}-{chr(hi)}] is too wide")
                    options.extend(chr(c) for c in range(lo, hi + 1))
                else:
                    raise PatternTooBig(f"unsupported character class element {sub_op}")
            pieces.append(options)
        elif op is sre_constants.BRANCH:
            options = []
            for branch in av[1]:
                strings, nb, na = _expand_nodes(branch)
                before.extend(nb)
                after.extend(na)
                options.extend(strings)
            pieces.append(options)
        elif op is sre_constants.SUBPATTERN:
            strings, nb, na = _expand_nodes(av[3])
            before.extend(nb)
            after.extend(na)
            pieces.append(strings)
        elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
            lo, hi, sub = av
            if hi > 1:
                raise PatternTooBig(
                    "unbounded or repeated group; write the alternatives out instead"
                )
            strings, nb, na = _expand_nodes(sub)
            before.extend(nb)
            after.extend(na)
            pieces.append(([""] if lo == 0 else []) + strings)
        elif op is sre_constants.AT:
            continue  # \b, ^, $ — token matching already respects word edges
        elif op is sre_constants.ASSERT_NOT:
            direction, sub = av
            strings, _, _ = _expand_nodes(sub)
            (before if direction < 0 else after).extend(strings)
        elif op is sre_constants.ASSERT:
            raise PatternTooBig("positive lookaround is not supported")
        else:
            raise PatternTooBig(f"unsupported regex construct {name}")

        total = 1
        for piece in pieces:
            total *= max(1, len(piece))
            if total > MAX_VARIANTS:
                raise PatternTooBig(f"pattern expands to more than {MAX_VARIANTS} strings")

    strings = ["".join(combo) for combo in itertools.product(*pieces)] if pieces else [""]
    return strings, before, after


def expand_pattern(pattern: str) -> tuple[list[str], list[str], list[str]]:
    return _expand_nodes(sre_parse.parse(pattern.lower()))


def tokenise(text: str, barriers: bool = False) -> list[str]:
    """Split text into tokens. With barriers=True, insert BARRIER at punctuation.

    Patterns are tokenised without barriers, the corpus with them.
    """
    text = unicodedata.normalize("NFKC", text).replace("\u2019", "'").lower()
    if not barriers:
        return TOKEN_RX.findall(text)

    out: list[str] = []
    prev_end = 0
    for match in TOKEN_RX.finditer(text):
        if out:
            gap = text[prev_end : match.start()]
            if any(ch not in GAP_OK for ch in gap):
                out.append(BARRIER)
        out.append(match.group())
        prev_end = match.end()
    return out


# --------------------------------------------------------------------------- #
# lexicon
# --------------------------------------------------------------------------- #
def load_lexicon(path: str = LEXICON) -> list[dict]:
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
                    "syntax": (row.get("syntax") or "regex").strip() or "regex",
                }
            )
    return terms


def save_lexicon(terms: list[dict], path: str = LEXICON) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["label", "tier", "weight", "pattern", "syntax"])
        for t in terms:
            w.writerow([t["label"], t["tier"], t["weight"], t["pattern"],
                        t.get("syntax", "regex")])


MAX_WILDCARD_WORDS = 20000


def plain_word_regex(word: str) -> re.Pattern[str]:
    """One word of a plain query as an expression to run against the vocabulary."""
    parts = []
    for ch in word:
        parts.append(r"[a-z0-9]*" if ch == "*" else re.escape(ch))
    return re.compile("".join(parts) + r"\Z")


def split_plain(text: str) -> list[list[str]]:
    """Plain query -> one list of alternatives per word position."""
    text = unicodedata.normalize("NFKC", text).replace("\u2019", "'").lower()
    words = [w for w in re.split(r"[^a-z0-9*|]+", text) if w]
    return [[alt for alt in word.split("|") if alt] for word in words]


# --------------------------------------------------------------------------- #
# the index
# --------------------------------------------------------------------------- #
class Index:
    def __init__(self, index_dir: str = INDEX_DIR) -> None:
        self.dir = index_dir
        self.tokens = np.fromfile(os.path.join(index_dir, "tokens.u32"), dtype=np.uint32)
        self.postings = np.fromfile(os.path.join(index_dir, "postings.u32"), dtype=np.uint32)
        self.post_off = np.fromfile(os.path.join(index_dir, "postings_off.u64"), dtype=np.uint64)

        self.vocab: dict[str, int] = {}
        self.freq: dict[str, int] = {}
        with open(os.path.join(index_dir, "vocab.tsv"), encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                self.vocab[row["token"]] = int(row["id"])
                self.freq[row["token"]] = int(row["corpus_freq"])

        self.docs: list[dict] = []
        with open(os.path.join(index_dir, "docs.csv"), encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                for key in ("doc_id", "token_offset", "n_tokens", "n_words",
                            "quantity_hits", "year_hits"):
                    row[key] = int(row[key])
                self.docs.append(row)
        self.doc_start = np.array([d["token_offset"] for d in self.docs], dtype=np.int64)
        self.n_words = np.array([d["n_words"] for d in self.docs], dtype=np.int64)
        self._variant_cache: dict[str, list] = {}

    # -- pattern -> token sequences ---------------------------------------- #
    def _plain_variants(self, text: str) -> list[dict]:
        """A plain query becomes one variant whose positions may accept many ids."""
        positions: list[np.ndarray] = []
        for alternatives in split_plain(text):
            ids: set[int] = set()
            for alt in alternatives:
                if "*" in alt:
                    rx = plain_word_regex(alt)
                    hits = [i for tok, i in self.vocab.items() if rx.match(tok)]
                    if len(hits) > MAX_WILDCARD_WORDS:
                        raise PatternTooBig(
                            f"'{alt}' matches {len(hits):,} different words; be more specific"
                        )
                    ids.update(hits)
                elif alt in self.vocab:
                    ids.add(self.vocab[alt])
            if not ids:
                return []  # a word that never occurs makes the phrase impossible
            positions.append(np.array(sorted(ids), dtype=np.uint32))
        if not positions:
            return []
        sizes = [sum(int(self.post_off[i + 1] - self.post_off[i]) for i in p.tolist())
                 for p in positions]
        return [{"positions": positions, "anchor": int(np.argmin(sizes)),
                 "before": [], "after": []}]

    def compile_term(self, pattern: str, syntax: str = "regex") -> list[dict]:
        """Token sequences for a term, dropping any that cannot occur."""
        key = f"{syntax}\x00{pattern}"
        if key in self._variant_cache:
            return self._variant_cache[key]
        if syntax == "plain":
            variants = self._plain_variants(pattern)
            self._variant_cache[key] = variants
            return variants

        strings, neg_before, neg_after = expand_pattern(pattern)
        before_seqs = [tokenise(s) for s in neg_before]
        after_seqs = [tokenise(s) for s in neg_after]

        seen: set[tuple[int, ...]] = set()
        variants: list[dict] = []
        for string in strings:
            toks = tokenise(string)
            if not toks or any(t not in self.vocab for t in toks):
                continue
            ids = tuple(self.vocab[t] for t in toks)
            if ids in seen:
                continue
            seen.add(ids)
            rarest = min(range(len(toks)), key=lambda i: self.freq[toks[i]])
            variants.append(
                {
                    "positions": [np.array([i], dtype=np.uint32) for i in ids],
                    "anchor": rarest,
                    "before": [
                        np.array([self.vocab[t] for t in seq], dtype=np.uint32)
                        for seq in before_seqs
                        if seq and all(t in self.vocab for t in seq)
                    ],
                    "after": [
                        np.array([self.vocab[t] for t in seq], dtype=np.uint32)
                        for seq in after_seqs
                        if seq and all(t in self.vocab for t in seq)
                    ],
                }
            )
        self._variant_cache[key] = variants
        return variants

    def positions_of(self, token_id: int) -> np.ndarray:
        lo, hi = int(self.post_off[token_id]), int(self.post_off[token_id + 1])
        return self.postings[lo:hi]

    # -- matching ----------------------------------------------------------- #
    def find(self, pattern: str, syntax: str = "regex") -> tuple[np.ndarray, np.ndarray]:
        """Start position and length of every occurrence, corpus-wide."""
        starts_all: list[np.ndarray] = []
        lengths_all: list[np.ndarray] = []
        n = self.tokens.size

        for variant in self.compile_term(pattern, syntax):
            positions = variant["positions"]
            length = len(positions)
            anchor = variant["anchor"]
            anchor_ids = positions[anchor]
            if anchor_ids.size == 1:
                starts = self.positions_of(int(anchor_ids[0])).astype(np.int64)
            else:
                starts = np.concatenate(
                    [self.positions_of(int(i)) for i in anchor_ids.tolist()]
                ).astype(np.int64)
                starts.sort()
            starts -= anchor
            if starts.size == 0:
                continue
            starts = starts[(starts >= 0) & (starts + length <= n)]
            for j in range(length):
                if j == anchor or starts.size == 0:
                    continue
                here = self.tokens[starts + j]
                wanted = positions[j]
                starts = starts[
                    (here == wanted[0]) if wanted.size == 1 else np.isin(here, wanted)
                ]
            if starts.size == 0:
                continue

            # A match may not straddle two reports.
            same_doc = np.searchsorted(self.doc_start, starts, "right") == np.searchsorted(
                self.doc_start, starts + length - 1, "right"
            )
            starts = starts[same_doc]

            for seq in variant["before"]:
                if starts.size == 0:
                    break
                k = seq.size
                bad = starts >= k
                probe = starts[bad]
                hit = np.ones(probe.size, dtype=bool)
                for j in range(k):
                    hit &= self.tokens[probe - k + j] == seq[j]
                drop = np.zeros(starts.size, dtype=bool)
                drop[bad] = hit
                starts = starts[~drop]

            for seq in variant["after"]:
                if starts.size == 0:
                    break
                k = seq.size
                ok = starts + length + k <= n
                probe = starts[ok]
                hit = np.ones(probe.size, dtype=bool)
                for j in range(k):
                    hit &= self.tokens[probe + length + j] == seq[j]
                drop = np.zeros(starts.size, dtype=bool)
                drop[ok] = hit
                starts = starts[~drop]

            if starts.size:
                starts_all.append(starts)
                lengths_all.append(np.full(starts.size, length, dtype=np.int64))

        if not starts_all:
            return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
        return np.concatenate(starts_all), np.concatenate(lengths_all)

    # -- scoring ------------------------------------------------------------ #
    def score(self, terms: list[dict]) -> dict:
        """Per-report counts for every term, longest match winning any overlap."""
        starts_list, lengths_list, term_list = [], [], []
        errors: dict[str, str] = {}

        for idx, term in enumerate(terms):
            try:
                starts, lengths = self.find(term["pattern"], term.get("syntax", "regex"))
            except (PatternTooBig, re.error) as exc:
                errors[term["label"]] = str(exc)
                continue
            if starts.size:
                starts_list.append(starts)
                lengths_list.append(lengths)
                term_list.append(np.full(starts.size, idx, dtype=np.int64))

        n_docs = len(self.docs)
        n_terms = len(terms)
        counts = np.zeros((n_docs, n_terms), dtype=np.int64)

        if starts_list:
            starts = np.concatenate(starts_list)
            lengths = np.concatenate(lengths_list)
            term_ids = np.concatenate(term_list)
            order = np.lexsort((-lengths, starts))
            s = starts[order].tolist()
            e = (starts + lengths)[order].tolist()
            t = term_ids[order].tolist()

            keep_start, keep_term = [], []
            cursor = -1
            for i in range(len(s)):
                if s[i] < cursor:
                    continue
                keep_start.append(s[i])
                keep_term.append(t[i])
                cursor = e[i]

            if keep_start:
                doc_of = np.searchsorted(self.doc_start, np.array(keep_start, dtype=np.int64), "right") - 1
                np.add.at(counts, (doc_of, np.array(keep_term, dtype=np.int64)), 1)

        return {"counts": counts, "errors": errors}


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #
def per_thousand(hits: float, words: int) -> float:
    return round(1000.0 * hits / words, 3) if words else 0.0


def build_rows(index: Index, terms: list[dict], counts: np.ndarray) -> list[dict]:
    weights = np.array([t["weight"] for t in terms], dtype=np.float64)
    tier_of = {tier: np.array([t["tier"] == tier for t in terms]) for tier in TIER_ORDER}

    totals = counts.sum(axis=1)
    weighted = counts @ weights
    tier_totals = {tier: counts[:, mask].sum(axis=1) for tier, mask in tier_of.items()}
    uniques = (counts > 0).sum(axis=1)

    rows: list[dict] = []
    for i, doc in enumerate(index.docs):
        words = doc["n_words"]
        total = int(totals[i])
        substance = doc["quantity_hits"] + doc["year_hits"]
        order = np.argsort(-counts[i])[:10]
        top = [(terms[j]["label"], int(counts[i, j])) for j in order if counts[i, j] > 0]
        rows.append(
            {
                "file": doc["file"],
                "symbol": doc["symbol"],
                "company": doc["company"],
                "doc_kind": doc["doc_kind"],
                "doc_id": doc["doc_num"],
                "total_words": words,
                "short_doc": 1 if words < 2000 else 0,
                "buzzword_hits": total,
                "buzzwords_per_1000_words": per_thousand(total, words),
                "buzzword_score_weighted": round(float(weighted[i]), 1),
                "weighted_per_1000_words": per_thousand(float(weighted[i]), words),
                "unique_buzzwords": int(uniques[i]),
                "peacock_hits": int(tier_totals["peacock"][i]),
                "hedge_hits": int(tier_totals["hedge"][i]),
                "corporate_hits": int(tier_totals["corporate"][i]),
                "esg_jargon_hits": int(tier_totals["esg_jargon"][i]),
                "peacock_per_1000_words": per_thousand(int(tier_totals["peacock"][i]), words),
                "hedge_per_1000_words": per_thousand(int(tier_totals["hedge"][i]), words),
                "corporate_per_1000_words": per_thousand(int(tier_totals["corporate"][i]), words),
                "esg_jargon_per_1000_words": per_thousand(int(tier_totals["esg_jargon"][i]), words),
                "quantity_hits": doc["quantity_hits"],
                "year_hits": doc["year_hits"],
                "substance_hits": substance,
                "substance_per_1000_words": per_thousand(substance, words),
                "buzzwords_per_substance_hit": round(total / substance, 3) if substance else "",
                "top_terms": "; ".join(f"{label}:{c}" for label, c in top),
            }
        )

    ranked = sorted(rows, key=lambda r: r["buzzwords_per_1000_words"])
    last = len(ranked) - 1
    for position, row in enumerate(ranked):
        row["density_percentile"] = round(100.0 * position / last, 1) if last else 0.0
    rows.sort(key=lambda r: -r["buzzwords_per_1000_words"])
    return rows


def aggregate_by_symbol(index: "Index", terms: list[dict], counts: np.ndarray,
                        sources: str = "auto") -> list[dict]:
    """Roll the per-document counts up to one row per company.

    A company can have more than one document: a main report, a separate ESG
    data sheet, and for some a captured web page. Hits and words are added
    across whichever documents are used before the density is taken, so a
    company is not helped or hurt by having split its disclosure over two files.

    sources decides which documents count.

      "auto"    reports when the company published one, its captured page only
                when it did not. This is the default, because a page capture is
                a weaker source, and because four of the captures turned out to
                be the same document as the report already held.
      "reports" reports only. Companies with nothing but a page drop out.
      "pages"   captured pages only.
      "all"     everything, duplicates included.
    """
    weights = np.array([t["weight"] for t in terms], dtype=np.float64)
    tier_mask = {tier: np.array([t["tier"] == tier for t in terms]) for tier in TIER_ORDER}
    extra_mask = np.array([bool(t.get("extra")) for t in terms])

    groups: dict[str, list[int]] = {}
    for i, doc in enumerate(index.docs):
        groups.setdefault(doc["symbol"], []).append(i)

    def choose(idxs: list[int]) -> list[int]:
        reports = [i for i in idxs if index.docs[i]["doc_kind"] != "page"]
        pages = [i for i in idxs if index.docs[i]["doc_kind"] == "page"]
        if sources == "reports":
            return reports
        if sources == "pages":
            return pages
        if sources == "all":
            return idxs
        return reports or pages

    rows: list[dict] = []
    for symbol, idxs in groups.items():
        idxs = choose(idxs)
        if not idxs:
            continue
        block = counts[idxs]
        per_term = block.sum(axis=0)
        words = int(sum(index.docs[i]["n_words"] for i in idxs))
        substance = int(sum(index.docs[i]["quantity_hits"] + index.docs[i]["year_hits"]
                            for i in idxs))
        total = int(per_term.sum())
        weighted = float(per_term @ weights)
        order = np.argsort(-per_term)[:15]
        rows.append(
            {
                "symbol": symbol,
                "company": index.docs[idxs[0]]["company"],
                "n_docs": len(idxs),
                "doc_kinds": sorted({index.docs[i]["doc_kind"] for i in idxs}),
                "files": [index.docs[i]["file"] for i in idxs],
                "total_words": words,
                "buzzword_hits": total,
                "buzzwords_per_1000_words": per_thousand(total, words),
                "buzzword_score_weighted": round(weighted, 1),
                "weighted_per_1000_words": per_thousand(weighted, words),
                "unique_buzzwords": int((per_term > 0).sum()),
                "extra_hits": int(per_term[extra_mask].sum()) if extra_mask.any() else 0,
                "extra_per_1000_words": (
                    per_thousand(int(per_term[extra_mask].sum()), words)
                    if extra_mask.any() else 0.0
                ),
                "substance_hits": substance,
                "buzzwords_per_substance_hit": round(total / substance, 2) if substance else "",
                **{
                    f"{tier}_per_1000_words": per_thousand(int(per_term[mask].sum()), words)
                    for tier, mask in tier_mask.items()
                },
                "top_terms": [
                    {"label": terms[j]["label"], "tier": terms[j]["tier"],
                     "hits": int(per_term[j]), "extra": bool(terms[j].get("extra"))}
                    for j in order if per_term[j] > 0
                ],
                "all_terms": [
                    {"label": terms[j]["label"], "tier": terms[j]["tier"],
                     "hits": int(per_term[j]), "pattern": terms[j]["pattern"],
                     "syntax": terms[j].get("syntax", "regex"),
                     "extra": bool(terms[j].get("extra"))}
                    for j in np.argsort(-per_term).tolist() if per_term[j] > 0
                ],
            }
        )
    rows.sort(key=lambda r: -r["buzzwords_per_1000_words"])
    return rows


def concordance(index: "Index", pattern: str, syntax: str = "regex",
                limit: int = 20, width: int = 9) -> list[dict]:
    """Real occurrences of a term, rebuilt from the token stream.

    The quickest way to tell whether a term catches what you meant. The text is
    reconstructed from the tokens, so punctuation is gone and a barrier between
    two words shows up as a middle dot.
    """
    starts, lengths = index.find(pattern, syntax)
    if starts.size == 0:
        return []
    inverse = {v: k for k, v in index.vocab.items()}
    step = max(1, starts.size // limit)
    out: list[dict] = []
    for start, length in zip(starts[::step][:limit].tolist(), lengths[::step][:limit].tolist()):
        doc_i = int(np.searchsorted(index.doc_start, start, "right") - 1)
        doc = index.docs[doc_i]
        lo = max(doc["token_offset"], start - width)
        hi = min(doc["token_offset"] + doc["n_tokens"], start + length + width)

        def word(pos: int) -> str:
            tok = inverse.get(int(index.tokens[pos]), "?")
            return "\u00b7" if tok == BARRIER else tok

        out.append({
            "symbol": doc["symbol"], "file": doc["file"],
            "before": " ".join(word(p) for p in range(lo, start)),
            "hit": " ".join(word(p) for p in range(start, start + length)),
            "after": " ".join(word(p) for p in range(start + length, hi)),
        })
    return out


def build_terms(lexicon_path: str = LEXICON, added: list[str] | None = None,
                only_added: bool = False, tier: str = "peacock") -> list[dict]:
    """The term list to score with: the saved lexicon, extra words, or both.

    Each extra word uses the plain syntax, so "steward*" and "net zero" work as
    typed. They are marked extra=True so their contribution is reported apart
    from the lexicon's.
    """
    terms: list[dict] = []
    if not only_added:
        for t in load_lexicon(lexicon_path):
            terms.append(dict(t, extra=False))
    for text in added or []:
        text = text.strip()
        if text:
            terms.append({"label": text, "tier": tier,
                          "weight": DEFAULT_WEIGHTS.get(tier, 1.0),
                          "pattern": text, "syntax": "plain", "extra": True})
    return terms


def added_word_stats(index: "Index", terms: list[dict], counts: np.ndarray) -> list[dict]:
    """For each added word: how often it occurs, and how much it actually added.

    An added word usually overlaps something the lexicon already has. Adding
    "steward*" when "stewardship" is already a term moves those hits from one
    term to the other rather than creating new ones, because the longest match
    wins and no stretch of text is ever counted twice.
    """
    stats: list[dict] = []
    for j, term in enumerate(terms):
        if not term.get("extra"):
            continue
        try:
            starts, _ = index.find(term["pattern"], term.get("syntax", "regex"))
            corpus_hits = int(starts.size)
        except (PatternTooBig, re.error) as exc:
            stats.append({"term": term["label"], "error": str(exc),
                          "corpus_hits": 0, "new_hits": 0, "already_counted": 0})
            continue
        new_hits = int(counts[:, j].sum())
        stats.append({"term": term["label"], "corpus_hits": corpus_hits,
                      "new_hits": new_hits, "already_counted": corpus_hits - new_hits})
    return stats
