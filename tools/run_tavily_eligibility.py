"""Search G1-G3 public evidence for companies that passed the G4 screen."""

import hashlib
import json
import os
import sys
import tempfile
import argparse
import io
import re
import time
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.competitor import _load_cache, _tavily_search  # noqa: E402
from config import LLM_MODEL  # noqa: E402
from llm import get_llm
from tools.eligibility_cache import company_hash

COMPANIES_PATH = ROOT / "data" / "processed" / "companies.json"
G4_PATH = ROOT / "data" / "processed" / "g4_screening_results.json"
OUTPUT_PATH = ROOT / "data" / "processed" / "external_eligibility_results.json"
DART_CACHE_PATH = ROOT / ".cache" / "dart_corp_codes.json"
DART_CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"

EXTERNAL_REVIEW_PROMPT = """
회사 후보의 외부 자격 요건을 검색 결과만으로 평가한다. 검색 결과 밖의 지식은 사용하지 않는다.

G1 비상장: 거래소(KRX/KIND) 또는 DART의 공식 페이지가 상장 상태를 명확히 뒷받침하면 상장 기업은 미충족이다. 공식 자료가 비상장 상태를 직접 확인할 때만 충족이다. 검색 결과가 없다는 이유로 충족을 주지 말고, 공식 근거가 부족하면 확인불가로 둔다.
G2 투자 단계: 최신 투자 라운드가 Series C 또는 그 이전이면 충족, Series D 이상이면 미충족이다. Pre-A는 Series A 직전 단계로 본다. Bridge는 검색 결과에 직전 라운드가 명시될 때만 그 단계에 붙인다. 최신 라운드와 날짜를 신뢰할 만한 출처에서 찾지 못하면 확인불가다.
G3 Exit: IPO/상장, 인수·합병으로 지배권이 이전된 사실이 확인되면 미충족이다. 현재 독립적으로 운영 중이라는 신뢰할 만한 최신 근거가 있고 Exit 근거가 없을 때 충족으로 둘 수 있다. 단순히 검색 결과에 Exit 소식이 없는 것만으로 충족 처리하지 않는다.

기업명 동명이인 가능성을 확인하고 홈페이지 도메인·제품 설명이 맞는 결과만 쓴다. 각 판정에는 입력 검색 결과 URL만 인용한다. 근거 URL이 없으면 확인불가로 둔다. 결과는 충족/미충족/확인불가, 간결한 사유, 근거URL 목록으로 출력한다.
""".strip()


class CriterionResult(BaseModel):
    결과: str
    사유: str
    근거URL: list[str] = Field(default_factory=list)


class ExternalResult(BaseModel):
    G1: CriterionResult
    G2: CriterionResult
    G3: CriterionResult


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
        temp_path = Path(f.name)
    temp_path.replace(path)


def _queries(company: dict) -> dict[str, str]:
    name = company.get("기업명", "")
    domain = urlparse(company.get("홈페이지") or "").netloc.removeprefix("www.")
    product = company.get("메인아이템", "")
    identity = f' "{domain}" {product}' if domain else f' "{product}"'
    return {
        "G1_G3": f'"{name}"{identity} 상장 종목코드 KRX 코스닥 코스피 인수 합병 피인수 IPO Exit',
        "G2": f'"{name}"{identity} 최근 투자 유치 투자 라운드 Series 투자 단계',
    }


def _collect_sources(company: dict, api_key: str) -> dict:
    cache = _load_cache()
    sources = {}
    failures = []
    for query_type, query in _queries(company).items():
        try:
            response = _tavily_search(query, api_key, cache)
            rows = []
            for row in response.get("results", [])[:5]:
                url = row.get("url")
                if not url:
                    continue
                source = {
                    "title": row.get("title", ""),
                    "url": url,
                    "content": row.get("raw_content") or row.get("content") or "",
                    "published_date": row.get("published_date"),
                }
                sources[url] = source
                rows.append(url)
            failures.append({"query_type": query_type, "urls": rows})
        except RuntimeError as exc:
            failures.append({"query_type": query_type, "error": str(exc), "urls": []})
    return {"sources": list(sources.values()), "query_log": failures}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _review_hash(company: dict) -> str:
    return _digest({
        "company": {
            key: company.get(key)
            for key in ("company_id", "기업명", "홈페이지", "메인아이템", "사업Point", "투자유치이력")
        },
        "prompt": EXTERNAL_REVIEW_PROMPT,
        "model": LLM_MODEL,
    })


def _load_old_results() -> dict[str, dict]:
    if not OUTPUT_PATH.exists():
        return {}
    try:
        payload = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        return {row["company_id"]: row for row in payload.get("results", [])}
    except (OSError, json.JSONDecodeError, KeyError):
        return {}


def _normalized_company_name(value: str) -> str:
    value = value.lower().replace("㈜", "주식회사")
    value = re.sub(r"\(주\)|주식회사|유한회사|\(유\)", "", value)
    return re.sub(r"[^0-9a-z가-힣]", "", value)


def _load_dart_corp_codes(api_key: str, refresh: bool = False) -> list[dict]:
    """Load OpenDART's official corp-code list, caching it for one day."""
    if not refresh and DART_CACHE_PATH.exists():
        try:
            cached = json.loads(DART_CACHE_PATH.read_text(encoding="utf-8"))
            if time.time() - cached.get("fetched_at", 0) < 86400 and cached.get("companies"):
                return cached["companies"]
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    request = Request(
        f"{DART_CORP_CODE_URL}?crtfc_key={api_key}",
        headers={"User-Agent": "skala-rag/1.0", "Accept": "application/zip"},
    )
    with urlopen(request, timeout=60) as response:
        archive = response.read()
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        xml_name = next((name for name in zf.namelist() if name.lower().endswith(".xml")), None)
        if not xml_name:
            raise RuntimeError("OpenDART 응답 ZIP에서 기업코드 XML을 찾지 못했습니다.")
        xml_data = zf.read(xml_name)
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_data)
    companies = []
    for item in root.findall(".//list"):
        companies.append({
            "corp_code": (item.findtext("corp_code") or "").strip(),
            "corp_name": (item.findtext("corp_name") or "").strip(),
            "stock_code": (item.findtext("stock_code") or "").strip(),
            "modify_date": (item.findtext("modify_date") or "").strip(),
        })
    if not companies:
        raise RuntimeError("OpenDART 기업코드 응답에 기업 목록이 없습니다.")
    DART_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(DART_CACHE_PATH, {"fetched_at": time.time(), "companies": companies})
    return companies


def _dart_g1(company: dict, dart_companies: list[dict], evidence: list[dict]) -> dict:
    target = _normalized_company_name(company.get("기업명", ""))
    matches = []
    for row in dart_companies:
        dart_name = _normalized_company_name(row["corp_name"])
        # OpenDART names sometimes append a Latin legal/brand suffix, e.g. XCENAInc.
        dart_korean_name = re.sub(r"[a-z0-9]+$", "", dart_name)
        if target == dart_name or (dart_korean_name and target == dart_korean_name):
            matches.append(row)
    if len(matches) != 1:
        return {
            "결과": "확인불가",
            "사유": "OpenDART 기업코드 목록에서 정확히 일치하는 단일 법인을 찾지 못함",
            "근거ID": [],
        }
    corp = matches[0]
    listed = bool(corp["stock_code"])
    evidence_id = f"ELG-{company['company_id']}-DART"
    evidence[:] = [item for item in evidence if item.get("근거ID") != evidence_id]
    evidence.append({
        "근거ID": evidence_id,
        "출처명": "금융감독원 OpenDART 고유번호 목록",
        "publisher": "opendart.fss.or.kr",
        "pub_year": date.today().year,
        "source_type": "공공데이터 API",
        "url": DART_CORP_CODE_URL,
        "source_page": None,
        "확인일": date.today().isoformat(),
        "원문발췌": f"corp_name={corp['corp_name']}; stock_code={corp['stock_code'] or '(공란)'}; corp_code={corp['corp_code']}; modify_date={corp['modify_date']}",
        "chunk_id": None,
    })
    if listed:
        return {"결과": "미충족", "사유": f"OpenDART 고유번호 목록에서 종목코드 {corp['stock_code']}가 확인되어 상장사로 판정", "근거ID": [evidence_id]}
    return {"결과": "충족", "사유": "OpenDART 고유번호 목록에서 법인은 일치하고 종목코드는 공란으로 확인되어 비상장으로 판정", "근거ID": [evidence_id]}


def _domain_g1(company: dict, evidence: list[dict]) -> dict | None:
    """Use a company's own website only when it explicitly says it is listed/unlisted."""
    homepage = company.get("홈페이지") or ""
    domain = urlparse(homepage if "://" in homepage else "https://" + homepage).netloc.removeprefix("www.")
    if not domain:
        return None
    matches = []
    for entry in _load_cache().values():
        for source in entry.get("response", {}).get("results", []):
            url = source.get("url") or ""
            host = urlparse(url).netloc.removeprefix("www.")
            if host != domain and not host.endswith("." + domain):
                continue
            content = " ".join((source.get("raw_content") or source.get("content") or "").split())
            title = source.get("title") or host
            listed_terms = ("listed company", "listed on kosdaq", "상장사", "코스닥 상장", "코스피 상장", "종목코드")
            unlisted_terms = ("unlisted company", "비상장 기업", "비상장사", "비상장 회사")
            if any(term in content.lower() for term in listed_terms + unlisted_terms):
                matches.append((url, title, content, listed_terms, unlisted_terms))
    if len(matches) != 1:
        return None
    url, title, content, listed_terms, unlisted_terms = matches[0]
    lower = content.lower()
    unlisted = any(term in lower for term in unlisted_terms)
    listing_text = lower
    for term in unlisted_terms:
        listing_text = listing_text.replace(term, "")
    listed = any(term in listing_text for term in listed_terms)
    if listed == unlisted:
        return None
    evidence_id = f"ELG-{company['company_id']}-WEB-G1"
    evidence.append({
        "근거ID": evidence_id,
        "출처명": title,
        "publisher": domain,
        "pub_year": date.today().year,
        "source_type": "기업 공식 웹사이트",
        "url": url,
        "source_page": None,
        "확인일": date.today().isoformat(),
        "원문발췌": content[:300],
        "chunk_id": None,
    })
    if listed:
        return {"결과": "미충족", "사유": "기업 공식 웹사이트가 상장사임을 명시함", "근거ID": [evidence_id]}
    return {"결과": "충족", "사유": "기업 공식 웹사이트가 비상장임을 명시함", "근거ID": [evidence_id]}


def _correct_g2_stage(row: dict) -> None:
    """Enforce the documented Series C-or-earlier rule on clearly stated stages."""
    item = row.get("criteria", {}).get("G2", {})
    reason = item.get("사유", "").lower()
    if item.get("결과") != "미충족":
        return
    early_stage = re.search(r"(?:시리즈|series)\s*[abc]\b|pre[- ]?[abc]\b|seed|시드|프리[- ]?[abc]", reason)
    if early_stage and not re.search(r"(?:시리즈|series)\s*[de]\b|pre[- ]?[de]\b", reason):
        item["결과"] = "충족"
        item["사유"] = item["사유"] + " (기준 정정: Series C 또는 이전 단계는 충족)"


def _validate_criterion(code: str, item: CriterionResult, source_map: dict[str, dict], company_id: str,
                        evidence: list[dict]) -> dict:
    allowed_urls = set(source_map)
    cited = [url for url in dict.fromkeys(item.근거URL) if url in allowed_urls]
    official_hosts = ("krx.co.kr", "kind.krx.co.kr", "dart.fss.or.kr", "opendart.fss.or.kr")
    if code == "G1":
        cited = [url for url in cited if any(urlparse(url).netloc.endswith(host) for host in official_hosts)]
        if item.결과 == "충족":
            text = " ".join(source_map[url].get("content", "") for url in cited).lower()
            if not any(term in text for term in ("비상장", "unlisted", "not listed")):
                cited = []
    if item.결과 not in ("충족", "미충족", "확인불가") or not cited:
        return {"결과": "확인불가", "사유": item.사유 or "검색 결과로 판정 근거를 확인하지 못함", "근거ID": []}

    ids = []
    for url in cited:
        source = source_map[url]
        found = next((row for row in evidence if row["url"] == url), None)
        if found is None:
            evidence_id = f"ELG-{company_id}-{len(evidence) + 1:02d}"
            publisher = urlparse(url).netloc
            pub_year = source.get("published_date") or date.today().isoformat()
            excerpt = " ".join((source.get("content") or "").split())[:300]
            found = {
                "근거ID": evidence_id,
                "출처명": source.get("title") or publisher,
                "publisher": publisher,
                "pub_year": pub_year,
                "source_type": "웹페이지",
                "url": url,
                "source_page": None,
                "확인일": date.today().isoformat(),
                "원문발췌": excerpt,
                "chunk_id": None,
            }
            evidence.append(found)
        ids.append(found["근거ID"])
    return {"결과": item.결과, "사유": item.사유, "근거ID": ids}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Reuse cached Tavily searches but rerun LLM assessment")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    # LangSmith tracing is optional; avoid remote trace attempts when its key is absent/invalid.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    tavily_key = os.getenv("TAVILY_API_KEY")
    if not tavily_key:
        raise RuntimeError("TAVILY_API_KEY가 없습니다. .env를 확인하세요.")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY가 없어 검색 근거를 구조화할 수 없습니다.")
    dart_key = os.getenv("DART_API_KEY")
    dart_companies = _load_dart_corp_codes(dart_key) if dart_key else []

    companies = json.loads(COMPANIES_PATH.read_text(encoding="utf-8"))
    g4_payload = json.loads(G4_PATH.read_text(encoding="utf-8"))
    g4_pass = {row["company_id"] for row in g4_payload.get("results", []) if row.get("판정") == "통과"}
    targets = [company for company in companies if company.get("company_id") in g4_pass]
    old_results = _load_old_results()
    llm = get_llm().with_structured_output(ExternalResult, method="json_schema", strict=True)
    results = []
    output_companies = {company["company_id"]: company for company in companies}

    for index, company in enumerate(targets, start=1):
        cid = company["company_id"]
        review_hash = _review_hash(company)
        previous = old_results.get(cid)
        completed_at = previous.get("completed_at") if previous else None
        still_fresh = False
        if completed_at:
            try:
                finished = datetime.fromisoformat(completed_at)
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=timezone.utc)
                still_fresh = finished > datetime.now(timezone.utc) - timedelta(days=14)
            except ValueError:
                pass
        if (
            not args.refresh
            and previous
            and previous.get("review_hash") == review_hash
            and still_fresh
            and not any(item.get("error") for item in previous.get("query_log", []))
        ):
            row = previous
            row.setdefault("current_evidence", [])
            if dart_companies:
                g1 = _dart_g1(company, dart_companies, row["current_evidence"])
                row.setdefault("criteria", {})["G1"] = g1 if g1["결과"] != "확인불가" else ( _domain_g1(company, row["current_evidence"]) or g1 )
                row["method"] = "Tavily 웹 검색 + OpenDART 기업코드 목록"
            _correct_g2_stage(row)
            results.append(row)
            print(f"[{index}/{len(targets)}] {cid} {company['기업명']}: cached")
            continue

        search = _collect_sources(company, tavily_key)
        search_hash = _digest(search)
        if not args.refresh and still_fresh and previous and previous.get("search_hash") == search_hash and previous.get("review_hash") == review_hash:
            row = previous
        elif not search["sources"]:
            evidence = []
            criteria = {
                code: {
                    "결과": "확인불가",
                    "사유": "Tavily에서 판정에 쓸 수 있는 검색 근거를 받지 못함",
                    "근거ID": [],
                }
                for code in ("G1", "G2", "G3")
            }
            if dart_companies:
                criteria["G1"] = _dart_g1(company, dart_companies, evidence)
            row = {
                "company_id": cid,
                "기업명": company.get("기업명"),
                "criteria": criteria,
                "current_evidence": evidence,
                "query_log": search["query_log"],
                "search_hash": search_hash,
                "review_hash": review_hash,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "method": "Tavily 웹 검색 + OpenDART 기업코드 목록" if dart_companies else "Tavily 웹 검색; DART_API_KEY 미설정",
            }
            old_results[cid] = row
            _write_atomic(OUTPUT_PATH, {
                "as_of_date": date.today().isoformat(),
                "scope": "G4 통과 시스템반도체 후보의 G1-G3 외부 검증 (G1 OpenDART, G2-G3 Tavily)",
                "results": [old_results[key] for key in (c["company_id"] for c in targets) if key in old_results],
            })
        else:
            input_payload = {
                "기업": {
                    "company_id": cid,
                    "기업명": company.get("기업명"),
                    "홈페이지": company.get("홈페이지"),
                    "메인아이템": company.get("메인아이템"),
                    "사업요약": company.get("사업Point", "")[:1800],
                    "PDF투자이력": company.get("투자유치이력", []),
                },
                "검색결과": [
                    {**source, "content": source.get("content", "")[:1800]}
                    for source in search["sources"]
                ],
            }
            assessment = llm.invoke([
                ("system", EXTERNAL_REVIEW_PROMPT),
                ("human", json.dumps(input_payload, ensure_ascii=False)),
            ])
            source_map = {source["url"]: source for source in search["sources"]}
            evidence: list[dict] = []
            criteria = {
                code: _validate_criterion(code, getattr(assessment, code), source_map, cid, evidence)
                for code in ("G1", "G2", "G3")
            }
            if dart_companies:
                criteria["G1"] = _dart_g1(company, dart_companies, evidence)
            _correct_g2_stage({"criteria": criteria})
            row = {
                "company_id": cid,
                "기업명": company.get("기업명"),
                "criteria": criteria,
                "current_evidence": evidence,
                "query_log": search["query_log"],
                "search_hash": search_hash,
                "review_hash": review_hash,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "method": "Tavily 웹 검색 + OpenDART 기업코드 목록" if dart_companies else "Tavily 웹 검색; DART_API_KEY 미설정",
            }
            old_results[cid] = row
            _write_atomic(OUTPUT_PATH, {
                "as_of_date": date.today().isoformat(),
                "scope": "G4 통과 시스템반도체 후보의 G1-G3 외부 검증 (G1 OpenDART, G2-G3 Tavily)",
                "results": [old_results[key] for key in (c["company_id"] for c in targets) if key in old_results],
            })
        results.append(row)
        if dart_companies:
            row.setdefault("current_evidence", [])
            g1 = _dart_g1(company, dart_companies, row["current_evidence"])
            row.setdefault("criteria", {})["G1"] = g1 if g1["결과"] != "확인불가" else ( _domain_g1(company, row["current_evidence"]) or g1 )
        _correct_g2_stage(row)
        output_companies[cid] = company
        counts = Counter(
            row.get("criteria", {}).get(code, {}).get("결과", "확인불가")
            for code in ("G1", "G2", "G3")
        )
        print(f"[{index}/{len(targets)}] {cid} {company['기업명']}: {dict(counts)}")

    for row in results:
        company = output_companies[row["company_id"]]
        row["company_hash"] = company_hash(company)
        if dart_companies and row["criteria"]["G1"]["결과"] == "확인불가":
            homepage = company.get("홈페이지") or ""
            domain = urlparse(homepage if "://" in homepage else "https://" + homepage).netloc.removeprefix("www.")
            name = company.get("기업명", "")
            queries = [
                f'"{name}" "{domain}" 상장 비상장 종목코드 DART' if domain else f'"{name}" 상장 비상장 종목코드 DART',
                f'site:{domain} "{name}" 회사 주식 상장 비상장' if domain else f'"{name}" 기업 법인명 사업자등록번호',
            ]
            search_sources = []
            cache = _load_cache()
            for query in queries:
                try:
                    response = _tavily_search(query, tavily_key, cache)
                    for source in response.get("results", [])[:5]:
                        if source.get("url"):
                            search_sources.append(source)
                except RuntimeError:
                    continue
            identities = []
            for source in search_sources:
                content = " ".join((source.get("raw_content") or source.get("content") or "").split())
                combined = (source.get("title") or "") + " " + content
                if domain and domain.lower() in combined.lower():
                    identities.append((source, content))
            if identities:
                source, content = identities[0]
                url = source["url"]
                ev_id = f"ELG-{row['company_id']}-ID-{len(row.get('current_evidence', [])) + 1:02d}"
                row.setdefault("current_evidence", []).append({
                    "근거ID": ev_id,
                    "출처명": source.get("title") or urlparse(url).netloc,
                    "publisher": urlparse(url).netloc,
                    "pub_year": source.get("published_date") or date.today().year,
                    "source_type": "웹페이지",
                    "url": url,
                    "source_page": None,
                    "확인일": date.today().isoformat(),
                    "원문발췌": content[:300],
                    "chunk_id": None,
                })
                # Web evidence can support entity identity, but does not itself prove unlisted status.
                row["criteria"]["G1"] = {
                    "결과": "확인불가",
                    "사유": "공식 도메인과 후보 기업의 동일성은 확인했으나, OpenDART 정확 일치 또는 거래소 상장 상태 근거는 찾지 못함",
                    "근거ID": [ev_id],
                }
        _correct_g2_stage(row)
    result_counts = {
        code: dict(Counter(row["criteria"][code]["결과"] for row in results))
        for code in ("G1", "G2", "G3")
    }
    _write_atomic(OUTPUT_PATH, {
        "as_of_date": date.today().isoformat(),
        "scope": "G4 통과 시스템반도체 후보의 G1-G3 외부 검증 (G1 OpenDART, G2-G3 Tavily)",
        "counts": result_counts,
        "results": results,
    })
    print("G1-G3 counts:", result_counts)
    print(f"저장: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
