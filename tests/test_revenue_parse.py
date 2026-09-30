"""매출액 표 파싱 회귀 테스트. 실제 디렉토리북에서 오류가 났던 사례(망고부스트·알에프온·원세미콘·아이디어스투실리콘)를 그대로 쓴다."""

import json

import pytest

from rag import parser
from rag.parser import money, revenue, revenue_cells_from_text, usd


def _rev(raw_text: str) -> dict:
    cells = revenue_cells_from_text(raw_text)
    assert cells is not None
    return revenue(cells)


def _hist(rev: dict) -> list:
    return [(e["연도"], e["국내"], e["해외"], e["상태"]) for e in rev["이력"]]


# PDF 원문 텍스트(get_text) 그대로: 헤더 → 값 4칸
MANGO = "매출액\n국내(원/천원)\n해외(달러/$)\n2023년\n2024년\n2023년\n2024년\n-\n21,818천원\n$100,000\n-\n투자 유치 이력\n"
RFON = "매출액\n국내(원/천원)\n해외(달러/$)\n2023년\n2024년\n2023년\n2024년\n261,000천원\n901,000천원\n-\n137,920달러 \n주요 거래처\n"
ONESEMI = "매출액\n국내(원)\n해외(달러/$)\n2023년\n2024년\n2023년\n2024년\n11.2억\n17.4억\n2.85K\n1.02M\n주요 거래처\n"
IDEUS = "매출액\n국내(원/천원)\n해외(달러/$)\n2023년\n2024년\n2023년\n2024년\nN/A\nN/A\nN/A\n4,107,122천원\n($2,928,236)\n"
ACONIC = "매출액\n국내(원/천원)\n해외(달러/$)\n2023년\n2024년\n2023년\n2024년\n508,000천원\n407,500천원\n$72,280(23.5월 환율 기준)\n$159,899\n인증 및 수상내역\n"


def test_dash_cells_do_not_shift_values_to_other_years():
    """망고부스트: 21,818천원은 2024 국내, $100,000은 2023 해외. 예전에는 2023으로 밀려 2024가 통째로 '확인불가'가 됐다."""
    rev = _rev(MANGO)
    assert _hist(rev) == [(2023, None, 100000, "공개"), (2024, 21818, None, "공개")]
    assert (rev["연도"], rev["국내"], rev["상태"]) == (2024, 21818, "공개")  # 최근 연도 매출이 잡힌다


def test_overseas_dash_in_first_year():
    """알에프온: 해외 $137,920은 2024. 예전에는 2023으로 밀려 2024 매출이 약 9억으로 과소 계산됐다."""
    assert _hist(_rev(RFON)) == [(2023, 261000, None, "공개"), (2024, 901000, 137920, "공개")]


def test_k_m_suffix_in_overseas_cells():
    """원세미콘: '2.85K'·'1.02M'이 3·1달러로 읽히던 문제."""
    assert _hist(_rev(ONESEMI)) == [(2023, 1120000, 2850, "공개"), (2024, 1740000, 1020000, "공개")]


def test_won_and_dollar_notation_uses_dollar_value():
    """아이디어스투실리콘: '4,107,122천원 ($2,928,236)' 병기에서 달러 값을 쓴다(원화를 달러로 잘못 읽던 문제)."""
    assert _hist(_rev(IDEUS)) == [(2023, None, None, "N/A"), (2024, None, 2928236, "공개")]


def test_trailing_note_in_cell():
    assert _hist(_rev(ACONIC)) == [(2023, 508000, 72280, "공개"), (2024, 407500, 159899, "공개")]


def test_unreadable_layouts_fall_back_to_none():
    assert revenue_cells_from_text("회사 개요\n설립일 2020") is None                     # 표 없음
    assert revenue_cells_from_text("매출액\n국내(원/천원)\n2023년\n주요 거래처\n") is None  # 값 칸 없음
    assert revenue_cells_from_text("매출액\n국내(원)\n해외($)\n2023년\n2024년\n2023년\n2024년\n1억\n") is None  # 칸 부족


def test_money_and_usd_units():
    assert money("-") == (None, "N/A")           # 표의 '-'는 매출 없음 (예전에는 확인불가)
    assert money("") == (None, "확인불가")        # 진짜 빈 칸만 확인불가
    assert usd("$3,500,000") == (3500000, "공개")
    assert usd("137,920달러") == (137920, "공개")
    assert usd("2.85K") == (2850, "공개") and usd("$1.02M") == (1020000, "공개")
    assert usd("2억달러") == (200000000, "공개")
    assert usd("-") == (None, "N/A") and usd("비공개") == (None, "비공개")
    assert usd("140,000천원") == (None, "확인불가")  # 원화만 적힌 해외 칸은 환산 근거 없이 달러로 바꾸지 않는다
    assert usd("4,107,122천원 ($2,928,236)") == (2928236, "공개")


def test_unknown_currency_is_not_mistaken_for_no_revenue():
    from agents._report_scoring import judge_q7, score_e2
    from rag.parser import RevenueCell
    sales = revenue([RevenueCell(year=2024, region="국내", raw="-"),
                     RevenueCell(year=2024, region="해외", raw="140,000천원")])
    assert sales["상태"] == "확인불가"
    assert score_e2({"매출액": sales})[:2] == (2, True)
    assert judge_q7({"매출액": sales})[0] == "확인불가"


def test_real_pdf_matches_expected_for_known_cases():
    """실제 PDF에서: 오류가 났던 4곳이 바로잡히고, 표가 없는 페이지는 None(기존 값 유지)."""
    import pymupdf

    pdf = parser.DATA_DIR / "raw" / "01_기업정보.pdf"
    if not pdf.exists():
        pytest.skip("data/raw/01_기업정보.pdf 없음")
    pages = parser.company_pages(pdf)
    with pymupdf.open(pdf) as doc:
        got = {f"C{n:02d}": revenue_cells_from_text(doc[p["output_page"] - 1].get_text()) for n, p in enumerate(pages, 1)}
    assert _hist(revenue(got["C03"])) == [(2023, None, 100000, "공개"), (2024, 21818, None, "공개")]
    assert _hist(revenue(got["C08"]))[1] == (2024, 901000, 137920, "공개")
    assert _hist(revenue(got["C39"]))[1] == (2024, 1740000, 1020000, "공개")
    assert got["C06"] is None  # 표 텍스트 자체가 없는 페이지
    assert sum(v is not None for v in got.values()) >= 40  # 45곳 중 40곳 이상은 원문 표에서 직접 읽는다


def test_committed_companies_json_is_repaired():
    """커밋된 기업 데이터가 원문 표와 같은지(이 브랜치가 고친 값 유지)."""
    from rag.parse_revenue_repair import COMPANIES, repair

    pdf = parser.DATA_DIR / "raw" / "01_기업정보.pdf"
    if not pdf.exists():
        pytest.skip("data/raw/01_기업정보.pdf 없음")
    records = json.loads(COMPANIES.read_text(encoding="utf-8"))
    assert repair(records, pdf) == []  # 다시 돌려도 바뀔 것이 없다 = 이미 원문과 일치


def test_repair_also_corrects_latest_summary_when_history_is_already_right():
    from rag.parse_revenue_repair import COMPANIES, repair

    pdf = parser.DATA_DIR / "raw" / "01_기업정보.pdf"
    if not pdf.exists():
        pytest.skip("data/raw/01_기업정보.pdf 없음")
    rec = next(r for r in json.loads(COMPANIES.read_text()) if r["company_id"] == "C03")
    rec["매출액"].update(국내=None, 상태="확인불가")
    assert len(repair([rec], pdf)) == 1
    assert rec["매출액"]["국내"] == 21818
    assert rec["매출액"]["상태"] == "공개"
