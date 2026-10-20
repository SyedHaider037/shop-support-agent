"""FastAPI app: GET /health and POST /chat.

create_app() takes its dependencies as arguments so each test builds its own
app with its own settings, fake client and store, and nothing leaks between tests.
"""

from dataclasses import asdict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from groq import Groq

from app.agent.loop import API_ERRORS, run_agent
from app.config import ConfigError, Settings, create_groq_client, load_settings
from app.schemas import ChatRequest, ChatResponse
from app.store.store import Store

UNAVAILABLE = "The assistant is temporarily unavailable. Please try again later."


def create_app(
    settings: Settings | None = None, client: Groq | None = None, store: Store | None = None
) -> FastAPI:
    if settings is None:
        load_dotenv()
        settings = load_settings()

    app = FastAPI(title="shop-support-agent")
    # One store per process. On serverless it is ephemeral: leads vanish on cold start.
    shop = store if store is not None else Store.fresh()
    groq_client = client

    def get_client() -> Groq:
        # Created on first use so a missing key breaks /chat only, never startup or /health.
        nonlocal groq_client
        if groq_client is None:
            groq_client = create_groq_client(settings)
        return groq_client

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/chat")
    def chat(request: ChatRequest) -> ChatResponse:
        try:
            model_client = get_client()
        except ConfigError as exc:
            raise HTTPException(
                status_code=503, detail="The assistant is not configured: GROQ_API_KEY is missing."
            ) from exc

        messages = [m.model_dump() for m in request.messages]
        result = run_agent(messages, shop, model_client, settings)
        if result.error in API_ERRORS:
            raise HTTPException(status_code=503, detail=UNAVAILABLE)

        return ChatResponse(
            reply=result.reply,
            steps=[asdict(step) for step in result.steps],
            usage=asdict(result.usage),
            guardrail_events=[asdict(event) for event in result.guardrail_events],
        )

    return app


app = create_app()
