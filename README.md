# ESG DART Sentiment Analysis

This repository contains a Korean NLP project for analyzing ESG-related
language in DART corporate filings and comparing text-derived ESG signals with
external ESG ratings.

## Project Summary

The project builds an ESG text analysis pipeline around Korean public filings:

- collect and repair DART filing XML documents
- build a noun corpus with Korean tokenization
- score seed ESG dictionaries with count and TF-IDF features
- expand ESG dictionaries with Kiwi embedding and fastText-style similarity
- validate expanded dictionaries against ESG ratings with Spearman correlation
- run ESG sentiment analysis on filing sentences
- compare ESG sentiment and ESG rating changes

## Repository Layout

```text
notebooks/
  00_corpus.ipynb
  01_pecab_noun_corpus.ipynb
  ...
  16_fasttext_dictionary_regex_count_kiwi_tfidf_validation.ipynb
  final_esg_sentiment_analysis.ipynb
scripts/
  download_dart_filings.py
  repair_dart_body_xml.py
  repair_dart_filing_index.py
  modal_build_noun_corpus.py
docs/
  spearman_statistics_summary.md
```

## Data Policy

Raw DART XML archives, local corpora, API keys, and derived large data files are
not included in this repository. The notebooks assume local data paths from the
original analysis environment and may need path updates before rerunning.

Required secrets should be supplied through environment variables, for example:

```text
DART_API_KEY
OPENDART_API_KEY
```

## Key Result

The validation notebooks compare seed-only ESG dictionaries with expanded
dictionaries. The summary in `docs/spearman_statistics_summary.md` reports
Spearman correlations for pooled and yearly firm-year observations.

## Notes

This is a portfolio snapshot of the analysis pipeline. The code and notebooks
are included for reproducibility and review, while restricted raw data is kept
out of version control.
