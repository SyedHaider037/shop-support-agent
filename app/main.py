from fastapi import FastAPI

app = FastAPI(title="shop-support-agent")


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
