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
DATA = ROOT / "data"
DART = DATA / "dart"
RAW = DART / "raw_xml"
OUT_OF_SAMPLE = DART / "raw_xml_out_of_sample"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_api_key() -> str:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENDART_API_KEY") or os.getenv("DART_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENDART_API_KEY or DART_API_KEY")
    return api_key


def disclosure_rows(
    session: requests.Session,
    api_key: str,
    corp_code: str,
    fiscal_year: int,
) -> list[dict]:
    disclosure_year = fiscal_year + 1
    rows: list[dict] = []
    seen: set[str] = set()

    for last_reprt_at in ["Y", None]:
        params = {
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bgn_de": f"{disclosure_year}0101",
            "end_de": f"{disclosure_year}1231",
            "pblntf_detail_ty": "A001",
            "page_count": "100",
        }
        if last_reprt_at:
            params["last_reprt_at"] = last_reprt_at

        response = session.get(
            "https://opendart.fss.or.kr/api/list.json",
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "000":
            continue

        for row in payload.get("list", []) or []:
            rcept_no = row.get("rcept_no", "")
            if rcept_no and rcept_no not in seen:
                rows.append(row)
                seen.add(rcept_no)
        time.sleep(0.2)

    return rows


def annual_report_candidates(rows: list[dict], fiscal_year: int) -> list[dict]:
    target = f"({fiscal_year}.12)"
    exact = [
        row
        for row in rows
        if "사업보고서" in row.get("report_nm", "")
        and target in row.get("report_nm", "")
    ]
    fallback = [
        row
        for row in rows
        if "사업보고서" in row.get("report_nm", "") and row not in exact
    ]
    return sorted(exact, key=lambda row: row.get("rcept_dt", ""), reverse=True) + sorted(
        fallback, key=lambda row: row.get("rcept_dt", ""), reverse=True
    )


def save_document_xml(
    session: requests.Session,
    api_key: str,
    rcept_no: str,
    out_path: Path,
) -> str | None:
    if out_path.exists() and out_path.stat().st_size > 0:
        return "cached"

    response = session.get(
        "https://opendart.fss.or.kr/api/document.xml",
        params={"crtfc_key": api_key, "rcept_no": rcept_no},
        timeout=45,
    )
    response.raise_for_status()

    try:
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            xml_names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
            if not xml_names:
                return None
            xml_text = archive.read(xml_names[0]).decode("utf-8", errors="ignore")
    except zipfile.BadZipFile:
        return None

    out_path.write_text(xml_text, encoding="utf-8")
    return "downloaded"


def main() -> int:
    api_key = get_api_key()
    RAW.mkdir(parents=True, exist_ok=True)
    OUT_OF_SAMPLE.mkdir(parents=True, exist_ok=True)

    master = pd.read_csv(DATA / "company_master.csv", dtype=str).fillna("")
    filing_index = pd.read_csv(DART / "filing_index.csv", dtype=str).fillna("")
    corp_codes = pd.read_csv(DART / "corp_codes.csv", dtype=str).fillna("")

    master["stock_code"] = master["stock_code"].str.zfill(6)
    master["fiscal_year"] = master["fiscal_year"].astype(int)
    master["esg_year"] = master["esg_year"].astype(int)

    filing_index["stock_code"] = filing_index["stock_code"].str.zfill(6)
    filing_index["fiscal_year"] = filing_index["fiscal_year"].astype(int)

    backup_path = DART / "filing_index.before_repair.csv"
    if not backup_path.exists():
        shutil.copy2(DART / "filing_index.csv", backup_path)

    master_keys = master[["stock_code", "fiscal_year", "esg_year"]]
    repaired = filing_index.merge(
        master_keys,
        on=["stock_code", "fiscal_year"],
        how="inner",
        suffixes=("", "_master"),
    )
    repaired["esg_year"] = repaired["esg_year_master"].astype(int)
    repaired = repaired.drop(columns=["esg_year_master"])

    missing = master[["company_name", "stock_code", "fiscal_year", "esg_year"]].merge(
        repaired[["stock_code", "fiscal_year"]],
        on=["stock_code", "fiscal_year"],
        how="left",
        indicator=True,
    )
    missing = missing[missing["_merge"].eq("left_only")].drop(columns=["_merge"])

    session = requests.Session()
    new_rows: list[dict] = []

    for row in missing.itertuples(index=False):
        corp_match = corp_codes[corp_codes["stock_code"].eq(row.stock_code)]
        if corp_match.empty:
            raise RuntimeError(f"corp_code not found for {row.stock_code}")
        corp = corp_match.iloc[0]
        candidates = annual_report_candidates(
            disclosure_rows(session, api_key, corp["corp_code"], int(row.fiscal_year)),
            int(row.fiscal_year),
        )
        if not candidates:
            raise RuntimeError(f"annual report not found for {row.stock_code} {row.fiscal_year}")

        selected = None
        selected_status = None
        selected_xml = None
        for report in candidates:
            rcept_no = report["rcept_no"]
            xml_path = RAW / f"{row.stock_code}_{row.fiscal_year}_{rcept_no}.xml"
            status = save_document_xml(session, api_key, rcept_no, xml_path)
            if status:
                selected = report
                selected_status = status
                selected_xml = xml_path
                break
            time.sleep(0.2)

        if selected is None or selected_xml is None:
            raise RuntimeError(f"document download failed for {row.stock_code} {row.fiscal_year}")

        source_row = master[
            master["stock_code"].eq(row.stock_code)
            & master["fiscal_year"].eq(int(row.fiscal_year))
        ].index[0]
        new_rows.append(
            {
                "source_row": source_row,
                "company_name": row.company_name,
                "stock_code": row.stock_code,
                "fiscal_year": int(row.fiscal_year),
                "esg_year": int(row.esg_year),
                "corp_code": corp["corp_code"],
                "dart_corp_name": corp["corp_name"],
                "report_nm": selected.get("report_nm", ""),
                "rcept_no": selected.get("rcept_no", ""),
                "rcept_dt": selected.get("rcept_dt", ""),
                "viewer_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={selected.get('rcept_no', '')}",
                "xml_path": str(selected_xml.relative_to(ROOT)),
                "document_status": selected_status,
            }
        )
        print(
            "downloaded",
            row.stock_code,
            int(row.fiscal_year),
            selected.get("report_nm", ""),
            selected.get("rcept_no", ""),
        )
        time.sleep(0.2)

    if new_rows:
        repaired = pd.concat([repaired, pd.DataFrame(new_rows)], ignore_index=True)

    repaired["source_row"] = repaired["source_row"].astype(int)
    repaired["fiscal_year"] = repaired["fiscal_year"].astype(int)
    repaired["esg_year"] = repaired["esg_year"].astype(int)
    repaired = repaired.sort_values("source_row")

    columns = [
        "source_row",
        "company_name",
        "stock_code",
        "fiscal_year",
        "esg_year",
        "corp_code",
        "dart_corp_name",
        "report_nm",
        "rcept_no",
        "rcept_dt",
        "viewer_url",
        "xml_path",
        "document_status",
    ]
    repaired[columns].to_csv(DART / "filing_index.csv", index=False, encoding="utf-8-sig")
    (DART / "failed_filings.csv").write_text("", encoding="utf-8")

    valid_keys = set(map(tuple, master[["stock_code", "fiscal_year"]].itertuples(index=False, name=None)))
    moved: list[str] = []
    for path in RAW.glob("*.xml"):
        parts = path.stem.split("_")
        if len(parts) < 3:
            continue
        key = (parts[0], int(parts[1]))
        if key not in valid_keys:
            target = OUT_OF_SAMPLE / path.name
            if target.exists():
                target.unlink()
            shutil.move(str(path), str(target))
            moved.append(path.name)

    print(f"repaired_rows={len(repaired)} downloaded_rows={len(new_rows)} moved_out_of_sample={len(moved)}")
    if moved:
        print("moved:", ", ".join(moved))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
