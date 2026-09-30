# 초기 환경 세팅

패키지 관리는 **uv** 하나로 통일한다. `pip install`·`uv init`은 사용하지 않는다.

## 1. 처음 한 번

```bash
# uv 설치 (없으면)
curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS / Linux
# Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

git clone https://github.com/piw0814-create/skala-rag-personal.git
cd skala-rag-personal
uv sync                  # Python 3.11 자동 설치 + .venv 생성 + uv.lock 그대로 설치
cp .env.example .env     # API 키 입력 (OPENAI, TAVILY, DART)
uv run pytest -q         # 전부 통과하면 세팅 완료
```

- Python 버전은 `pyproject.toml`의 `requires-python = "==3.11.*"`로 고정되어 있다. 로컬에 3.11이 없어도 uv가 내려받는다.
- 임베딩 비교 실험(P6)을 맡은 사람만 추가로 `uv sync --group eval`.
- VS Code / Jupyter 인터프리터는 `skala-rag-personal/.venv/bin/python` 선택.
- 보고서 PDF는 Chrome headless로 만든다. macOS는 Google Chrome, Linux는 `google-chrome` 또는 `chromium` 실행 파일이 필요하다. 없으면 Markdown은 저장되고 PDF 실패 경고가 나온다. `--pdf` 실행은 PDF 실패를 오류로 반환한다.

## 2. 실행

한 명령으로 사전 조사부터 전체 평가까지 실행하려면 `uv run python app.py --prepare --pdf`를 사용한다. 중단 재개·보고서 재생성·결과 점검은 [FULL_PIPELINE](FULL_PIPELINE.md)을 참고한다. 아래는 각 단계를 따로 실행하는 방법이다.

```bash
uv run python -m tools.g4_screening          # 45개사 AI 관련성 검증
uv run python -m tools.run_tavily_eligibility # G4 통과 기업 DART·Tavily 검증
uv run python app.py                     # 전체 파이프라인 → outputs/report.md
uv run python app.py --as-of 2026-09-30 --pdf # 기준일 + 5쪽 이내 PDF
uv run python app.py --graph             # 그래프 mermaid 출력
```

가상환경 활성화(`source .venv/bin/activate`) 없이 `uv run`을 앞에 붙이면 된다.

외부 검증은 graph 실행 전 별도 단계다. 결과 JSON은 `data/processed/`에 저장하며 graph는 같은 기업 입력 해시와 G1~G3의 14일 유효기간을 확인한다. DART에서 정확한 법인을 찾지 못한 경우 비상장으로 추정하지 않고 확인불가로 남긴다. Tavily 검색은 캐시를 재사용하며 강제 갱신은 `tools.run_tavily_eligibility --refresh`로 실행한다. 기준일은 근거 날짜 검사에 사용되며 과거 시점 웹검색을 재현하지 않는다.

전체 실행 중 노드명이 출력된다. `outputs/evaluation_results.json`에는 45개사 판정·점수·근거·오류가 저장된다. 보고서 노드는 PDF를 한 번 생성하고 저장 상태를 반환하며, `--pdf`는 PDF 성공과 실제 5쪽 제한을 확인한다. 초과하면 내용 삭제 없이 오류를 반환한다. 각 LLM 요청은 60초 timeout과 최대 2회 전송 재시도를 사용한다. D의 구조화 응답 검증 실패는 한 번만 다시 시도하고, 끝내 실패하면 해당 모델 분석을 폐기해 확인불가로 기록한다. 에이전트 내부 반복은 호출당 12단계로 제한되며 전체 45개사 graph 제한과 별개다. 투자 판단은 같은 입력을 3회 병렬 채점해 중앙값을 사용한다.

## 3. 두 파일의 역할

| 파일 | 역할 | 누가 수정 |
|---|---|---|
| `pyproject.toml` | 직접 쓰는 패키지와 허용 범위, Python 버전 (의도) | 사람 (`uv add`가 대신 써줌) |
| `uv.lock` | 하위 의존성까지 정확한 버전·해시 고정 (결과) | uv만. 손으로 수정 금지 |

둘 다 커밋한다. 없으면 팀원·평가자마다 다른 버전이 설치되어 "내 컴퓨터에선 되는데"가 생긴다.

## 4. 개발 중 규칙

| 상황 | 할 일 |
|---|---|
| 패키지 추가 | `uv add <pkg>` → `pyproject.toml`·`uv.lock` **같은 커밋**에 포함 |
| 패키지 제거 | `uv remove <pkg>` → 동일 |
| pull 후 두 파일이 바뀌었음 | `uv sync` |
| PR에서 `uv.lock` 충돌 | 손으로 고치지 말고 `git checkout --theirs uv.lock && uv lock` 후 커밋 |
| 커밋 전 | `uv run pytest -q` 통과 확인 |

## 5. 트러블슈팅

실행 중 오류는 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)를 먼저 확인한다.

## 6. 커밋하지 않는 것

`.env`(API 키), `.venv/`, `.cache/`, `outputs/`의 실행 결과, `data/processed/`의 외부 조사 결과, 개발·테스트 캐시는 `.gitignore`에 등록되어 있다. `.env.example`, `.python-version`, 원천 PDF·manifest·companies.json, 테스트 fixture·eval 기준 데이터·문서는 추적 대상이다. 외부 조사 JSON은 위 사전 검증 명령으로 재생성한다.
