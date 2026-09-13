"""Turn captured sustainability web pages into plain text for the index.

Some companies publish no downloadable report, only a sustainability section on
their website. Those pages were captured as HTML, and most of them also as a
printed PDF. This reads whichever capture yields more usable text and writes one
text file per company, named so the index picks up the ticker and marks the
document kind as "page".

    python3 extract_pages.py --pages-dir ../pages --out-dir data/pages_txt

The file type is decided by the first bytes, not by the extension, because some
captures are PDFs saved under an .html name. HTML is stripped of script, style
and navigation, and then of cookie, consent and site-furniture lines, which
otherwise make up most of a thin landing page. PDFs go through pdftotext when it
is available and pdfminer otherwise.

A capture that yields fewer than --min-words words of real text is reported and
skipped. A cookie banner is not a sustainability disclosure.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import re
import shutil
import subprocess
import sys

WORD_RX = re.compile(r"[A-Za-z][A-Za-z'-]*")
DROP_TAGS = ("script", "style", "noscript", "svg", "nav", "header", "footer",
             "form", "iframe", "template", "button", "select", "option")
DROP_ATTR = re.compile(r"cookie|consent|gdpr|banner|modal|popup|newsletter|breadcrumb"
                       r"|site-?nav|main-?nav|menu|skip-?link|social|share", re.I)

# Lines that are site furniture rather than disclosure. These dominate a thin
# landing page, and leaving them in would score the cookie notice, not the company.
BOILERPLATE = re.compile(
    r"\bcookies?\b|\bconsent\b|privacy (policy|preference|notice|statement)|accept all"
    r"|manage (your )?(preferences|settings)|\bgdpr\b|opt[- ]out|do not sell"
    r"|all rights reserved|terms of (use|service)|site ?map|skip to (main )?content"
    r"|sign ?in|log ?in|subscribe|follow us|back to top|©|\bcopyright\b"
    r"|javascript|enable js|browser (is )?(not )?supported"
    r"|more options|by visiting our website|web domains|for advertising",
    re.I,
)


def sniff(path: str) -> str:
    """PDF or HTML, decided by the first bytes rather than the file name."""
    with open(path, "rb") as fh:
        head = fh.read(1024).lstrip()
    return "pdf" if head[:4] == b"%PDF" else "html"


def clean_lines(text: str) -> str:
    """Drop site furniture and repeated navigation, keep the prose."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for line in (ln.strip() for ln in text.splitlines()):
        if not line or BOILERPLATE.search(line):
            continue
        # A line of one or two words repeated across the page is navigation.
        key = line.lower()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 2 and len(WORD_RX.findall(line)) <= 4:
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def words(text: str) -> int:
    return len(WORD_RX.findall(text))


def from_html(path: str) -> str:
    raw = open(path, "rb").read()
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        sys.exit("beautifulsoup4 is required for HTML captures:  pip3 install beautifulsoup4")
    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:
        soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(list(DROP_TAGS)):
        tag.decompose()
    for attr in ("class", "id", "role", "aria-label"):
        for tag in soup.find_all(attrs={attr: DROP_ATTR}):
            tag.decompose()
    return clean_lines(soup.get_text("\n", strip=True))


def from_pdf(path: str) -> str:
    text = _pdf_text(path)
    return clean_lines(text)


def _pdf_text(path: str) -> str:
    if shutil.which("pdftotext"):
        try:
            out = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", path, "-"],
                                 capture_output=True, timeout=120)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.decode("utf-8", "replace")
        except (subprocess.TimeoutExpired, OSError):
            pass
    try:
        from pdfminer.high_level import extract_text
        return extract_text(path) or ""
    except Exception:
        return ""


def extract(job: tuple[str, dict]) -> dict:
    """Read every capture for one ticker and keep the richest."""
    symbol, paths = job
    best_text, best_from, tried = "", "", {}
    for name, path in sorted(paths.items()):
        kind = sniff(path)
        label = name if kind == name else f"{name}(really {kind})"
        try:
            text = from_html(path) if kind == "html" else from_pdf(path)
        except Exception as exc:
            tried[label] = f"failed: {exc}"
            continue
        n = words(text)
        tried[label] = n
        if n > words(best_text):
            best_text, best_from = text, label
    return {"symbol": symbol, "text": best_text, "source": best_from,
            "words": words(best_text), "tried": tried}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages-dir", default=os.path.join("..", "pages"))
    ap.add_argument("--out-dir", default=os.path.join("data", "pages_txt"))
    ap.add_argument("--min-words", type=int, default=150)
    ap.add_argument("--jobs", type=int, default=0)
    args = ap.parse_args()

    if not os.path.isdir(args.pages_dir):
        sys.exit(f"no such folder: {args.pages_dir}")

    captures: dict[str, dict] = {}
    for name in sorted(os.listdir(args.pages_dir)):
        stem, ext = os.path.splitext(name)
        ext = ext.lower().lstrip(".")
        if ext in ("html", "htm", "pdf"):
            captures.setdefault(stem, {})["html" if ext != "pdf" else "pdf"] = \
                os.path.join(args.pages_dir, name)
    if not captures:
        sys.exit(f"no .html or .pdf captures in {args.pages_dir}")

    os.makedirs(args.out_dir, exist_ok=True)
    jobs = max(1, args.jobs or (os.cpu_count() or 1))
    print(f"{len(captures)} companies captured, {jobs} process(es)", flush=True)

    kept, thin = 0, []
    with mp.Pool(jobs) as pool:
        for n, r in enumerate(pool.imap_unordered(extract, sorted(captures.items()), chunksize=1), 1):
            if r["words"] < args.min_words:
                thin.append((r["symbol"], r["words"], r["tried"]))
            else:
                with open(os.path.join(args.out_dir, f"{r['symbol']}__page_0.txt"),
                          "w", encoding="utf-8") as fh:
                    fh.write(r["text"])
                kept += 1
            if n % 20 == 0 or n == len(captures):
                print(f"  {n}/{len(captures)}", flush=True)

    print(f"\nwrote {kept} text files to {args.out_dir}")
    if thin:
        print(f"skipped {len(thin)} capture(s) with under {args.min_words} words:")
        for symbol, n, tried in sorted(thin):
            print(f"  {symbol:<8} {n:>5} words   {tried}")


if __name__ == "__main__":
    main()
