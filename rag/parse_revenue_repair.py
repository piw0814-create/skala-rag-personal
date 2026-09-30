"""기존 data/processed/companies.json의 매출액만 PDF 원문 표로 다시 계산해 고친다 (LLM 호출·비용 없음).

    uv run python -m rag.parse_revenue_repair            # 바뀔 내용만 보여 준다(파일은 그대로)
    uv run python -m rag.parse_revenue_repair --write    # companies.json에 반영

원문 표를 읽을 수 있는 기업만 바꾸고, 표가 없거나 형태가 달라 못 읽는 기업은 기존 값을 그대로 둔다.
배경: LLM이 표 칸을 연도에 잘못 짝지어(‘-’ 칸을 건너뜀) 값이 다른 연도로 밀리거나, K·M 표기·원화 병기를 잘못 읽은 기업이 있었다.
"""

import argparse
import json
from pathlib import Path

import pymupdf

from config import DATA_DIR
from runtime import write_json
from rag.parser import company_pages, revenue, revenue_cells_from_text

COMPANIES = DATA_DIR / "processed" / "companies.json"
PDF = DATA_DIR / "raw" / "01_기업정보.pdf"


def _key(rev: dict) -> list:
    return [(e["연도"], e["국내"], e["해외"], e["상태"]) for e in rev.get("이력") or []]


def repair(records: list[dict], pdf: Path = PDF) -> list[tuple[str, str, list, list]]:
    """records를 제자리에서 고친다. 반환: 바뀐 기업 [(company_id, 기업명, 이전, 이후)]."""
    pages = company_pages(pdf)
    changed = []
    with pymupdf.open(pdf) as doc:
        for rec in records:
            n = int(rec["company_id"][1:])
            cells = revenue_cells_from_text(doc[pages[n - 1]["output_page"] - 1].get_text())
            if cells is None:
                continue
            new = revenue(cells)
            if new != rec["매출액"]:
                changed.append((rec["company_id"], rec["기업명"], _key(rec["매출액"]), _key(new)))
                rec["매출액"] = new
    return changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="companies.json에 저장")
    args = ap.parse_args()
    records = json.loads(COMPANIES.read_text(encoding="utf-8"))
    changed = repair(records)
    for cid, name, old, new in changed:
        print(f"- {cid} {name}\n    이전: {old}\n    이후: {new}")
    print(f"\n바뀐 기업 {len(changed)}곳 / 전체 {len(records)}곳")
    if args.write:
        write_json(COMPANIES, records)
        print(f"저장: {COMPANIES}")
    else:
        print("(--write를 붙이면 저장합니다)")


if __name__ == "__main__":
    main()
