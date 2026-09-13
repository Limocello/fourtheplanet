# Buzzword / peacock index

Scores the sustainability disclosure of 395 S&P 500 companies on how much of the text is corporate and sustainability boilerplate. It does not measure how sustainable a company is. It measures how much of the writing is language rather than information.

485 documents, 9.5 million words. Everything needed to query them is in this folder.

## Getting started

Needs Python 3.9 or newer and numpy. Nothing else.

```
pip3 install numpy
python3 buzzword.py board
```

That prints the leaderboard. Scoring the whole corpus takes about 0.2 seconds, so every command re-scores from scratch.

## Commands

```
python3 buzzword.py board                          rank companies
python3 buzzword.py board --metric peacock_per_1000_words
python3 buzzword.py board --add "steward*"         add a word of your own
python3 buzzword.py board --only-added --add "future ready"

python3 buzzword.py lookup AAPL                    one company in detail
python3 buzzword.py lookup nike.com                ticker, name or website all work
python3 buzzword.py lookup "Bank of America"

python3 buzzword.py find "net zero"                where a term occurs, in context
python3 buzzword.py terms                          the lexicon
python3 buzzword.py export out.csv --by company    write the table
```

Add `--json` to any command for machine-readable output. That is the simplest way to drive a front end: either shell out to the CLI and read the JSON, or import `buzzword_engine` directly.

## What is measured

**Density.** Buzzwords per 1 000 words. This is the headline figure, and it is independent of report length, which is the point. Across the 450 reports the correlation between density and word count is 0.04, so a long report is not penalised for being long.

**Raw count.** How many buzzwords the report contains. This mostly measures length, so it is the least useful number on its own.

**Weighted score.** The count with each term multiplied by its weight. Every term currently has weight 1, so this is the same number as the raw count. It becomes a separate figure the moment you change a weight in the lexicon.

**Buzzwords per hard number.** The counterweight. A hard number is a figure carrying a unit, a percentage, or a year. A report can be long and vague or long and dense with data, and this separates the two. Dell has 14 buzzwords for every hard number. Avery Dennison has 1.2.

## What is in the corpus

| Kind | Documents | What it is |
|---|---|---|
| `main` | 366 | The company's sustainability or ESG report. |
| `data` | 84 | A separate ESG data sheet, where one was published apart from the report. |
| `page` | 35 | A captured sustainability web page, for companies that published no report. |

By default a company is scored on its report, and on its captured page only if it published no report. 17 companies are in the index on a page alone. That default is `--sources auto`, and `reports`, `pages` or `all` override it.

The default exists for a reason. Four of the page captures turned out to be the same document as the report already held, byte for byte in substance: Meta's capture is 16 071 words against a 16 019-word report. Counting both would have counted the same text twice.

63 web captures were on hand. 35 survived extraction. The other 28 are JavaScript shells or cookie banners that yielded under 150 words, which is not a disclosure and is not scored. Six captures were PDFs saved under an `.html` name, so the extractor decides the format from the first bytes rather than the file extension.

## The word list

`buzzword_lexicon.tsv`, 252 terms in four tiers.

Every term carries weight 1. The score is a straight count, and no category counts for more than another.

The `tier` column still labels what kind of language a term is, and the per-tier columns still show what a company's score is made of, but it no longer changes the score.

| Tier | Terms | What it catches | Examples |
|---|---|---|---|
| `peacock` | 71 | Pure display. Says nothing at all. | journey, best-in-class, force for good, move the needle, in our DNA |
| `hedge` | 39 | Commitment without commitment. | aim to, strive to, where feasible, on track to, contribute to |
| `corporate` | 75 | Management register. | leverage, ecosystem, pillars, unlock value, empower, robust |
| `esg_jargon` | 67 | Sustainability vocabulary. Real concepts, heavily worn. | net zero, circular economy, stewardship, materiality, impact |

Weighting is still supported, so raising or lowering a tier is one column edit away. Be aware of what the tiers are actually worth before you do. The 71 peacock terms account for 10 115 hits across the corpus, while the 75 corporate terms account for 102 528 and the 67 ESG terms for 171 636. Putting peacock on 3 and corporate on 2, as an earlier version did, still left peacock at under 6 per cent of the score and generic management vocabulary at nearly 40 per cent. A tier's weight and its influence are not the same thing.

Each row of the file is a label, a tier, a weight, a pattern and a syntax. Edit the file to add, drop or reweight terms. No code changes are needed. A `regex` row is a regular expression. A `plain` row uses the simple syntax below.

## Adding words

Words added with `--add`, and `plain` rows in the lexicon, use a simple syntax rather than a regular expression.

| You write | It finds |
|---|---|
| `net zero` | "net zero" and, because a hyphen counts as a space, "net-zero" |
| `steward*` | steward, stewards, stewardship, stewarding |
| `strive\|aspire to` | "strive to" and "aspire to" |

One thing to expect. Adding a word that overlaps something the lexicon already has moves hits from one term to the other rather than creating new ones, because the longest match wins and no stretch of text is ever counted twice. Adding `steward*` finds 4 323 occurrences but only adds 236, since `stewardship` is already a term. The tools report both numbers so this is visible rather than mysterious.

## Counting rules

Matches are resolved in favour of the longest one, so each stretch of text is counted exactly once. "A sustainable future" is one hit for `sustainable X`, not two hits for `sustainable` plus a phrase. A company cannot inflate or deflate its score by writing a more specific phrase.

A hyphen counts as a space, so "net-zero" and "net zero" are the same thing, and `biodiversity` is found inside "biodiversity-related". Any other punctuation is a barrier that a phrase cannot be matched across, so the line "positive. Impact on water" is not a hit for "positive impact". A maximal run of letters and digits stays one word, so the running header "FY24ESG" does not contain an `esg`.

A company with both a main report and a separate ESG data sheet has them added together before the density is taken, so publishing the numbers in a second file neither helps nor hurts.

## The visual page

`web/ecovision.html` is the Three.js assessment page with the peacock index built into it. Open it however you already open it. It needs `web/peacock-data.js` beside it, which is why they live in the same folder.

- The **Peacock Index** panel sits at the bottom right, under Assessment: density, the split across the four categories, buzzword count, report length, buzzwords per hard number, and rank among the 372 companies long enough to compare.
- A **peacock** sits beside the number, one of five drawings from a closed tail to a full squawking fan. Which one appears depends on where the company falls among the others, in fifths, rather than on a fixed score. That is deliberate: the cut points are recomputed from whatever terms are currently active, so the picture keeps its meaning when words are added or dropped. Every term set still has a least and a most pompous fifth. A side effect worth knowing is that dropping a term can move a company up a peacock, if its peers leaned on that word more than it did.
- **Buzzwords**, top right of that panel, opens the editor. Type a word to add it, untick a term to drop it, and the score, the composition bar and the rank all move immediately.
- Companies with no report in the corpus say so rather than showing a zero. The page carries 501 companies and the corpus covers 395 of them.

The page is standalone, so the index travels with it. `peacock-data.js` is generated by `make_page_data.py` and holds per-company counts for the 252 lexicon terms plus 24 176 individual words, which is what lets a word typed into the page be counted without asking anything. The five peacock drawings are inlined into it as well, about 150 kB of SVG against 7 MB of counts. Regenerate it after editing the lexicon:

```
python3 make_page_data.py --out web/peacock-data.js
```

Two limits come from being a single file with no server. Only single words can be added, with no phrases and no wildcards, and a word must occur at least four times in the corpus to have been baked in. For those, use `buzzword.py`.

Adding a word counts only the occurrences the lexicon does not already claim. "sustainability" occurs 44 588 times and every one is already inside the term `sustainable`, so adding it moves nothing and the page says why. Counting them again would count the same words twice.

## Files

| File | What it is |
|---|---|
| `buzzword.py` | The command line. Start here. |
| `buzzword_engine.py` | The library. `Index`, `score`, `aggregate_by_symbol`, `concordance`. |
| `buzzword_lexicon.tsv` | The 252 terms. The thing to edit. |
| `build_index.py` | Rebuilds the index from document text. `--txt-dir` is repeatable. |
| `extract_pages.py` | Turns captured web pages into text the index can read. |
| `make_page_data.py` | Bakes the index into `web/peacock-data.js` for the visual page. |
| `web/ecovision.html` | The Three.js assessment page, with the peacock index panel. |
| `web/peacock-data.js` | The baked index the page reads, 7 MB. Generated, not edited. |
| `web/peacocks/` | The five drawings, SVG and PNG, plus the scale image. The SVGs are inlined into `peacock-data.js`; the folder is the source. |
| `scan_reports.py` | A second, independent implementation that scans the text directly. Slow, about 20 minutes, and depends on nothing but the text. Kept as the cross-check on the fast one. |
| `data/index/` | The token index, 98 MB. Rebuild it with `build_index.py`, never edit it. |
| `data/pages_txt/` | Text extracted from the 35 usable web captures. |
| `data/buzzword_index.csv` | Results, one row per report. |
| `data/buzzword_index_by_company.csv` | Results, one row per company. |
| `data/companies.csv` | Ticker, name, website and GICS sector for each of the 395. |

The report PDFs and their extracted text are not in this folder, because they are 6 GB and 126 MB. They are only needed to rebuild the index or to run `scan_reports.py`. To rebuild:

```
python3 build_index.py --txt-dir ../raw/reports/txt --txt-dir data/pages_txt
```

## How it is fast

Scoring by scanning means walking 126 MB of text with 252 patterns, which takes about 20 minutes. That is fine once and impossible to work with interactively.

`build_index.py` reads the text once and stores every report as a sequence of integers, one per word, plus a list of every position each word occupies. Counting a phrase then means taking its rarest word, looking up where that word already is, and checking the neighbours. The work no longer depends on how much text there is, only on how often the word occurs.

The corpus is 12.0 million tokens over 73 000 distinct words. Re-scoring all 252 terms across all 485 documents takes **0.15 seconds**, about 8 000 times faster than scanning. A new wildcard such as `steward*` takes 11 milliseconds, most of it spent asking the vocabulary which words start with "steward".

## Is it right

`scan_reports.py` is a separate implementation that walks the raw text with regular expressions and shares no matching code with the fast one. Comparing them: identical counts on 251 of the 450 reports, a median difference of zero, a worst case of 3.9 per cent, and a median change in rank of zero places. The remaining differences are all the index treating a hyphenated compound as the phrase it is, which the scanner cannot do.

Rebuilding the index and re-exporting reproduces the shipped CSV exactly, all 450 rows.

## Caveats worth stating

The text comes from PDF extraction, so tables, headers, figure captions and running footers are in the word count along with the prose. A report titled "Impact Report" collects a hit for `impact` in every page header, which is why Tesla's top term is `impact` at 247 occurrences in 6 910 words. Use `buzzword.py find` on a term before trusting it.

Many documents are under 2 000 words, the page captures most of all. These are ESG data sheets rather than reports, and their density is unstable, because one extra "sustainable" in a 250-word table moves it by four points. The tools drop them by default, controlled with `--min-words`.

A high score is evidence about the writing, not about the company. A report can be honest and badly written, or slick and empty. The index measures the register, and is most useful next to emissions data rather than instead of it.

## Results as they stand

Density across the 372 companies with more than 2 000 words runs from 3.9 to 65.0 per 1 000 words, median about 36.

Highest: PTC 65.0, Tesla 63.0, Rockwell Automation 62.9.
Lowest: EQT 3.9, Cincinnati Financial 7.9, Weyerhaeuser 10.3.
`paradigm shift` appears exactly once in all 450 reports.
