"""레인 간 데이터 계약(docs/CONTRACTS.md 3장)의 Pydantic 모델. 🔒 보호 파일.

- 필드명은 계약 키(한글) 그대로 → `model_dump()` 결과를 그대로 State에 넣는다.
- 클래스명은 영문 필수: create_agent(response_format=ToolStrategy(모델))에서 클래스명이 OpenAI 함수명이 되며
  `^[a-zA-Z0-9_-]+$`만 허용된다 (docs/TROUBLESHOOTING.md).
- `dict[str, ...]` 필드 금지: 임의 키 객체는 OpenAI 구조화 출력에서 거부될 수 있어 list[...]로 표현한다.
- 판정 규칙은 LLM이 아니라 검증기(model_validator)가 강제한다 (설계: 판정은 코드로 처리).
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from config import ITEMS, UNKNOWN_SCORE

TechDomain = Literal[
    "ai_computing", "packaging", "power", "rf_sensor", "memory", "interface", "materials", "metrology", "yield"
]


def _ids():
    return Field(default_factory=list, description="인용한 근거ID 목록. 검색 결과에 표시된 [TEC-...] 등 ID만 쓴다. 추측 금지")


# ---------- 3-3. 기업 레코드 (A) ----------

class Member(BaseModel):
    이름: str
    직책: str
    학력: str = ""
    경력: str = ""


class Revenue(BaseModel):
    연도: int | None = None
    국내: int | None = Field(None, description="천원 단위")
    해외: int | None = Field(None, description="해외단위에 따른 금액; 디렉토리북은 USD")
    해외단위: Literal["USD", "천원", "KRW_THOUSAND"] = Field("KRW_THOUSAND", description="환산 근거 없이 다른 통화와 합산하지 않는다")
    상태: Literal["공개", "N/A", "비공개", "확인불가"] = Field(description="N/A=매출 없음, 비공개·확인불가=확인할 수 없음")


class Funding(BaseModel):
    일자: str | None = Field(None, description="YYYY-MM-DD")
    단계: str = Field(description="Seed, Pre-A, Series A 등")
    금액: int | None = Field(None, description="천원 단위")
    투자자: list[str] = Field(default_factory=list)
    확정: bool = Field(description="'협의 중' 등 미확정이면 false")


class Patent(BaseModel):
    번호: str
    명칭: str = ""


class IPRights(BaseModel):
    등록: list[Patent] = Field(default_factory=list)
    출원: list[Patent] = Field(default_factory=list)


class CompanyRecord(BaseModel):
    company_id: str = Field(description="C01~C45. 코드가 채운다")
    기업명: str
    홈페이지: str | None = None
    설립일: str | None = Field(None, description="YYYY-MM-DD")
    대표자명: str
    직원수: int | None = None
    업종: str
    기술분야: str
    sub_domain: TechDomain | None = Field(None, description="기술분야에 대응하는 기술 문서 분야")
    메인아이템: str
    주요구성원: list[Member] = Field(default_factory=list)
    매출액: Revenue
    투자유치이력: list[Funding] = Field(default_factory=list)
    지식재산권: IPRights = Field(default_factory=IPRights)
    인증수상: list[str] = Field(default_factory=list)
    주요거래처: list[str] = Field(default_factory=list)
    개발진척도: str
    TRL: int | None = Field(None, ge=1, le=9, description="명시되지 않으면 null")
    혁신성: str
    사업Point: str
    source_page: int = Field(description="원본 페이지. 코드가 채운다")


# ---------- 3-4. 근거 레코드 (코드가 생성) ----------

class Evidence(BaseModel):
    근거ID: str
    출처명: str
    publisher: str | None = None
    pub_year: int | str | None = None
    source_type: Literal["기관 보고서", "학술 논문", "웹페이지"]
    url: str | None = None
    source_page: int | None = None
    확인일: str
    원문발췌: str = Field(max_length=300)
    chunk_id: str | None = None


# ---------- 3-5. 적격성 (C) ----------

class GCheck(BaseModel):
    결과: Literal["충족", "미충족", "확인불가"]
    사유: str
    근거ID: list[str] = _ids()


class Eligibility(BaseModel):
    """자격 요건 G1~G4 판정. 판정은 조건별 결과에서 자동 계산된다."""

    G1: GCheck = Field(description="비상장: DART 종목코드 없음")
    G2: GCheck = Field(description="최신 투자 단계가 Seed~Series C. Pre-A·브릿지는 직전 단계 기준")
    G3: GCheck = Field(description="M&A 등 Exit 이전")
    G4: GCheck = Field(description="메인 아이템이 AI 연산 칩, AI 워크로드 가속, AI 기반 공정 개선 중 하나")
    사유: str
    근거ID: list[str] = _ids()
    판정: Literal["적격", "부적격", "확인필요"] = Field("확인필요", description="자동 계산되므로 입력 불필요")

    @model_validator(mode="after")
    def _decide(self):
        results = [g.결과 for g in (self.G1, self.G2, self.G3, self.G4)]
        self.판정 = "부적격" if "미충족" in results else "확인필요" if "확인불가" in results else "적격"
        return self


# ---------- 3-6. 기술·시장 (D) ----------

class Maturity(BaseModel):
    TRL: int | None = Field(None, ge=1, le=9)
    단계: str = Field(description="연구, 시제품, PoC, 양산 등")


class Metric(BaseModel):
    지표명: str
    값: str
    측정조건: str = Field(description="공정, 정밀도, 워크로드 등. 모르면 '미기재'")
    검증수준: str = Field(description="자체 발표, 공인 벤치마크, 제3자 검증 등")
    근거ID: list[str] = _ids()


class Benchmark(BaseModel):
    지표명: str
    업계기준: str
    비교결과: Literal["상회", "동등", "하회", "비교불가"]
    근거ID: list[str] = _ids()


class TechnologyAnalysis(BaseModel):
    """기업 주장 성능을 업계 로드맵 기준값과 대조한 기술 분석."""

    핵심기술: str
    제품성숙도: Maturity
    성능지표: list[Metric] = Field(default_factory=list)
    기준대조: list[Benchmark] = Field(default_factory=list)
    강점: list[str] = Field(default_factory=list)
    한계: list[str] = Field(default_factory=list)
    미확인정보: list[str] = Field(default_factory=list)
    근거ID: list[str] = _ids()
    근거충분: bool = Field(description="핵심 지표를 업계 기준과 대조할 근거를 검색 결과에서 찾았으면 true. false면 질의를 바꿔 재검색한다")


class Figure(BaseModel):
    값: str
    기준연도: int | None = None
    근거ID: list[str] = _ids()


class Growth(BaseModel):
    값: str = Field(description="예: CAGR 12%")
    기간: str = Field(description="예: 2025-2030")
    근거ID: list[str] = _ids()


class MarketAnalysis(BaseModel):
    """기업의 시장 주장을 시장 보고서와 대조한 시장성 분석."""

    목표고객: list[str] = Field(default_factory=list)
    시장규모: Figure
    성장률: Growth
    사업모델: str
    글로벌확장성: str
    성장요인: list[str] = Field(default_factory=list)
    위험: list[str] = Field(default_factory=list)
    미확인정보: list[str] = Field(default_factory=list)
    근거ID: list[str] = _ids()
    근거충분: bool = Field(description="시장 규모·성장률 근거를 검색 결과에서 찾았으면 true")


# ---------- 3-7. 경쟁사 (C) ----------

class NamedValue(BaseModel):
    이름: str
    값: str


class CompetitorProduct(BaseModel):
    기업명: str
    제품: str
    핵심지표값: list[NamedValue] = Field(default_factory=list, description="이름=지표명, 값=수치")
    근거ID: list[str] = _ids()


class CompareRow(BaseModel):
    지표: str
    대상기업: str = Field(description="평가 대상 기업의 값")
    경쟁사: list[NamedValue] = Field(default_factory=list, description="이름=경쟁사명, 값=수치")


class CompetitorAnalysis(BaseModel):
    """경쟁 제품과의 지표 비교."""

    경쟁제품: list[CompetitorProduct] = Field(default_factory=list)
    비교표: list[CompareRow] = Field(default_factory=list)
    우위: list[str] = Field(default_factory=list)
    열위: list[str] = Field(default_factory=list)
    비교조건: str
    비교한계: str
    근거ID: list[str] = _ids()


# ---------- 3-8. 체크리스트·스코어카드·판단 (E) ----------

class ChecklistItem(BaseModel):
    판정: Literal["YES", "PARTIAL", "NO", "확인불가"]
    근거: str
    근거ID: list[str] = _ids()


class Checklist(BaseModel):
    """Bessemer 기반 체크리스트 11문항 (판정에는 반영하지 않음)."""

    Q1: ChecklistItem = Field(description="목표 시장이 구체적인가?")
    Q2: ChecklistItem = Field(description="구체적인 문제를 해결하는가?")
    Q3: ChecklistItem = Field(description="고객이 구매할 이유가 있는가?")
    Q4: ChecklistItem = Field(description="차별성이 있는가?")
    Q5: ChecklistItem = Field(description="주요 구성원을 신뢰할 수 있는가?")
    Q6: ChecklistItem = Field(description="초기 고객이 있는가?")
    Q7: ChecklistItem = Field(description="매출이 발생하는가?")
    Q8: ChecklistItem = Field(description="글로벌 기회가 있는가?")
    Q9: ChecklistItem = Field(description="창업자가 해당 분야에 꾸준히 몸담아 왔는가?")
    Q10: ChecklistItem = Field(description="개발이 어디까지 진행됐는가?")
    Q11: ChecklistItem = Field(description="외부에서 검증받았는가?")


class ScoreItem(BaseModel):
    점수: int = Field(ge=1, le=5, description="1~5. 2·4점은 인접 기준 사이")
    채점이유: str
    근거ID: list[str] = _ids()
    확인불가: bool = Field(False, description="자료에 없어 판단할 수 없으면 true (점수는 자동으로 2)")

    @model_validator(mode="after")
    def _unknown_is_two(self):
        if self.확인불가:
            self.점수 = UNKNOWN_SCORE
        return self


class ScorecardItems(BaseModel):
    """스코어카드 15개 세부 항목. 총점·대분류 평균은 코드(total_score)가 계산한다."""

    A1: ScoreItem = Field(description="창업자-기술 전문성")
    A2: ScoreItem = Field(description="창업자-팀 완성도")
    A3: ScoreItem = Field(description="창업자-분야 몰입도")
    B1: ScoreItem = Field(description="시장성-시장 규모·성장성")
    B2: ScoreItem = Field(description="시장성-고객 가치")
    B3: ScoreItem = Field(description="시장성-글로벌 확장성")
    C1: ScoreItem = Field(description="기술-문제 해결력")
    C2: ScoreItem = Field(description="기술-개발 성숙도")
    C3: ScoreItem = Field(description="기술-분야별 핵심 기술 지표")
    D1: ScoreItem = Field(description="경쟁-지식재산")
    D2: ScoreItem = Field(description="경쟁-경쟁사 대비 차별성")
    D3: ScoreItem = Field(description="경쟁-외부 검증")
    E1: ScoreItem = Field(description="실적-고객 확보")
    E2: ScoreItem = Field(description="실적-매출")
    E3: ScoreItem = Field(description="실적-투자 유치")

    def scores(self) -> dict[str, int]:
        """total_score()에 넣을 {항목: 점수}."""
        return {i: getattr(self, i).점수 for items in ITEMS.values() for i in items}

    def unknown_items(self) -> list[str]:
        return [i for items in ITEMS.values() for i in items if getattr(self, i).확인불가]


class DecisionDetails(BaseModel):
    판단사유: str
    주요위험: list[str] = Field(default_factory=list)
    추가확인사항: list[str] = Field(default_factory=list)
    재검토조건: list[str] = Field(default_factory=list)


class InvestmentJudgement(BaseModel):
    """투자 판단 LLM 출력: 체크리스트 → 스코어카드 채점 → 판단 사유. 판정(decision)은 코드가 계산."""

    checklist: Checklist
    scorecard: ScorecardItems
    decision_details: DecisionDetails
