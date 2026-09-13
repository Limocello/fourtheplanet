# fourtheplanet

Sustainability work on the S&P 500.

## buzzword-index

Scores the sustainability disclosure of 395 S&P 500 companies on how much of the text is corporate and sustainability boilerplate. It does not measure how sustainable a company is. It measures how much of the writing is language rather than information.

485 documents, 9.5 million words, all of it queryable in about a fifth of a second.

```
cd buzzword-index
pip3 install numpy
python3 buzzword.py board          # rank the companies
python3 buzzword.py lookup AAPL    # one company in detail
```

Open `buzzword-index/web/ecovision.html` for the visual version, with the peacock.

`buzzword-index/README.md` explains the method, the 252-term word list, the counting rules and the caveats.

## A note on size

The repository carries the token index, two 48 MB binary files under `buzzword-index/data/index/`. They are the corpus, so everything works straight after a clone with nothing to rebuild. GitHub warns about files over 50 MB; these sit just under. The index only changes when documents are added, not when the word list is edited.
