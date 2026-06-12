from functools import lru_cache

import redis

from app.config import settings


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL is required for stateless mode.")
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)

