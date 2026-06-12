"""
Production FinTrack Agent — Lab 6 Day 12 final project.
"""
import json
import signal
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import logging
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.config import settings
from app.cost_guard import check_budget, estimate_cost, record_cost
from app.fintrack_models import DashboardSummary, ParsedTransaction, TransactionRecord, Wallet
from app.fintrack_parser import parse_financial_text
from app.fintrack_store import FinTrackStore
from app.rate_limiter import check_rate_limit
from app.redis_client import get_redis

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

START_TIME = time.time()
_is_ready = False
_request_count = 0
_error_count = 0
_store: FinTrackStore | None = None


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    user_id: str = Field(default="demo-user", min_length=1, max_length=64)


class AskResponse(BaseModel):
    user_id: str
    parsed: ParsedTransaction
    transaction: TransactionRecord
    wallet: Wallet
    dashboard: DashboardSummary
    timestamp: str


def _store_or_raise() -> FinTrackStore:
    if _store is None:
        raise HTTPException(503, "Storage is not ready")
    return _store


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _is_ready, _store
    base_dir = Path(__file__).resolve().parents[1]
    data_dir = Path(settings.fintrack_data_dir)
    seed_dir = Path(settings.fintrack_seed_dir)
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir
    if not seed_dir.is_absolute():
        seed_dir = base_dir / seed_dir
    _store = FinTrackStore(str(data_dir), seed_dir=str(seed_dir))

    logger.info(
        json.dumps(
            {
                "event": "startup",
                "app": settings.app_name,
                "version": settings.app_version,
                "environment": settings.environment,
                "data_dir": str(data_dir),
                "seed_dir": str(seed_dir),
            }
        )
    )
    time.sleep(0.1)
    _is_ready = True
    logger.info(json.dumps({"event": "ready"}))
    yield
    _is_ready = False
    logger.info(json.dumps({"event": "shutdown"}))


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    global _request_count, _error_count
    start = time.time()
    _request_count += 1
    try:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if "server" in response.headers:
            del response.headers["server"]
        duration = round((time.time() - start) * 1000, 1)
        logger.info(
            json.dumps(
                {
                    "event": "request",
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "ms": duration,
                }
            )
        )
        return response
    except Exception:
        _error_count += 1
        raise


@app.get("/", tags=["Info"])
def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "concept": "Natural language to safe finance operations",
        "endpoints": {
            "ask": "POST /ask (requires X-API-Key)",
            "transactions": "GET /transactions",
            "wallets": "GET /wallets",
            "dashboard": "GET /dashboard",
            "history": "GET /history",
            "health": "GET /health",
            "ready": "GET /ready",
        },
    }


@app.post("/ask", response_model=AskResponse, tags=["FinTrack"])
async def ask_agent(
    body: AskRequest,
    request: Request,
    auth_user_id: str = Depends(verify_api_key),
):
    user_id = body.user_id or auth_user_id
    check_rate_limit(user_id)
    input_tokens = len(body.question.split()) * 2
    check_budget(user_id, estimate_cost(input_tokens=input_tokens, output_tokens=0))

    logger.info(
        json.dumps(
            {
                "event": "nl_request",
                "user_id": user_id,
                "text": body.question,
                "client": str(request.client.host) if request.client else "unknown",
            }
        )
    )
    store = _store_or_raise()
    history = store.get_history(user_id, limit=10)
    if history:
        logger.info(json.dumps({"event": "history_loaded", "user_id": user_id, "items": len(history)}))

    parsed = parse_financial_text(
        body.question,
        default_currency=settings.default_currency,
        openai_api_key=settings.openai_api_key,
        llm_model=settings.llm_model,
    )
    if parsed.meta.needs_clarification:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Can not safely create transaction. Need clarification.",
                "notes": parsed.meta.notes,
            },
        )

    try:
        transaction, wallet = store.create_transaction(user_id, parsed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    dashboard = store.build_dashboard(user_id)
    output_tokens = len(json.dumps(transaction.model_dump()).split()) * 2
    check_budget(user_id, estimate_cost(input_tokens=0, output_tokens=output_tokens))
    total_cost = record_cost(user_id, estimate_cost(input_tokens, output_tokens))
    logger.info(json.dumps({"event": "cost_recorded", "user_id": user_id, "monthly_cost_usd": total_cost}))
    return AskResponse(
        user_id=user_id,
        parsed=parsed,
        transaction=transaction,
        wallet=wallet,
        dashboard=dashboard,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/transactions", response_model=list[TransactionRecord], tags=["FinTrack"])
def list_transactions(
    user_id: str = "demo-user",
    limit: int = 20,
    _auth_user_id: str = Depends(verify_api_key),
):
    check_rate_limit(user_id)
    store = _store_or_raise()
    return store.get_transactions(user_id=user_id, limit=max(1, min(limit, 200)))


@app.get("/wallets", response_model=list[Wallet], tags=["FinTrack"])
def list_wallets(user_id: str = "demo-user", _auth_user_id: str = Depends(verify_api_key)):
    check_rate_limit(user_id)
    store = _store_or_raise()
    return store.get_wallets(user_id=user_id)


@app.get("/dashboard", response_model=DashboardSummary, tags=["FinTrack"])
def dashboard(user_id: str = "demo-user", _auth_user_id: str = Depends(verify_api_key)):
    check_rate_limit(user_id)
    store = _store_or_raise()
    return store.build_dashboard(user_id=user_id)


@app.get("/history", tags=["FinTrack"])
def history(user_id: str = "demo-user", _auth_user_id: str = Depends(verify_api_key)):
    store = _store_or_raise()
    return {"user_id": user_id, "items": store.get_history(user_id, limit=20)}


@app.get("/health", tags=["Operations"])
def health():
    redis_status = "ok"
    try:
        get_redis().ping()
    except Exception:
        redis_status = "error"
    return {
        "status": "ok",
        "version": settings.app_version,
        "environment": settings.environment,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "checks": {
            "storage": "redis",
            "redis": redis_status,
            "fintrack": "active",
            "llm_parser": "openai" if settings.openai_api_key else "deterministic",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/ready", tags=["Operations"])
def ready():
    if not _is_ready:
        raise HTTPException(503, "Not ready")
    try:
        get_redis().ping()
    except Exception as exc:
        raise HTTPException(503, f"Not ready: redis unavailable ({exc})") from exc
    return {"ready": True}


@app.get("/metrics", tags=["Operations"])
def metrics(_auth_user_id: str = Depends(verify_api_key)):
    store = _store_or_raise()
    sample = store.build_dashboard("demo-user")
    r = get_redis()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    monthly_cost = float(r.get(f"fintrack:budget:demo-user:{month}") or 0.0)
    return {
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "error_count": _error_count,
        "monthly_cost_usd_demo_user": round(monthly_cost, 4),
        "monthly_budget_usd": settings.monthly_budget_usd,
        "budget_used_pct_demo_user": round(monthly_cost / settings.monthly_budget_usd * 100, 1)
        if settings.monthly_budget_usd
        else 0.0,
        "demo_user_transactions": sample.transaction_count,
    }


def _handle_signal(signum, _frame):
    logger.info(json.dumps({"event": "signal", "signum": signum}))


signal.signal(signal.SIGTERM, _handle_signal)


if __name__ == "__main__":
    logger.info(f"Starting {settings.app_name} on {settings.host}:{settings.port}")
    logger.info(f"API Key: {settings.agent_api_key[:4]}****")
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        timeout_graceful_shutdown=30,
    )
