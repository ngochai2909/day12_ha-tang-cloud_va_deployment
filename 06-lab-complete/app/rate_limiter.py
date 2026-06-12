import time

from fastapi import HTTPException

from app.config import settings
from app.redis_client import get_redis


def check_rate_limit(user_id: str) -> dict:
    now = time.time()
    key = f"fintrack:rl:{user_id}"
    window_start = now - 60
    r = get_redis()

    # Sliding window with sorted set timestamps.
    with r.pipeline() as pipe:
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {f"{now:.6f}": now})
        pipe.expire(key, 120)
        _, current_count, _, _ = pipe.execute()

    limit = settings.rate_limit_per_minute
    if current_count >= limit:
        retry_after = 60
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit} req/min",
            headers={
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "Retry-After": str(retry_after),
            },
        )

    remaining = max(0, limit - (current_count + 1))
    return {"limit": limit, "remaining": remaining}

