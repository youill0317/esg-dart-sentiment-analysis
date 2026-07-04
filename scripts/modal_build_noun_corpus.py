from __future__ import annotations

import html
import json
import re
from pathlib import Path

import modal


APP_NAME = "ud26-dart-noun-corpus"
VOLUME_NAME = "ud26-dart-noun-corpus"
MOUNT_PATH = Path("/mnt/ud26")
REMOTE_RAW_XML_DIR = MOUNT_PATH / "raw_xml"
REMOTE_OUTPUT_DIR = MOUNT_PATH / "final"
REMOTE_PARTS_DIR = MOUNT_PATH / "parts"

LOCAL_RAW_XML_DIR = Path("data/dart/raw_xml")

volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("pandas", "pecab", "tqdm")
)

app = modal.App(APP_NAME, image=image)


def extract_target_sections(xml_text: str) -> tuple[str, int]:
    title_re = re.compile(r"<TITLE\b[^>]*>(.*?)</TITLE>", flags=re.I | re.S)
    main_title_re = re.compile(
        "^\s*(I|II|III|IV|V|VI|VII|VIII|IX|X|"
        "\u2160|\u2161|\u2162|\u2163|\u2164|\u2165|\u2166|\u2167|\u2168|\u2169)\."
    )
    target_title_regex = {
        "II. \uc0ac\uc5c5\uc758 \ub0b4\uc6a9": (
            "^(II|\u2161)\.\s*\uc0ac\uc5c5\uc758\s*\ub0b4\uc6a9"
        ),
        "IV. \uc774\uc0ac\uc758 \uacbd\uc601\uc9c4\ub2e8 \ubc0f \ubd84\uc11d\uc758\uacac": (
            "^(IV|\u2163)\.\s*\uc774\uc0ac\uc758\s*\uacbd\uc601\uc9c4\ub2e8"
            "\s*\ubc0f\s*\ubd84\uc11d\uc758\uacac"
        ),
        "VI. \uc774\uc0ac\ud68c \ub4f1 \ud68c\uc0ac\uc758 \uae30\uad00\uc5d0 \uad00\ud55c \uc0ac\ud56d": (
            "^(VI|\u2165)\.\s*\uc774\uc0ac\ud68c\s*\ub4f1\s*\ud68c\uc0ac\uc758"
            "\s*\uae30\uad00\uc5d0\s*\uad00\ud55c\s*\uc0ac\ud56d"
        ),
    }

    titles: list[tuple[str, int]] = []
    for match in title_re.finditer(xml_text):
        title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
        if main_title_re.match(title):
            titles.append((title, match.start()))

    sections: list[str] = []
    seen_sections: set[str] = set()

    for i, (title, start) in enumerate(titles):
        section_name = None
        for name, pattern in target_title_regex.items():
            if re.search(pattern, title):
                section_name = name
                break

        if section_name is None:
            continue

        end = titles[i + 1][1] if i + 1 < len(titles) else len(xml_text)
        section_text = re.sub(r"<[^>]+>", " ", xml_text[start:end])
        section_text = html.unescape(re.sub(r"\s+", " ", section_text)).strip()
        sections.append(section_text)
        seen_sections.add(section_name)

    return " ".join(sections), len(seen_sections)


@app.function(volumes={str(MOUNT_PATH): volume}, timeout=60 * 10)
def list_remote_xml_files() -> list[str]:
    return sorted(path.name for path in REMOTE_RAW_XML_DIR.glob("*.xml"))


@app.function(volumes={str(MOUNT_PATH): volume}, timeout=60 * 30, max_containers=100)
def process_xml_file(file_name: str) -> dict[str, int | str]:
    from pecab import PeCab

    match = re.match(r"(\d{6})_(\d{4})_(\d+)\.xml$", file_name)
    if not match:
        return {"file_name": file_name, "status": "skipped"}

    REMOTE_PARTS_DIR.mkdir(parents=True, exist_ok=True)

    path = REMOTE_RAW_XML_DIR / file_name
    tagger = PeCab()
    stock_code, fiscal_year, rcept_no = match.groups()
    xml_text = path.read_text(encoding="utf-8", errors="ignore")
    document, section_count = extract_target_sections(xml_text)
    noun_tokens = [noun for noun in tagger.nouns(document) if len(noun) > 1]

    row = {
        "stock_code": stock_code,
        "fiscal_year": int(fiscal_year),
        "rcept_no": rcept_no,
        "file_name": file_name,
        "section_count": section_count,
        "document": document,
        "total_word_count": len(document.split()),
        "noun_tokens": noun_tokens,
        "noun_document": " ".join(noun_tokens),
        "noun_token_count": len(noun_tokens),
    }

    part_path = REMOTE_PARTS_DIR / f"{Path(file_name).stem}.json"
    part_path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    volume.commit()

    return {
        "file_name": file_name,
        "status": "ok",
        "section_count": section_count,
        "noun_token_count": len(noun_tokens),
    }


@app.function(volumes={str(MOUNT_PATH): volume}, timeout=60 * 30)
def merge_noun_parts(file_names: list[str]) -> dict[str, int | str]:
    import pandas as pd

    volume.reload()
    REMOTE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    missing = []
    for file_name in file_names:
        part_path = REMOTE_PARTS_DIR / f"{Path(file_name).stem}.json"
        if not part_path.exists():
            missing.append(file_name)
            continue
        rows.append(json.loads(part_path.read_text(encoding="utf-8")))

    noun_corpus_df = pd.DataFrame(rows)
    csv_path = REMOTE_OUTPUT_DIR / "noun_corpus_pecab.csv"
    pkl_path = REMOTE_OUTPUT_DIR / "noun_corpus_pecab.pkl"

    noun_corpus_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    noun_corpus_df.to_pickle(pkl_path)
    volume.commit()

    return {
        "xml_files": len(file_names),
        "rows": len(noun_corpus_df),
        "missing_parts": len(missing),
        "csv_path": str(csv_path),
        "pkl_path": str(pkl_path),
    }


@app.local_entrypoint()
def main(upload: bool = True, raw_xml_dir: str = str(LOCAL_RAW_XML_DIR)):
    raw_xml_path = Path(raw_xml_dir)

    if upload:
        if not raw_xml_path.exists():
            raise FileNotFoundError(f"raw XML directory not found: {raw_xml_path}")

        print(f"Uploading {raw_xml_path} to Modal volume {VOLUME_NAME}:/raw_xml")
        with volume.batch_upload() as batch:
            batch.put_directory(raw_xml_path, "/raw_xml")

    if upload:
        xml_file_names = sorted(path.name for path in raw_xml_path.glob("*.xml"))
    else:
        xml_file_names = list_remote_xml_files.remote()

    print(f"Processing {len(xml_file_names):,} XML files with Modal map")
    results = list(process_xml_file.map(xml_file_names, order_outputs=False))
    ok_count = sum(1 for result in results if result.get("status") == "ok")
    print(f"Processed OK: {ok_count:,} / {len(results):,}")

    result = merge_noun_parts.remote(xml_file_names)
    print(result)
    print()
    print("Download outputs with:")
    print(f"  modal volume get {VOLUME_NAME} final/noun_corpus_pecab.csv final/noun_corpus_pecab.csv")
    print(f"  modal volume get {VOLUME_NAME} final/noun_corpus_pecab.pkl final/noun_corpus_pecab.pkl")
