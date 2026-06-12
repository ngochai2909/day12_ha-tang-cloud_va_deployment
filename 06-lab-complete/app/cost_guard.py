from datetime import datetime, timezone

from fastapi import HTTPException

from app.config import settings
from app.redis_client import get_redis

PRICE_PER_1K_INPUT_TOKENS = 0.00015
PRICE_PER_1K_OUTPUT_TOKENS = 0.0006


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    input_cost = (input_tokens / 1000) * PRICE_PER_1K_INPUT_TOKENS
    output_cost = (output_tokens / 1000) * PRICE_PER_1K_OUTPUT_TOKENS
    return round(input_cost + output_cost, 6)


def _monthly_key(user_id: str) -> str:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return f"fintrack:budget:{user_id}:{month}"


def check_budget(user_id: str, estimated_cost: float) -> None:
    key = _monthly_key(user_id)
    r = get_redis()
    current = float(r.get(key) or 0.0)
    if current + estimated_cost > settings.monthly_budget_usd:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "Monthly budget exceeded",
                "used_usd": round(current, 6),
                "budget_usd": settings.monthly_budget_usd,
            },
        )


def record_cost(user_id: str, amount_usd: float) -> float:
    key = _monthly_key(user_id)
    r = get_redis()
    new_value = r.incrbyfloat(key, amount_usd)
    r.expire(key, 32 * 24 * 3600)
    return round(float(new_value), 6)

