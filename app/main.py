"""FastAPI app: GET /health, POST /chat, the admin approval API and the chat page.

create_app() takes its dependencies as arguments so each test builds its own
app with its own settings, fake client and store, and nothing leaks between tests.
"""

import hmac
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from groq import Groq

from app.agent.loop import API_ERRORS, BUSY_ERROR, BUSY_REPLY, run_agent
from app.config import ConfigError, Settings, create_groq_client, load_settings
from app.ratelimit import RateLimiter, client_ip, retry_after_header
from app.schemas import AdminApproval, ChatRequest, ChatResponse
from app.store.models import PendingApproval
from app.store.store import ApprovalAlreadyDecided, ApprovalNotFound, Store
from app.trace.redact import mask_email, mask_emails
from app.trace.trace import build_trace, log_line

UNAVAILABLE = "The assistant is temporarily unavailable. Please try again later."
RATE_LIMITED = "Too many messages. Please wait a moment and try again."

PUBLIC_DIR = Path(__file__).resolve().parents[1] / "public"
# The page loads only its own script and stylesheet, so everything else can be refused.
# The strict CSP means model text can never run as script, even if a rendering bug let
# HTML through.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def create_app(
    settings: Settings | None = None,
    client: Groq | None = None,
    store: Store | None = None,
    limiter: RateLimiter | None = None,
    log: Callable[[str], None] | None = None,
    public_dir: Path | None = PUBLIC_DIR,
) -> FastAPI:
    if settings is None:
        load_dotenv()
        settings = load_settings()

    app = FastAPI(title="shop-support-agent")
    # One store per process. On serverless it is ephemeral: leads vanish on cold start.
    shop = store if store is not None else Store.fresh()
    groq_client = client
    limits = limiter if limiter is not None else RateLimiter(settings.rate_limit_per_minute)
    # One JSON line per request on stdout, where serverless hosts collect logs.
    write_log = log if log is not None else (lambda line: print(line, flush=True))

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    def get_client() -> Groq:
        # Created on first use so a missing key breaks /chat only, never startup or /health.
        nonlocal groq_client
        if groq_client is None:
            groq_client = create_groq_client(settings)
        return groq_client

    def rate_limit(request: Request) -> None:
        # A dependency, so it runs before body validation: invalid requests count too.
        wait = limits.check(client_ip(request))
        if wait > 0:
            raise HTTPException(
                status_code=429,
                detail=RATE_LIMITED,
                headers={"Retry-After": retry_after_header(wait)},
            )

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        # No token configured: behave as if the admin API does not exist.
        if not settings.admin_token:
            raise HTTPException(status_code=404, detail="Not Found")
        # Constant-time compare so the token cannot be guessed byte by byte from timing.
        given = (x_admin_token or "").encode()
        if not hmac.compare_digest(given, settings.admin_token.encode()):
            raise HTTPException(status_code=401, detail="Invalid admin token.")

    def admin_view(approval: PendingApproval) -> AdminApproval:
        return AdminApproval(
            **approval.model_dump(exclude={"reason"}),
            reason=mask_emails(approval.reason),
            email=mask_email(shop.approval_email(approval.id)),
        )

    def decide(approval_id: str, approve: bool) -> AdminApproval:
        try:
            return admin_view(shop.decide_approval(approval_id, approve))
        except ApprovalNotFound as exc:
            raise HTTPException(status_code=404, detail="No such approval.") from exc
        except ApprovalAlreadyDecided as exc:
            raise HTTPException(
                status_code=409, detail="This approval was already decided."
            ) from exc

    # Not behind the visitor rate limit, and not linked from any public page.
    @app.get("/admin/approvals", dependencies=[Depends(require_admin)])
    def list_approvals() -> list[AdminApproval]:
        return [admin_view(a) for a in shop.list_approvals()]

    @app.post("/admin/approvals/{approval_id}/approve", dependencies=[Depends(require_admin)])
    def approve(approval_id: str) -> AdminApproval:
        return decide(approval_id, approve=True)

    @app.post("/admin/approvals/{approval_id}/reject", dependencies=[Depends(require_admin)])
    def reject(approval_id: str) -> AdminApproval:
        return decide(approval_id, approve=False)

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/chat", dependencies=[Depends(rate_limit)])
    def chat(request: ChatRequest) -> ChatResponse:
        try:
            model_client = get_client()
        except ConfigError as exc:
            raise HTTPException(
                status_code=503, detail="The assistant is not configured: GROQ_API_KEY is missing."
            ) from exc

        messages = [m.model_dump() for m in request.messages]
        started = time.perf_counter()
        result = run_agent(messages, shop, model_client, settings)
        trace = build_trace(
            result,
            settings,
            request_id=uuid.uuid4().hex,
            total_latency_ms=round((time.perf_counter() - started) * 1000),
        )
        write_log(log_line(trace))

        if result.error == BUSY_ERROR:
            # Distinct from an outage: the model is fine, just over its rate limit.
            raise HTTPException(
                status_code=503,
                detail=BUSY_REPLY,
                headers={"Retry-After": retry_after_header(result.retry_after_s or 60)},
            )
        if result.error in API_ERRORS:
            raise HTTPException(status_code=503, detail=UNAVAILABLE)

        return ChatResponse(
            reply=result.reply,
            steps=[asdict(step) for step in result.steps],
            usage=asdict(result.usage),
            guardrail_events=[asdict(event) for event in result.guardrail_events],
            trace=trace,
        )

    # Mounted last, so /health, /chat and /admin keep priority. The rate limit is a /chat
    # dependency, so static files are never limited. No public/ folder: API only.
    if public_dir is not None and public_dir.is_dir():
        app.mount("/", StaticFiles(directory=public_dir, html=True), name="public")

    return app


app = create_app()
