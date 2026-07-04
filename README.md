# ESG DART Sentiment Analysis

Korean NLP term project for analyzing ESG-related text in DART corporate
filings and comparing text-derived signals with ESG rating information.

## File

- `비정형_최종.ipynb`: final project notebook

## What This Notebook Covers

- DART filing corpus preparation
- Korean noun/token extraction
- ESG seed dictionary scoring
- dictionary expansion with embedding-based approaches
- validation with Spearman correlation
- ESG sentence sentiment analysis
- comparison between ESG text signals and rating changes

## Data Policy

Raw DART XML files, intermediate corpora, CSV/XLSX data files, API keys, model
artifacts, and generated outputs are not included in this repository.

The notebook may reference local or Colab paths from the original project
environment. To rerun it, prepare the required data locally and update paths in
the notebook.

## Environment

Install the Python dependencies with:

```bash
pip install -r requirements.txt
```

Some sections were originally run in Google Colab and may require GPU/runtime
adjustments depending on the environment.
