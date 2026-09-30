"""⑤ 경쟁사 비교 — RAG 근거와 Tavily 검색 결과만으로 비교한다."""

import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from langchain_core.documents import Document
from pydantic import BaseModel, Field

from llm import get_llm
from schemas import CompareRow, NamedValue
from state import State
from tools.retrieval import evidence_id, to_evidence

ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "prompts" / "competitor.md"
CACHE_PATH = ROOT / ".cache" / "tavily_search_cache.json"


class CompetitorProduct(BaseModel):
    기업명: str
    제품: str
    핵심지표값: list[NamedValue] = Field(default_factory=list)
    source_url: str
    source_excerpt: str


class CompetitorComparison(BaseModel):
    경쟁제품: list[CompetitorProduct] = Field(default_factory=list)
    비교표: list[CompareRow] = Field(default_factory=list)
    우위: list[str] = Field(default_factory=list)
    열위: list[str] = Field(default_factory=list)
    비교조건: str = "공개 자료 기준이며 측정 조건이 다를 수 있음"
    비교한계: str = ""


def _empty(reason: str) -> dict:
    return {
        "경쟁제품": [],
        "비교표": [],
        "우위": [],
        "열위": [],
        "비교조건": "공개 자료 기준; 비교 가능한 근거가 없으면 수치를 추정하지 않음",
        "비교한계": reason,
        "근거ID": [],
    }


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=CACHE_PATH.parent, delete=False) as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
        f.write("\n")
        temp_path = Path(f.name)
    temp_path.replace(CACHE_PATH)


def _tavily_search(query: str, api_key: str, cache: dict) -> dict:
    cache_key = hashlib.sha256(query.encode("utf-8")).hexdigest()
    cached = cache.get(cache_key)
    if cached:
        cached_at = datetime.fromisoformat(cached["cached_at"])
        if cached_at > datetime.now(timezone.utc) - timedelta(days=14):
            return cached["response"]

    body = json.dumps({
        "query": query,
        "search_depth": "basic",
        "max_results": 5,
        "include_raw_content": True,
        "include_answer": False,
    }).encode("utf-8")
    req = Request(
        "https://api.tavily.com/search",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Tavily 검색 실패: {type(exc).__name__}: {exc}") from exc

    cache[cache_key] = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "response": payload,
    }
    _save_cache(cache)
    return payload


def _queries(company: dict, technology: dict, market: dict) -> list[str]:
    product = company.get("메인아이템") or company.get("기업명") or "반도체 제품"
    core = technology.get("핵심기술") or company.get("혁신성") or ""
    indicators = [item.get("지표명", "") for item in technology.get("성능지표", []) if item.get("지표명")]
    market_terms = " ".join(market.get("목표고객", [])[:2]) if market.get("목표고객") else ""
    return [
        f'"{product}" semiconductor competing products specifications {core[:100]}',
        f'{product} {" ".join(indicators[:3])} datasheet alternatives {market_terms}'.strip(),
    ]


def _search_sources(queries: list[str], api_key: str) -> tuple[list[dict], str]:
    cache = _load_cache()
    sources: dict[str, dict] = {}
    failures = []
    for query in queries:
        try:
            response = _tavily_search(query, api_key, cache)
            for row in response.get("results", []):
                url = row.get("url")
                if url:
                    sources[url] = {
                        "title": row.get("title", ""),
                        "url": url,
                        "content": row.get("raw_content") or row.get("content") or "",
                        "score": row.get("score"),
                        "published_date": row.get("published_date"),
                    }
        except RuntimeError as exc:
            failures.append(str(exc))
    return list(sources.values()), "; ".join(failures)


def _retrieve_context(company: dict, technology: dict) -> tuple[list[dict], str]:
    query = " ".join(filter(None, [
        company.get("메인아이템"),
        technology.get("핵심기술"),
        "경쟁 제품 성능 지표",
    ]))
    try:
        from rag.retriever import hybrid_search

        docs = hybrid_search(
            query,
            doc_type="tech",
            sub_domain=company.get("sub_domain"),
            k=5,
        )
        docs += hybrid_search(query, doc_type="market", doc_id="15", k=5)
        context = []
        for doc in docs:
            metadata = doc.metadata or {}
            context.append({
                "근거ID": evidence_id(doc),
                "doc_type": metadata.get("doc_type"),
                "text": doc.page_content[:1200],
                "title": metadata.get("title", ""),
                "url": metadata.get("url"),
                "chunk_id": metadata.get("chunk_id"),
                "source_page": metadata.get("source_page"),
                "publisher": metadata.get("publisher"),
                "pub_year": metadata.get("pub_year"),
                "source_type": metadata.get("source_type"),
            })
        return context, ""
    except Exception as exc:
        return [], f"기존 RAG 인덱스 재조회 불가: {type(exc).__name__}"


def _source_map(sources: list[dict]) -> dict[str, dict]:
    return {source["url"]: source for source in sources}


def _excerpt_from_source(product: CompetitorProduct, source_map: dict[str, dict]) -> str:
    source = source_map[product.source_url]
    text = " ".join((source.get("content") or "").split())
    claimed = " ".join(product.source_excerpt.split())
    if claimed and claimed in text:
        return claimed[:300]
    return ""


def _value_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value).replace(",", "")).casefold()


def _is_target(product: CompetitorProduct, company: dict) -> bool:
    def normalized(name):
        name = re.sub(r"주식회사|\(주\)|\b(?:inc|incorporated|corp|corporation|ltd|limited)\b", "", name, flags=re.I)
        return re.sub(r"[^a-z0-9가-힣]", "", name.casefold())
    homepage = company.get("홈페이지") or ""
    domain = (urlparse(homepage if "://" in homepage else "https://" + homepage).hostname or "").removeprefix("www.")
    labels = domain.split(".")
    brand = labels[-3] if len(labels) >= 3 and labels[-2:] == ["co", "kr"] else labels[-2] if len(labels) >= 2 else ""
    aliases = {normalized(company.get("기업명") or "")}
    if len(brand) >= 4:
        aliases.add(normalized(brand))
    host = urlparse(product.source_url).hostname or ""
    return normalized(product.기업명) in aliases - {""} or bool(domain and (host == domain or host.endswith("." + domain)))


def _quote_contains(value: str, excerpt: str) -> bool:
    return bool(value and re.search(r"(?<![\d.])" + re.escape(_value_text(value)) + r"(?![\d.])",
                                   _value_text(excerpt)))


def _validated_rows(rows: list[CompareRow], products: list[dict], technology: dict) -> list[dict]:
    """출처 확인된 제품 사양과 실제 D 분석 값만 비교표에 남긴다."""
    metrics = {}
    for product in products:
        for metric in product["핵심지표값"]:
            metrics.setdefault((product["기업명"], _value_text(metric["이름"])), set()).add(_value_text(metric["값"]))
    actual = {(_value_text(m.get("지표명", "")), _value_text(m["값"]))
              for m in technology.get("성능지표", []) if m.get("값") and m["값"] != "확인 불가"}
    result = []
    for row in rows:
        peers = [v.model_dump() for v in row.경쟁사
                 if _value_text(v.값) in metrics.get((v.이름, _value_text(row.지표)), set())]
        if peers:
            result.append({"지표": row.지표, "대상기업": row.대상기업
                           if (_value_text(row.지표), _value_text(row.대상기업)) in actual else "확인 불가",
                           "경쟁사": peers})
    return result


def run(state: State) -> dict:
    """Input: D's analyses and current company. Missing dependencies produce an honest empty result."""
    load_dotenv(ROOT / ".env")
    company = state.get("current_company") or {}
    technology = state.get("technology_analysis") or {}
    market = state.get("market_analysis") or {}
    tavily_key = os.getenv("TAVILY_API_KEY")
    if not tavily_key:
        return {
            "competitor_analysis": _empty("TAVILY_API_KEY가 없어 최신 경쟁 제품과 출처 URL을 확인하지 못함"),
            "current_evidence": [],
        }
    if not technology or not market:
        return {
            "competitor_analysis": _empty("기술·시장 분석 결과가 없어 비교 기준을 만들 수 없음"),
            "current_evidence": [],
        }

    index_context, index_note = _retrieve_context(company, technology)
    sources, search_note = _search_sources(_queries(company, technology, market), tavily_key)
    if not sources:
        notes = [n for n in (index_note, search_note, "웹 검색 결과 없음") if n]
        return {"competitor_analysis": _empty("; ".join(notes)), "current_evidence": []}
    if not os.getenv("OPENAI_API_KEY"):
        return {
            "competitor_analysis": _empty("검색 자료는 확보했지만 구조화 비교를 위한 OPENAI_API_KEY가 없음"),
            "current_evidence": [],
        }

    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    llm = get_llm().with_structured_output(CompetitorComparison, method="json_schema", strict=True)
    input_payload = {
        "기업": {
            "기업명": company.get("기업명"),
            "메인아이템": company.get("메인아이템"),
            "기술분야": company.get("기술분야"),
        },
        "기술분석": technology,
        "시장분석": market,
        "RAG근거": index_context,
        "웹검색결과": sources,
    }
    comparison = llm.invoke([
        ("system", prompt),
        ("human", json.dumps(input_payload, ensure_ascii=False)),
    ])

    source_map = _source_map(sources)
    valid_products = [p for p in comparison.경쟁제품 if p.source_url in source_map
                      and not _is_target(p, company) and _excerpt_from_source(p, source_map)]
    existing_ids = {e["근거ID"] for e in state.get("current_evidence") or []}
    retrieved = [Document(page_content=context["text"], metadata=context) for context in index_context]
    rag_evidence = to_evidence(retrieved, checked=state.get("as_of_date") or date.today().isoformat())
    evidence = [e for e in rag_evidence if e["근거ID"] not in existing_ids]
    products = []
    for product in valid_products:
        source = source_map[product.source_url]
        excerpt = _excerpt_from_source(product, source_map)
        evidence_id = f"CMP-{company.get('company_id', 'UNKNOWN')}-{len(evidence) + 1:02d}"
        domain = urlparse(product.source_url).netloc
        evidence.append({
            "근거ID": evidence_id,
            "출처명": source.get("title") or domain,
            "publisher": domain or None,
            "pub_year": source.get("published_date") or date.today().isoformat(),
            "source_type": "웹페이지",
            "url": product.source_url,
            "source_page": None,
            "확인일": state.get("as_of_date") or date.today().isoformat(),
            "원문발췌": excerpt,
            "chunk_id": None,
        })
        metrics = [value.model_dump() for value in product.핵심지표값
                   if _quote_contains(value.값, excerpt)]
        products.append({
            "기업명": product.기업명,
            "제품": product.제품,
            "핵심지표값": metrics,
            "근거ID": [evidence_id],
        })

    ids = list(dict.fromkeys([e["근거ID"] for e in rag_evidence + evidence]))
    limitations = [x for x in (comparison.비교한계, index_note, search_note) if x]
    if len(products) < 2:
        limitations.append("출처 URL이 확인되는 경쟁 제품이 2개 미만")
    rows = _validated_rows(comparison.비교표, products, technology)
    discarded = (len(products) != len(comparison.경쟁제품)
                 or sum(len(p["핵심지표값"]) for p in products) != sum(len(p.핵심지표값) for p in comparison.경쟁제품)
                 or rows != [row.model_dump() for row in comparison.비교표])
    if discarded:
        limitations.append("출처·인용·수치 검증을 통과하지 못한 비교 내용과 우위·열위 서술을 제외함")
    return {
        "competitor_analysis": {
            "경쟁제품": products,
            "비교표": rows,
            "우위": comparison.우위 if not discarded and products else [],
            "열위": comparison.열위 if not discarded and products else [],
            "비교조건": comparison.비교조건,
            "비교한계": "; ".join(limitations),
            "근거ID": ids,
        },
        "current_evidence": evidence,
    }
