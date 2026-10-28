"""FastAPI 조합 지점."""

from fastapi import FastAPI

from fulfill.modules.requests.api import router as requests_router

app = FastAPI(title="THE FINAL fulfillment backend")
app.include_router(requests_router)


@app.get("/health")
def health() -> dict[str, str]:
    """프로세스 생존만 확인한다.

    일부러 DB에 접속하지 않는다. 프로세스에 도달 가능한 것과 DB를 쓸 수 있는
    것은 서로 다른 사실이며, 후자는 통합 테스트에서 따로 검증한다.
    """
    return {"status": "ok"}
