# THE FINAL

국내 다판매사 3PL 굿즈 출고 운영 플랫폼의 Python 백엔드입니다. 판매사별 출고 요청 접수부터
입고·QC, Seller+SKU 엄격 FIFO 배정, 피킹 준비 판정까지의 정합성을 **실제 PostgreSQL 테스트로
증명하는 것**을 1차 목표로 합니다.

업무 규칙·요구사항·아키텍처 결정의 원본은 Notion에 있고 이 저장소에 복제하지 않습니다.

## 현재 상태

**24-0 앱 골격 진행 중.** FastAPI health, Docker PostgreSQL, Alembic, pytest + Testcontainers,
CI가 동작합니다. 도메인 모델과 테이블은 아직 없고 24-1에서 시작합니다.

> **SIMULATION 데이터만 사용합니다.** 실제 수취인 개인정보와 실제 택배사 API는 쓰지 않습니다.
> 가상/수기 운송장과 가상 인계 기록은 실제 출고 실적이 아닙니다.

## 필요 환경

- Python 3.13 (uv가 자동 설치)
- [uv](https://docs.astral.sh/uv/) 0.12 이상
- Docker — 로컬 DB와 통합 테스트(Testcontainers)에 필요합니다

## 실행

```bash
docker compose up -d          # PostgreSQL 18.6
uv sync                       # 의존성 설치
uv run alembic upgrade head   # 스키마를 head로
uv run uvicorn fulfill.main:app --reload
```

`GET /health`는 프로세스 생존만 확인하며 DB에 접속하지 않습니다. DB 사용 가능 여부는 별개의
사실이므로 통합 테스트에서 따로 검증합니다.

## 검증

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest                 # 통합 테스트 포함, Docker 필요
uv run pytest -m "not integration"   # Docker 없이 단위 테스트만
```

데이터베이스 URL은 `DATABASE_URL` 환경변수로 덮어쓸 수 있습니다. 설정하지 않으면
`docker-compose.yml`과 같은 개발용 기본값을 사용합니다.

## 브랜치 전략

`main` 하나를 유지하고 작업은 단기 feature 브랜치에서 수행합니다. PR에서 CI(lint·타입·
테스트)를 통과해야 squash 머지되므로 `main`은 항상 검사를 통과한 상태입니다. 배포와 버전
릴리스가 없어 `develop` 브랜치는 두지 않았습니다.
