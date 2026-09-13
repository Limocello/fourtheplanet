PEACOCK INDEX — the visual page
================================

Two files must sit in the same folder:

    ecovision.html      the page
    peacock-data.js     the buzzword index it reads (7 MB)

Open ecovision.html. Nothing to install and nothing to run.

peacocks/   the five drawings as SVG and PNG, plus the scale image.
            The SVGs are already inlined into peacock-data.js, so this
            folder is the source, not a dependency. The page works
            without it.

WHAT IT DOES
------------
Pick a company in the top-left search. The bottom-right panel scores its
sustainability report on how much of the text is corporate and
sustainability boilerplate, and shows one of five peacocks.

Which peacock depends on where the company falls among the others, in
fifths, not on a fixed score. The cut points are recomputed from whatever
terms are active, so the picture keeps its meaning when words are added
or dropped.

The BUZZWORDS button opens the editor. Type a word to add it, untick a
term to drop it. Everything recomputes in about a fifth of a second.

Single words only here, no phrases and no wildcards, and a word must
occur at least four times in the corpus. For phrases, wildcards and the
full 485-document corpus, use buzzword.py in the folder above.

REGENERATING
------------
After editing ../buzzword_lexicon.tsv:

    cd ..
    python3 make_page_data.py --out web/peacock-data.js

NOTE
----
The page loads three.js from unpkg.com, so the 3D scene needs an
internet connection. The peacock panel works offline either way.
