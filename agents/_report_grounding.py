"""서술 검수: 경력 기간을 추정하지 않고, 확인 불가와 미공개를 구별한다."""


def disclosure_text(text: str) -> str:
    for before, after in (
        ("공개되지 않았습니다", "이번 평가에서 확인하지 못했습니다"),
        ("공개되지 않았으며", "이번 평가에서 확인되지 않았으며"),
        ("공개되지 않아", "이번 평가에서 확인되지 않아"),
        ("공개되지 않았다", "이번 평가에서 확인되지 않았다"),
        ("미공개", "이번 평가에서 확인되지 않은 상태"),
    ):
        text = text.replace(before, after)
    return text


def citations(ids: list[str]) -> str:
    return f" [{', '.join(dict.fromkeys(ids))}]" if ids else ""


def team_text(company: dict, scorecard: dict, directory_ids: list[str]) -> str:
    lines = []
    for member in company.get("주요구성원") or []:
        facts = "; ".join(str(member[k]).strip() for k in ("학력", "경력") if member.get(k))
        lines.append(f"{member.get('직책', '')} {member.get('이름', '')}: {facts or '상세 학력·경력 확인 필요'}."
                     + citations(directory_ids))
    item = scorecard.get("items", {}).get("A2", {})
    if item:
        lines.append(f"팀 완성도 항목은 {item['점수']}점(5점 만점)으로 평가했다. "
                     "경력 기간과 실제 역할 수행 이력은 투자 전 별도로 확인해야 한다." + citations(directory_ids))
    return "\n\n".join(lines) or "구성원 정보 확인 필요."


def risk_text(record: dict, directory_ids: list[str]) -> str:
    labels = {
        "A1": "핵심 구성원의 기술 전문성과 경력 증빙을 추가 확인해야 함",
        "A2": "기술·사업 담당의 실제 역할과 팀 구성을 추가 확인해야 함",
        "A3": "대표의 해당 분야 경력과 활동 기간을 추가 확인해야 함",
        "B1": "직접 목표 시장의 규모·성장률을 확인하지 못해 시장성 판단에 한계가 있음",
        "B2": "고객이 구매할 이유와 실제 비용·성능 이점을 추가 확인해야 함",
        "B3": "해외 매출·거래처·협력 관계를 추가 확인해야 함",
        "C1": "제품이 해결하는 문제와 효과를 검증할 자료를 추가 확보해야 함",
        "C2": "제품 개발 단계와 납품·상용화 실적을 추가 확인해야 함",
        "C3": "핵심 성능 수치·측정조건·공인 벤치마크 근거가 부족해 기술 성능을 추가 검증해야 함",
        "D1": "핵심 지식재산의 등록 여부와 권리 범위를 추가 확인해야 함",
        "D2": "동일 조건의 경쟁 제품 비교 근거가 부족해 기술 우위를 확인하지 못함",
        "D3": "공인 인증·수상·투자 참여의 실제 증빙을 추가 확인해야 함",
        "E1": "실제 거래처·PoC·계약 관계를 추가 확인해야 함",
        "E2": "최근 연도 매출과 매출 구성을 확인하지 못해 사업 실적을 추가 확인해야 함",
        "E3": "확정 투자 금액과 투자자 정보를 추가 확인해야 함",
    }
    scorecard = record["scorecard"]
    rows = []
    for key in scorecard.get("unknown_items", []):
        if key in labels:
            rows.append("- " + labels[key] + citations(scorecard["items"][key].get("근거ID") or directory_ids))
    revenue = (record.get("checklist") or {}).get("Q7") or {}
    if revenue.get("판정") == "PARTIAL":
        rows.append("- " + revenue["근거"].replace(" (코드 산출)", "")
                    + "; 디렉토리북 기록상 매출 규모가 작아 최신 매출과 반복 매출 여부를 추가 확인해야 함"
                    + citations(revenue.get("근거ID") or directory_ids))
    maturity = (record.get("technology_analysis") or {}).get("제품성숙도") or {}
    if maturity.get("TRL") is None:
        rows.append("- TRL 수치를 확인하지 못했으므로 양산·납품 실적과 실제 제품 개발 단계를 추가 확인해야 함"
                    + citations(directory_ids))
    return "\n".join(rows) or "확인된 분석 한계와 투자 조건을 별도로 검토해야 한다."


def competition_text(record: dict) -> str:
    analysis = record.get("competitor_analysis") or {}
    products = analysis.get("경쟁제품") or []
    rows = []
    for product in products:
        rows.append(f"검토 제품: {product['기업명']} {product['제품']}."
                    + citations(product.get("근거ID") or []))
    if not rows:
        rows.append("이번 검색에서 출처가 확인되는 직접 경쟁 제품을 확보하지 못했다.")
    if not analysis.get("비교표"):
        rows.append("기업과 경쟁 제품의 같은 지표·측정조건을 확인하지 못해 정량 우위를 판단하지 않았다.")
    else:
        rows.append("출처와 지표명·값을 확인한 공개 사양을 비교했다. 서로 다른 측정조건에서는 우열을 단정할 수 없다.")
    rows.append("완제품·서버 공급사와 핵심 반도체 개발사의 역할이 다를 수 있어, 직접 경쟁 관계와 제품 구성을 투자 전에 추가 확인해야 한다.")
    return "\n\n".join(rows)


def summary_text(record: dict, strengths: list, directory_ids: list[str]) -> str:
    parts = [f"{s.area}: {s.fact}" for s in strengths]
    text = "기업 자료에서 확인한 주요 평가 근거는 " + "; ".join(parts) + "." + citations(directory_ids)
    text += "\n\n투자 유치·매출·제품 개발 정보는 디렉토리북 작성 시점의 기록이며, 최신 투자 단계와 Exit 여부는 조사 기준일의 외부 근거로 별도 확인했다."
    text += "\n\n실측 성능, 개발 단계의 최신 상태, 목표 시장과 동일 조건의 경쟁 비교는 투자 전에 추가 확인해야 한다."
    return text
