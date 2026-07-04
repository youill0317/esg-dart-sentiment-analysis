from __future__ import annotations

import argparse
import csv
import os
import time
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MASTER = ROOT / "final" / "company_master.csv"
DEFAULT_OUT = ROOT / "data" / "dart"


@dataclass
class CorpCode:
    corp_code: str
    corp_name: str
    stock_code: str


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def request_json(session: requests.Session, url: str, params: dict[str, str], timeout: int) -> dict:
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def merge_disclosure_rows(*payloads: dict) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for payload in payloads:
        for row in payload.get("list", []) or []:
            key = row.get("rcept_no", "")
            if key and key not in seen:
                merged.append(row)
                seen.add(key)
    return merged


def load_corp_codes(session: requests.Session, api_key: str, out_dir: Path, timeout: int) -> dict[str, CorpCode]:
    cache_path = out_dir / "corp_codes.csv"
    if cache_path.exists():
        frame = pd.read_csv(cache_path, dtype=str).fillna("")
        return {
            row.stock_code: CorpCode(row.corp_code, row.corp_name, row.stock_code)
            for row in frame.itertuples(index=False)
            if row.stock_code
        }

    response = session.get(
        "https://opendart.fss.or.kr/api/corpCode.xml",
        params={"crtfc_key": api_key},
        timeout=timeout,
    )
    response.raise_for_status()

    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        xml_bytes = archive.read(archive.namelist()[0])

    root = ET.fromstring(xml_bytes)
    rows: list[dict[str, str]] = []
    for item in root.findall("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        if not stock_code:
            continue
        rows.append(
            {
                "corp_code": (item.findtext("corp_code") or "").strip(),
                "corp_name": (item.findtext("corp_name") or "").strip(),
                "stock_code": stock_code,
            }
        )

    pd.DataFrame(rows).sort_values(["stock_code", "corp_code"]).to_csv(
        cache_path, index=False, encoding="utf-8-sig"
    )
    return {
        row["stock_code"]: CorpCode(row["corp_code"], row["corp_name"], row["stock_code"])
        for row in rows
    }


def annual_report_candidates(rows: list[dict], fiscal_year: str) -> list[dict]:
    target = f"({fiscal_year}.12)"
    exact = [
        row
        for row in rows
        if "사업보고서" in row.get("report_nm", "") and target in row.get("report_nm", "")
    ]

    fallback = [row for row in rows if "사업보고서" in row.get("report_nm", "")]
    fallback = [row for row in fallback if row not in exact]

    ordered: list[dict] = []
    # Prefer the most recent corrected filing that has a downloadable XML body.
    # Attachment-only corrections often appear newer but document.xml returns status 014;
    # those are attempted and skipped if no XML exists.
    for group in [exact, fallback]:
        ordered.extend(sorted(group, key=lambda row: row.get("rcept_dt", ""), reverse=True))
    return ordered


def save_document_xml(
    session: requests.Session,
    api_key: str,
    rcept_no: str,
    out_path: Path,
    timeout: int,
) -> tuple[bool, str]:
    if out_path.exists() and out_path.stat().st_size > 0:
        return True, "cached"

    response = session.get(
        "https://opendart.fss.or.kr/api/document.xml",
        params={"crtfc_key": api_key, "rcept_no": rcept_no},
        timeout=timeout,
    )
    response.raise_for_status()

    try:
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            xml_names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
            if not xml_names:
                return False, "zip_has_no_xml"
            xml_text = archive.read(xml_names[0]).decode("utf-8", errors="ignore")
    except zipfile.BadZipFile:
        text = response.content.decode("utf-8", errors="ignore")
        return False, f"not_zip:{text[:120].replace(chr(10), ' ')}"

    out_path.write_text(xml_text, encoding="utf-8")
    return True, "downloaded"


def write_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENDART_API_KEY") or os.getenv("DART_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENDART_API_KEY or DART_API_KEY")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = args.out_dir / "raw_xml"
    raw_dir.mkdir(parents=True, exist_ok=True)

    master = pd.read_csv(args.master, dtype=str).fillna("")
    required_columns = {"company_name", "stock_code", "fiscal_year"}
    missing = sorted(required_columns - set(master.columns))
    if missing:
        raise SystemExit(f"Missing required columns: {missing}")

    if args.limit:
        master = master.head(args.limit)

    session = requests.Session()
    corp_codes = load_corp_codes(session, api_key, args.out_dir, args.timeout)

    successes: list[dict] = []
    failures: list[dict] = []

    for idx, row in master.iterrows():
        company_name = row["company_name"]
        stock_code = row["stock_code"].zfill(6)
        fiscal_year = row["fiscal_year"]
        base = {
            "source_row": str(idx),
            "company_name": company_name,
            "stock_code": stock_code,
            "fiscal_year": fiscal_year,
            "esg_year": row.get("esg_year", ""),
        }

        corp = corp_codes.get(stock_code)
        if not corp:
            failures.append({**base, "stage": "corp_code", "reason": "stock_code_not_found"})
            continue

        disclosure_year = int(fiscal_year) + 1
        list_params = {
            "crtfc_key": api_key,
            "corp_code": corp.corp_code,
            "bgn_de": f"{disclosure_year}0101",
            "end_de": f"{disclosure_year}1231",
            "last_reprt_at": "Y",
            "pblntf_detail_ty": "A001",
            "page_count": "100",
        }

        try:
            payload = request_json(
                session,
                "https://opendart.fss.or.kr/api/list.json",
                list_params,
                args.timeout,
            )
            retry_params = dict(list_params)
            retry_params.pop("last_reprt_at", None)
            retry_payload = request_json(
                session,
                "https://opendart.fss.or.kr/api/list.json",
                retry_params,
                args.timeout,
            )
            if payload.get("status") != "000" and retry_payload.get("status") == "000":
                payload = retry_payload

            if payload.get("status") != "000":
                failures.append(
                    {
                        **base,
                        "corp_code": corp.corp_code,
                        "stage": "filing_list",
                        "reason": payload.get("message", payload.get("status", "unknown")),
                    }
                )
                time.sleep(args.sleep)
                continue

            disclosure_rows = (
                merge_disclosure_rows(payload, retry_payload)
                if retry_payload.get("status") == "000"
                else payload.get("list", []) or []
            )
            candidates = annual_report_candidates(disclosure_rows, fiscal_year)
            if not candidates:
                failures.append(
                    {
                        **base,
                        "corp_code": corp.corp_code,
                        "stage": "choose_report",
                        "reason": "annual_report_not_found",
                    }
                )
                time.sleep(args.sleep)
                continue

            report = None
            rcept_no = ""
            xml_path = None
            document_status = ""
            attempted_documents: list[str] = []
            for candidate in candidates:
                rcept_no = candidate.get("rcept_no", "")
                xml_path = raw_dir / f"{stock_code}_{fiscal_year}_{rcept_no}.xml"
                ok, document_status = save_document_xml(
                    session, api_key, rcept_no, xml_path, args.timeout
                )
                attempted_documents.append(f"{rcept_no}:{document_status}")
                if ok:
                    report = candidate
                    break
            if not ok:
                failures.append(
                    {
                        **base,
                        "corp_code": corp.corp_code,
                        "rcept_no": rcept_no,
                        "stage": "document",
                        "reason": "; ".join(attempted_documents),
                    }
                )
                time.sleep(args.sleep)
                continue

            successes.append(
                {
                    **base,
                    "corp_code": corp.corp_code,
                    "dart_corp_name": corp.corp_name,
                    "report_nm": report.get("report_nm", ""),
                    "rcept_no": rcept_no,
                    "rcept_dt": report.get("rcept_dt", ""),
                    "viewer_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
                    "xml_path": str(xml_path.relative_to(ROOT)),
                    "document_status": document_status,
                }
            )
        except Exception as exc:
            failures.append(
                {
                    **base,
                    "corp_code": corp.corp_code,
                    "stage": "exception",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
        time.sleep(args.sleep)

        processed = len(successes) + len(failures)
        if processed % 25 == 0:
            print(f"processed={processed} success={len(successes)} failure={len(failures)}", flush=True)

    write_rows(args.out_dir / "filing_index.csv", successes)
    write_rows(args.out_dir / "failed_filings.csv", failures)
    print(f"done success={len(successes)} failure={len(failures)}")
    return 0 if successes else 1


if __name__ == "__main__":
    raise SystemExit(main())
