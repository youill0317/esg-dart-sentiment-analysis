from __future__ import annotations

import os
import shutil
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
DART = ROOT / "data" / "dart"
RAW = DART / "raw_xml"
NON_BODY = DART / "raw_xml_non_body"

SECTION_PATTERNS = {
    "II": r"<TITLE\b[^>]*>\s*(II|Ⅱ)\.\s*사업의\s*내용\s*</TITLE>",
    "IV": r"<TITLE\b[^>]*>\s*(IV|Ⅳ)\.\s*이사의\s*경영진단\s*및\s*분석의견\s*</TITLE>",
    "VI": r"<TITLE\b[^>]*>\s*(VI|Ⅵ)\.\s*이사회\s*등\s*회사의\s*기관에\s*관한\s*사항\s*</TITLE>",
}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_key() -> str:
    load_dotenv(ROOT / ".env")
    key = os.getenv("OPENDART_API_KEY") or os.getenv("DART_API_KEY")
    if not key:
        raise SystemExit("Missing OPENDART_API_KEY or DART_API_KEY")
    return key


def section_count(xml_text: str) -> int:
    import re

    return sum(bool(re.search(pattern, xml_text, flags=re.I | re.S)) for pattern in SECTION_PATTERNS.values())


def target_files() -> pd.DataFrame:
    index = pd.read_csv(DART / "filing_index.csv", dtype=str).fillna("")
    rows = []
    for row in index.itertuples(index=False):
        xml_path = ROOT / row.xml_path
        count = section_count(xml_path.read_text(encoding="utf-8", errors="ignore")) if xml_path.exists() else -1
        rows.append(
            {
                "stock_code": row.stock_code,
                "fiscal_year": int(row.fiscal_year),
                "rcept_no": row.rcept_no,
                "xml_path": xml_path,
                "section_count": count,
            }
        )
    return pd.DataFrame(rows)


def best_xml_from_document(session: requests.Session, key: str, rcept_no: str) -> tuple[str, str, int]:
    response = session.get(
        "https://opendart.fss.or.kr/api/document.xml",
        params={"crtfc_key": key, "rcept_no": rcept_no},
        timeout=60,
    )
    response.raise_for_status()

    candidates: list[tuple[str, str, int]] = []
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".xml"):
                continue
            text = archive.read(name).decode("utf-8", errors="ignore")
            candidates.append((name, text, section_count(text)))

    if not candidates:
        raise RuntimeError(f"document zip has no xml: {rcept_no}")

    return max(candidates, key=lambda item: (item[2], len(item[1])))


def main() -> int:
    key = api_key()
    NON_BODY.mkdir(parents=True, exist_ok=True)

    targets = target_files()
    needs_repair = targets[targets["section_count"] < 3].copy()
    print(f"files={len(targets)} needs_repair={len(needs_repair)}")

    session = requests.Session()
    repaired = 0
    still_bad = []

    for row in needs_repair.itertuples(index=False):
        name, text, count = best_xml_from_document(session, key, row.rcept_no)
        old_path = Path(row.xml_path)
        backup_path = NON_BODY / old_path.name
        if old_path.exists() and not backup_path.exists():
            shutil.move(str(old_path), str(backup_path))
        elif old_path.exists():
            old_path.unlink()

        old_path.write_text(text, encoding="utf-8")
        repaired += 1
        print(row.stock_code, row.fiscal_year, row.rcept_no, "selected", name, "sections", count)
        if count < 3:
            still_bad.append((row.stock_code, row.fiscal_year, row.rcept_no, count, name))
        time.sleep(0.2)

    print(f"repaired={repaired} still_bad={len(still_bad)}")
    for item in still_bad[:30]:
        print("still_bad", item)
    return 0 if not still_bad else 2


if __name__ == "__main__":
    raise SystemExit(main())
