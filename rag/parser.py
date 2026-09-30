"""문서 로딩 (설계 2-2 문서군별 차등 처리).

- 01_기업정보 : 청킹하지 않는다. 펼침면을 좌/우 페이지로 나누고 블록 좌표(bbox)로 읽기 순서를 복원한 뒤
               LLM 구조화 추출로 1페이지 = 1기업 레코드를 만든다. (parse_company_pages)
- 02~15      : 페이지별로 같은 방식의 읽기 순서 복원 → 반복 머리말·꼬리말 제거 → 700자/100자 청킹. (load_corpus)

읽기 순서 복원 규칙
  1. 세로로 비는 구간(가로 띠)마다 영역을 나눈다.
  2. 띠 안에서 세로 여백으로 갈리는 두 영역의 줄 높이가 서로 맞지 않으면 다른 단(column)으로 보고 따로 읽는다.
  3. 줄 높이가 맞으면 표로 보고 한 줄로 읽는다 (칸 사이는 " | "). 헤더·단위·값의 행 관계를 보존하기 위함.
"""

import json
import re
from pathlib import Path
from typing import Literal

import pymupdf
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field

from config import CHUNK_OVERLAP, CHUNK_SIZE, DATA_DIR, EXCLUDED_PAGES, LLM_MODEL

ROW_TOL = 3.0       # 같은 줄로 보는 y 오차 (pt)
BAND_GAP = 10.0     # 이만큼 세로로 비면 가로 띠를 나눈다
MIN_GUTTER = 8.0    # 단 사이 최소 여백
ALIGN_RATIO = 0.6   # 양쪽 줄 높이가 이 비율 이상 맞으면 표로 본다
BULLET = re.compile(r"^\s*([-•·▪▶※○●■□◆◇]|[①-⑳]|\d+[.)]|\[)")


# ---------- 공통: 블록 → 읽기 순서 텍스트 ----------

def _blocks(page: pymupdf.Page) -> list[tuple]:
    """텍스트 블록만 (x0, y0, x1, y1, text, no, type)."""
    return [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]


def _clean(text: str) -> str:
    """블록 내부 줄바꿈 정리: 글머리 기호로 시작하면 줄을 유지하고, 아니면 이어 붙인다."""
    out = ""
    for line in (ln.strip() for ln in text.splitlines()):
        if not line:
            continue
        if not out:
            out = line
        elif out.endswith("-") and line[:1].islower():  # 영문 하이픈 줄바꿈
            out = out[:-1] + line
        elif BULLET.match(line):
            out += "\n" + line
        else:
            out += " " + line
    return out


def _rows(blocks: list[tuple]) -> list[list[tuple]]:
    rows: list[list[tuple]] = []
    for b in sorted(blocks, key=lambda b: (b[1], b[0])):
        if rows and abs(b[1] - rows[-1][0][1]) <= ROW_TOL:
            rows[-1].append(b)
        else:
            rows.append([b])
    return [sorted(r, key=lambda b: b[0]) for r in rows]


def _bands(blocks: list[tuple]) -> list[list[tuple]]:
    bands: list[list[tuple]] = []
    bottom = None
    for b in sorted(blocks, key=lambda b: b[1]):
        if bands and b[1] - bottom <= BAND_GAP:
            bands[-1].append(b)
            bottom = max(bottom, b[3])
        else:
            bands.append([b])
            bottom = b[3]
    return bands


def _aligned(a: list[tuple], b: list[tuple]) -> bool:
    ya = [r[0][1] for r in _rows(a)]
    yb = [r[0][1] for r in _rows(b)]
    small, big = sorted((ya, yb), key=len)
    hit = sum(any(abs(y - z) <= ROW_TOL for z in big) for y in small)
    return hit / len(small) >= ALIGN_RATIO


def _columns(blocks: list[tuple]) -> list[list[tuple]]:
    """세로 여백으로 갈리고 줄 높이가 안 맞으면 단을 나눈다 (재귀). 표는 나누지 않는다."""
    xs = sorted(blocks, key=lambda b: b[0])
    reach = xs[0][2]
    for b in xs[1:]:
        if b[0] - reach >= MIN_GUTTER:
            cut = (reach + b[0]) / 2
            left = [c for c in blocks if (c[0] + c[2]) / 2 < cut]
            right = [c for c in blocks if (c[0] + c[2]) / 2 >= cut]
            if left and right and not _aligned(left, right):
                return _columns(left) + _columns(right)
        reach = max(reach, b[2])
    return [blocks]


def layout_text(blocks: list[tuple]) -> str:
    """블록 좌표로 읽기 순서를 복원한 텍스트. 영역 사이는 빈 줄, 표의 칸 사이는 ' | '."""
    parts = []
    for band in _bands(blocks):
        for col in _columns(band):
            parts.append("\n".join(" | ".join(_clean(b[4]) for b in row) for row in _rows(col)))
    return "\n\n".join(p for p in parts if p.strip())


# ---------- 01_기업정보: 펼침면 → 기업 레코드 ----------

def spread_text(page: pymupdf.Page) -> str:
    """펼침면이면 좌/우 페이지를 따로 복원한다. 좌측 본문과 우측 항목이 한 줄로 섞이는 문제를 막는다."""
    blocks = _blocks(page)
    w, h = page.rect.width, page.rect.height
    if w <= h * 1.2:  # 한 쪽짜리 페이지
        return layout_text(blocks)
    mid = w / 2
    left = [b for b in blocks if (b[0] + b[2]) / 2 < mid]
    right = [b for b in blocks if (b[0] + b[2]) / 2 >= mid]
    return f"[왼쪽 페이지]\n{layout_text(left)}\n\n[오른쪽 페이지]\n{layout_text(right)}"


def printed_pages(page: pymupdf.Page) -> str | None:
    """꼬리말의 인쇄 쪽번호 (예: '010-011'). REFERENCE 표기 보조용."""
    h = page.rect.height
    nums = set()
    for b in _blocks(page):
        if b[1] > h * 0.92:
            nums |= set(re.findall(r"(?<!\d)(\d{3})(?!\d)", b[4]))
    return "-".join(sorted(nums, key=int)) if nums else None


SubDomain = Literal["ai_computing", "packaging", "power", "rf_sensor", "memory",
                    "interface", "materials", "metrology", "yield", "none"]


class Member(BaseModel):
    name: str = Field(description="이름")
    role: str = Field(description="직책 (CEO, CTO 등)")
    education: str = Field(description="학력 원문, 없으면 빈 문자열")
    career: str = Field(description="경력 원문 (여러 줄은 ' / '로 연결)")


class RevenueCell(BaseModel):
    year: int
    region: Literal["국내", "해외"]
    raw: str = Field(description="표 칸 원문 그대로. 예: '169,569천원', 'N/A', '비공개', '11.2억', '$3,500,000'")


class Investment(BaseModel):
    year: str = Field(description="투자 시기 원문 (예: '2024')")
    investors: str = Field(description="투자자 원문")
    stage: str = Field(description="투자 단계 원문 (예: 'Seed', 'Pre A', 'Series A(SAFE)')")
    amount_raw: str = Field(description="투자 금액 원문 그대로 (예: '3,000,000천원(협의 중)')")


class IPItem(BaseModel):
    kind: Literal["등록", "출원", "기타"] = Field(description="[등록번호:...]면 등록, [출원번호:...]면 출원")
    number: str = Field(description="번호만 (예: '10-2024-0153586')")
    title: str = Field(description="명칭")
    date: str = Field(description="표에 적힌 날짜 원문 (예: '2024.11')")


class CompanyExtract(BaseModel):
    name: str = Field(description="기업명 (페이지 최상단 제목)")
    homepage: str = Field(description="홈페이지 URL, 없으면 빈 문자열")
    founded: str = Field(description="설립일 원문")
    ceo: str = Field(description="대표자명")
    employees: str = Field(description="직원수 원문 (예: '13명')")
    industry: str = Field(description="업종/업태 원문")
    tech_field: str = Field(description="기술분야 원문")
    main_item: str = Field(description="메인 아이템 원문")
    sub_domain: SubDomain = Field(description=(
        "메인 아이템의 세부 분야. ai_computing=NPU·AI 가속기·데이터센터 칩, packaging=칩렛·첨단 패키징, "
        "power=전력반도체, rf_sensor=RF·센서, memory=메모리·PIM, interface=UCIe·CXL·인터커넥트 IP, "
        "materials=신소자·소재, metrology=계측·AI 공정 모니터링, yield=수율·결함 검사, none=해당 없음"))
    headline: str = Field(description="왼쪽 페이지 헤드라인과 소개글 원문")
    members: list[Member] = Field(description="주요 구성원 소개")
    revenue: list[RevenueCell] = Field(description="주요 재무현황 매출액 표의 모든 칸")
    customers: list[str] = Field(description="주요 거래처 (번호 기호 제외, '*****'·'A사'처럼 가려진 것도 그대로)")
    investments: list[Investment] = Field(description="투자 유치 이력 표의 모든 행")
    ip: list[IPItem] = Field(description="지식재산권 표의 모든 항목")
    awards: list[str] = Field(description="인증 및 수상내역, 각 항목 '날짜 내용' 형태")
    dev_progress: str = Field(description="핵심 기술력 > 개발진척도 원문 (단계별 줄은 줄바꿈 유지)")
    trl: int | None = Field(description="본문에 TRL 숫자가 글자로 적힌 경우만, 눈금만 있는 그래프 표는 null")
    innovation: str = Field(description="핵심 기술력 > 혁신성 원문")
    business_point: str = Field(description="사업 Point 원문")


EXTRACT_PROMPT = """너는 스타트업 디렉토리북 페이지에서 정보를 옮겨 적는 추출기다.

입력은 한 기업의 펼침면(왼쪽·오른쪽 페이지)을 좌표 순서로 복원한 텍스트다.
- 왼쪽 페이지: 기업명·홈페이지, 헤드라인·소개글, 회사 개요(설립일·대표자명·직원수·업종/업태·기술분야·메인 아이템·주요 구성원 소개·소재지), 주요 재무현황(매출액·주요 거래처·투자 유치 이력), 지식재산권 및 수상 현황
- 오른쪽 페이지: 사업 Point, 핵심 기술력(개발진척도·혁신성)
- 표는 한 행이 한 줄이고 칸은 ' | '로 구분된다. 나란히 놓인 두 표의 행이 한 줄에 섞여 있을 수 있으니 머리글로 구분한다.

규칙
1. 요약하거나 바꿔 쓰지 말고 원문 그대로 옮긴다. 숫자·특허번호·단위는 한 글자도 바꾸지 않는다.
2. 페이지에 없는 항목은 빈 문자열 또는 빈 리스트로 둔다. 추측으로 채우지 않는다.
3. 금액·매출은 계산하거나 단위를 바꾸지 말고 칸 원문(raw)을 그대로 넣는다.
4. TRL은 본문 문장에 'TRL 7', '기술 성숙도 레벨 7~8'처럼 숫자가 글자로 적힌 경우만 넣는다(범위면 낮은 값).
   '개발단계 (TRL) 1 2 3 ... 9'처럼 눈금만 있는 표는 막대 그래프라 값을 알 수 없으므로 null로 둔다."""


def _llm():
    from langchain_openai import ChatOpenAI  # 키 없이도 모듈 import가 되도록 지연 로딩

    # strict: 스키마의 모든 필드를 반드시 채우게 강제 (non-strict에서는 뒤쪽 필드를 빠뜨리는 경우가 있었음)
    return ChatOpenAI(model=LLM_MODEL, temperature=0).with_structured_output(
        CompanyExtract, method="json_schema", strict=True
    )


def _messages(text: str, image_b64: str | None = None) -> list:
    content: list = [{"type": "text", "text": text}]
    if image_b64:  # 텍스트 복원이 흔들리는 페이지용 보조 입력
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}})
    return [("system", EXTRACT_PROMPT), ("human", content)]


class SubDomainPick(BaseModel):
    reason: str = Field(description="메인 아이템과 분류를 연결한 한 줄 근거")
    sub_domain: SubDomain


SUBDOMAIN_PROMPT = """반도체 스타트업의 메인 아이템을 기술 문서 분류 하나에 매핑한다.
기술 요약 에이전트가 이 분류의 기술 문서만 검색하므로, 가장 가까운 분류를 반드시 하나 고른다.

- ai_computing : NPU, AI 가속기, AI 추론 칩, DPU, 데이터센터·엣지 AI 연산, AI 영상처리 반도체·IP, SoC 설계
- packaging    : 칩렛, 첨단 패키징, 패키지 기판, 수동소자 집적
- power        : 전력반도체(GaN·SiC), PMIC, BMS, 아날로그 전원 IC
- rf_sensor    : RF, 무선통신 칩, V2X, 레이더, 이미지·광 센서
- memory       : 메모리, PIM, CIM, 메모리 기반 연산
- interface    : CXL, UCIe, SerDes, 인터커넥트·고속 인터페이스 IP
- materials    : 신소자, 웨이퍼·에피, 소재, 2D 반도체
- metrology    : 계측, 공정 모니터링, 반도체 장비·장비 부품
- yield        : 수율, 결함·불량 검사 (AI 기반 검사 포함)
- none         : 위 어디에도 전혀 해당하지 않을 때만"""


def classify_sub_domains(records: list[dict]) -> list[dict]:
    """기업별 sub_domain을 전용 프롬프트로 다시 분류한다 (추출과 분리해 보수적 'none' 남발 방지)."""
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model=LLM_MODEL, temperature=0).with_structured_output(
        SubDomainPick, method="json_schema", strict=True
    )
    targets = [r for r in records if r.get("기업명")]
    inputs = [[("system", SUBDOMAIN_PROMPT),
               ("human", f"기업명: {r['기업명']}\n기술분야: {r.get('기술분야', '')}\n"
                         f"메인 아이템: {r.get('메인아이템', '')}\n사업 Point: {r.get('사업Point', '')[:600]}")]
              for r in targets]
    for r, pick in zip(targets, llm.batch(inputs, config={"max_concurrency": 5}, return_exceptions=True)):
        if isinstance(pick, Exception):
            print(f"[분류 실패] {r['company_id']}: {pick!r}")
            continue
        r["sub_domain"] = None if pick.sub_domain == "none" else pick.sub_domain
        r["sub_domain_근거"] = pick.reason
    return records


def company_pages(pdf_path: Path) -> list[dict]:
    """LLM 호출 없이 페이지별 복원 텍스트만 뽑는다 (16·20쪽 제외). 수동 점검·재추출에 사용."""
    excluded = set(EXCLUDED_PAGES.get(pdf_path.stem, []))
    page_map = _manifest_entry(pdf_path.stem).get("page_map", {})
    out = []
    with pymupdf.open(pdf_path) as pdf:
        for i, page in enumerate(pdf, start=1):
            if i in excluded:
                continue
            out.append({
                "output_page": i,
                "source_page": page_map.get(str(i), i),
                "인쇄페이지": printed_pages(page),
                "text": spread_text(page),
            })
    return out


def parse_company_pages(pdf_path: Path, only_pages: set[int] | None = None, with_image: bool = False) -> list[dict]:
    """01_기업정보: bbox 컬럼 복원 → LLM 구조화 추출 → 1페이지 = 1기업 레코드 (16·20쪽 제외).

    only_pages: 추출 페이지 번호(1부터) 일부만 다시 뽑을 때. company_id는 전체 순번 기준이라 그대로 유지된다.
    with_image: 페이지 이미지를 함께 넣어 레이아웃 인식을 보조한다 (비용 증가).
    """
    pages = company_pages(pdf_path)
    for n, p in enumerate(pages, start=1):
        p["company_id"] = f"C{n:02d}"
    targets = [p for p in pages if only_pages is None or p["output_page"] in only_pages]

    images: dict[int, str] = {}
    if with_image:
        import base64

        with pymupdf.open(pdf_path) as pdf:
            for p in targets:
                png = pdf[p["output_page"] - 1].get_pixmap(dpi=110).tobytes("png")
                images[p["output_page"]] = base64.b64encode(png).decode()

    with pymupdf.open(pdf_path) as pdf:  # 매출 표는 원문 텍스트에서 직접 읽는다(LLM은 칸-연도 짝짓기를 틀린 적이 있다)
        for p in targets:
            p["매출칸"] = revenue_cells_from_text(pdf[p["output_page"] - 1].get_text())

    llm = _llm()
    inputs = [_messages(p["text"], images.get(p["output_page"])) for p in targets]
    results = llm.batch(inputs, config={"max_concurrency": 5}, return_exceptions=True)

    records = []
    for p, r in zip(targets, results):
        if isinstance(r, Exception):
            print(f"[추출 실패] {p['company_id']} (p.{p['output_page']}): {r!r}")
            records.append({"company_id": p["company_id"], "기업명": None, "추출오류": repr(r),
                            "source_page": p["source_page"], "인쇄페이지": p["인쇄페이지"], "원문": p["text"]})
            continue
        records.append(to_record(r, p))
    return classify_sub_domains(records)


# ---------- 원문 → 계약 스키마 (docs/CONTRACTS.md 3-3) ----------

def to_record(e: CompanyExtract, page: dict) -> dict:
    rev = revenue(page.get("매출칸") or e.revenue)  # 원문 표를 읽었으면 그것을, 못 읽었으면 LLM 추출값을 쓴다
    return {
        "company_id": page["company_id"],
        "기업명": e.name.strip(),
        "홈페이지": e.homepage.strip() or None,
        "설립일": to_date(e.founded),
        "대표자명": e.ceo.strip(),
        "직원수": to_int(e.employees),
        "업종": e.industry.strip(),
        "기술분야": e.tech_field.strip(),
        "sub_domain": None if e.sub_domain == "none" else e.sub_domain,
        "메인아이템": e.main_item.strip(),
        "주요구성원": [{"이름": m.name, "직책": m.role, "학력": m.education, "경력": m.career} for m in e.members],
        "매출액": rev,
        "투자유치이력": [investment(i) for i in e.investments],
        "지식재산권": {
            "등록": [{"번호": i.number, "명칭": i.title, "일자": i.date} for i in e.ip if i.kind == "등록"],
            "출원": [{"번호": i.number, "명칭": i.title, "일자": i.date} for i in e.ip if i.kind == "출원"],
        },
        "인증수상": e.awards,
        "주요거래처": [c.strip() for c in e.customers if c.strip()],
        "개발진척도": e.dev_progress.strip(),
        "TRL": e.trl,
        "혁신성": e.innovation.strip(),
        "사업Point": e.business_point.strip(),
        "헤드라인": e.headline.strip(),
        "source_page": page["source_page"],   # 디렉토리북 원본 PDF 페이지
        "인쇄페이지": page["인쇄페이지"],       # 책에 인쇄된 쪽번호
        "원문": page["text"],                  # 복원 텍스트 (DIR 근거 발췌·수동 대조용)
    }


def to_int(raw: str | None) -> int | None:
    m = re.search(r"\d[\d,]*", raw or "")
    return int(m.group().replace(",", "")) if m else None


def to_date(raw: str | None) -> str | None:
    """'2021년 5월 25일' / '2021.05.25' → '2021-05-25'. 연·월만 있으면 있는 만큼만."""
    nums = re.findall(r"\d+", raw or "")
    if not nums or len(nums[0]) != 4:
        return (raw or "").strip() or None
    return "-".join([nums[0]] + [n.zfill(2) for n in nums[1:3]])


def money(raw: str | None) -> tuple[int | None, str]:
    """금액 원문 → (천원 단위 int, 상태). 상태: 공개 | N/A | 비공개 | 확인불가."""
    s = (raw or "").strip()
    if not s:
        return None, "확인불가"
    if s in {"-", "–", "—"}:  # 디렉토리북 표에서 '-'는 해당 연도·지역 매출 없음
        return None, "N/A"
    if re.search(r"\bn/?a\b", s, re.I):
        return None, "N/A"
    if "비공개" in s or "*" in s:
        return None, "비공개"
    m = re.search(r"\d[\d,]*(\.\d+)?", s)
    if not m:
        return None, "확인불가"
    v = float(m.group().replace(",", ""))
    unit = s[m.end():]
    if "억" in unit:
        v *= 100_000
    elif "백만" in unit:
        v *= 1_000
    elif "만" in unit:
        v *= 10
    elif "천" not in unit and "원" in unit:
        v /= 1_000
    return round(v), "공개"


_USD_MULT = {"k": 1e3, "m": 1e6, "b": 1e9, "천": 1e3, "만": 1e4, "백만": 1e6, "억": 1e8}


def usd(raw: str | None) -> tuple[int | None, str]:
    """해외 매출 칸 원문 → (달러 int, 상태). '$3,500,000'·'137,920달러'·'2.85K'·'1.02M'·'2억달러'를 읽는다."""
    s = (raw or "").strip()
    paired = re.search(r"\(\s*(\$\s*\d[\d,]*(?:\.\d+)?\s*[KkMmBb]?)\s*\)", s)
    if paired:
        s = paired.group(1)  # 같은 칸의 원화·달러 병기는 명시된 달러 값을 사용
    _, status = money(s)  # 빈 칸·'-'·N/A·비공개 판정은 money와 같다
    if status != "공개":
        return None, status
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(백만|[KkMmBb천만억])?", s)
    num = float(m.group(1).replace(",", ""))
    mult = _USD_MULT.get((m.group(2) or "").lower(), 1)
    if "원" in s[m.end():].split("(")[0] and "$" not in s and "달러" not in s:
        return None, "확인불가"  # 원화만 적힌 해외 칸을 임의 환율로 달러 값으로 바꾸지 않는다
    return round(num * mult), "공개"


# 매출액 표를 PDF 원문 텍스트에서 직접 읽는다. 표는 '헤더(연도 4칸) → 값 4칸' 순서가 고정이라
# LLM이 칸을 연도에 짝짓게 두면 '-' 칸을 건너뛰어 값이 다른 연도로 밀리는 오류가 났다(망고부스트·알에프온).
_YEAR = re.compile(r"(20\d\d)\s*년?")
_CELL = re.compile(r"^(?:[-–—]|n/?a|비공개|\*+|\$?\s*\d[\d,]*(?:\.\d+)?\s*(?:백만|[KkMmBb천만억])?\s*(?:천원|만원|백만원|억원|억|원|달러|불)?(?:\s*\(.*\))?)$", re.I)  # 금액 뒤 설명 괄호 허용: '$72,280(23.5월 환율 기준)'
_PAREN_USD = re.compile(r"^\(\s*\$\s*\d[\d,]*(?:\.\d+)?[^)]*\)$")
_SECTION_END = ("투자 유치", "주요 거래처", "지식재산", "인증 및")


def revenue_cells_from_text(raw_page: str) -> list[RevenueCell] | None:
    """페이지 원문(pymupdf get_text)의 '매출액' 표 → 칸 목록. 읽을 수 없는 형태면 None(→ LLM 결과 사용).

    지원 형태: 국내·해외 두 헤더 + 연도 4칸(국내 2, 해외 2) 또는 헤더 하나 + 연도 2칸.
    해외 칸이 '4,107,122천원'과 '($2,928,236)'로 병기되면 괄호 안 달러 값을 쓴다.
    """
    k = raw_page.find("매출액")
    if k < 0:
        return None
    lines = [l.strip() for l in raw_page[k:].splitlines()[1:] if l.strip()]
    regions, years, i = [], [], 0
    while i < len(lines) and not _CELL.match(lines[i]):  # 헤더 구간: 국내/해외 라벨과 연도
        ln = lines[i]
        if ln.startswith(("국내", "해외")):
            regions.append(ln[:2])
        elif _YEAR.fullmatch(ln):
            years.append(int(_YEAR.fullmatch(ln).group(1)))
        elif any(ln.startswith(x) for x in _SECTION_END):
            return None
        i += 1
        if years and len(years) == (4 if len(regions) == 2 else 2 if len(regions) == 1 else -1):
            break
    if not years or (len(regions), len(years)) not in {(2, 4), (1, 2)}:
        return None
    slots = [(region, year) for n, region in enumerate(regions)
             for year in years[n * 2:n * 2 + 2]]
    values: list[str] = []
    while i < len(lines) and len(values) < len(slots):
        if not _CELL.match(lines[i]):
            return None  # 값 칸이 모자라거나 형태가 다르다
        values.append(lines[i])
        i += 1
    if len(values) < len(slots):
        return None
    if slots[-1][0] == "해외" and i < len(lines) and _PAREN_USD.match(lines[i]):
        values[-1] = lines[i].strip("() ")  # 원화·달러 병기 → 달러 값
    return [RevenueCell(year=y, region=r, raw=v) for (r, y), v in zip(slots, values)]


def revenue(cells: list[RevenueCell]) -> dict:
    """매출액 표 → 최근 연도 요약 + 연도별 이력. 국내는 천원, 해외는 표기 그대로 달러."""
    by_year: dict[int, dict] = {}
    for c in cells:
        row = by_year.setdefault(c.year, {"연도": c.year, "국내": None, "해외": None, "_st": []})
        v, st = money(c.raw) if c.region == "국내" else usd(c.raw)
        row["국내" if c.region == "국내" else "해외"] = v
        row["_st"].append(st)
    if not by_year:
        return {"연도": None, "국내": None, "해외": None, "상태": "확인불가", "이력": []}
    for row in by_year.values():
        st = row.pop("_st")
        row["상태"] = ("공개" if "공개" in st else "비공개" if "비공개" in st
                      else "확인불가" if "확인불가" in st else "N/A")
    latest = by_year[max(by_year)]
    return {**latest, "해외단위": "USD", "이력": [by_year[y] for y in sorted(by_year)]}


def normalize_stage(raw: str) -> str:
    """투자 단계 원문 → 정규화 (설계 G2: Pre-A·브릿지 등 중간 라운드는 직전 단계 기준).

    'Pre A' → Seed, 'Pre B' → Series A, 'Series A-Bridge'·'Series A(SAFE)' → Series A,
    'Series A,B' → Series B, 'Angel'·'Safe'·'Seed Round' → Seed, 단계가 없는 표기('벤처투자', 'Bridge') → 확인불가.
    """
    s = raw.lower().replace(" ", "")
    ranks: list[int] = []  # 0 = Seed, 1 = Series A, 2 = Series B ...
    for letter in re.findall(r"pre-?(?:series)?([a-f])(?![a-z])", s):
        ranks.append(ord(letter) - ord("a"))  # 직전 단계
    s_wo_pre = re.sub(r"pre-?(?:series)?[a-f](?![a-z])", "", s)
    for group in re.findall(r"series([a-f](?:[,/&·][a-f])*)", s_wo_pre):
        ranks += [ord(x) - ord("a") + 1 for x in re.findall(r"[a-f]", group)]
    if not ranks and re.search(r"seed|angel|엔젤|safe|accelerat|시드", s):
        ranks.append(0)
    if not ranks:
        return "확인불가"
    top = max(ranks)
    return "Seed" if top == 0 else f"Series {chr(ord('A') + top - 1)}"


def investment(i: Investment) -> dict:
    amount, _ = money(i.amount_raw)
    return {
        "일자": i.year.strip(),   # 디렉토리북은 연도만 표기
        "단계": i.stage.strip(),  # 원문 그대로 (G2 판정은 적격성 에이전트 소관)
        "단계_정규화": normalize_stage(i.stage),
        "금액": amount,
        "금액_원문": i.amount_raw.strip(),
        "투자자": [x.strip() for x in re.split(r"[,·/]", i.investors) if x.strip()],
        "확정": not re.search(r"협의|예정|진행\s*중|계획", i.amount_raw + i.stage),
    }


# ---------- 02~15: 코퍼스 로딩·청킹 ----------

def _manifest(data_dir: Path = DATA_DIR) -> list[dict]:
    path = data_dir / "manifest.json"
    if not path.exists():
        raise FileNotFoundError("data/manifest.json이 없습니다. 먼저 `uv run python -m rag.parse_prepare --src <원본폴더>`")
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_entry(stem: str, data_dir: Path = DATA_DIR) -> dict:
    try:
        return next(m for m in _manifest(data_dir) if Path(m["file"]).stem == stem)
    except (FileNotFoundError, StopIteration):
        return {}


def _strip_boilerplate(pages: list[str]) -> list[str]:
    """여러 페이지에 반복되는 머리말·꼬리말 줄과 쪽번호만 있는 줄을 지운다."""
    if len(pages) < 3:
        return pages
    counts: dict[str, int] = {}
    for text in pages:
        for line in set(text.splitlines()):
            counts[line.strip()] = counts.get(line.strip(), 0) + 1
    repeated = {ln for ln, c in counts.items() if ln and c >= max(3, len(pages) // 2)}
    return ["\n".join(ln for ln in text.splitlines()
                      if ln.strip() not in repeated and not re.fullmatch(r"\s*\d{1,3}\s*", ln))
            for text in pages]


def load_corpus(data_dir: Path = DATA_DIR) -> list[Document]:
    """02~15: 700자/100자 청킹. metadata = doc_id, doc_type, sub_domain, company_id, title, publisher, pub_year,
    source_type, url, chunk_id, source_page(원본), output_page(추출본)."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, separators=["\n\n", "\n", ". ", " ", ""]
    )
    docs: list[Document] = []
    for m in _manifest(data_dir):
        if m["doc_type"] == "company":
            continue
        with pymupdf.open(data_dir / "raw" / m["file"]) as pdf:
            pages = _strip_boilerplate([layout_text(_blocks(p)) for p in pdf])
        meta = {k: m.get(k) for k in ("doc_id", "doc_type", "sub_domain", "title", "publisher",
                                      "pub_year", "source_type", "url")}
        n = 0
        for i, text in enumerate(pages, start=1):
            if len(text.strip()) < 30:  # 차트·이미지만 있는 페이지
                continue
            for chunk in splitter.split_text(text):
                n += 1
                docs.append(Document(page_content=chunk, metadata={
                    **meta, "company_id": None, "chunk_id": f"{m['doc_id']}-{n:04d}",
                    "source_page": m["page_map"].get(str(i), i), "output_page": i,
                }))
    return docs


def company_documents(companies: list[dict], data_dir: Path = DATA_DIR) -> list[Document]:
    """기업 레코드 45건을 색인용 Document로 (설계 3-4 벡터 수 = 청크 + 기업 레코드 45건). 기업당 1개."""
    m = _manifest_entry("01_기업정보", data_dir)
    meta = {k: m.get(k) for k in ("doc_type", "title", "publisher", "pub_year", "source_type", "url")}
    docs = []
    for c in companies:
        if not c.get("기업명"):
            continue
        text = (f"{c['기업명']} | 기술분야: {c.get('기술분야', '')} | 메인 아이템: {c.get('메인아이템', '')}\n"
                f"사업 Point: {c.get('사업Point', '')}")[:CHUNK_SIZE]
        docs.append(Document(page_content=text, metadata={
            **meta, "doc_id": "01", "doc_type": "company", "sub_domain": c.get("sub_domain"),
            "company_id": c["company_id"], "chunk_id": f"01-{c['company_id']}",
            "source_page": c.get("source_page"), "output_page": None,
        }))
    return docs


def directory_evidence(company: dict, seq: int, excerpt: str, checked: str) -> dict:
    """디렉토리북 근거 레코드 (docs/CONTRACTS.md 3-4, 접두어 DIR). 다른 레인이 DIR 근거를 만들 때 사용."""
    m = _manifest_entry("01_기업정보")
    return {
        "근거ID": f"DIR-{company['company_id']}-{seq:02d}",
        "출처명": m.get("title", "2025년 초격차 스타트업 1000+ 프로젝트 디렉토리북 1권"),
        "publisher": m.get("publisher", "창업진흥원"),
        "pub_year": m.get("pub_year", 2025),
        "source_type": m.get("source_type", "기관 보고서"),
        "url": m.get("url"),
        "source_page": company.get("source_page"),
        "확인일": checked,
        "원문발췌": excerpt[:300],
        "chunk_id": None,
    }
