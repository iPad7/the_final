"""FastAPI composition root."""

from fastapi import FastAPI

app = FastAPI(title="THE FINAL fulfillment backend")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only: the process is up.

    This deliberately does not touch the database. Reachable process and usable
    database are separate facts, and the integration tests check the second one.
    """
    return {"status": "ok"}
