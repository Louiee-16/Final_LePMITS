# accounts/ratelimit.py
"""Lightweight failed-attempt throttle for login/OTP, backed by Django's
cache framework (see CACHES in config/settings.py — currently the default
per-process LocMemCache, same limitation documents/rag/generator.py's own
cache use already has; fine for a single-process dev/internal deployment,
revisit alongside that if this ever moves behind multiple workers).

Deliberately generous (5 attempts / 15 minutes) rather than a library's
aggressive default — this is an internal tool with a small, known user base,
and locking a real secretariat/councilor user out mid-workday over a few
mistyped attempts is worse than the marginal brute-force risk at this scale.
Only failed attempts count against the limit; a successful login/OTP clears
its own counter.
"""
from django.core.cache import cache

from audit.utils import get_client_ip

WINDOW_SECONDS = 15 * 60
MAX_ATTEMPTS = 5


def _key(prefix, identifier):
    return f"ratelimit:{prefix}:{identifier}"


def is_locked_out(prefix, identifier):
    return cache.get(_key(prefix, identifier), 0) >= MAX_ATTEMPTS


def record_failure(prefix, identifier):
    key = _key(prefix, identifier)
    attempts = cache.get(key, 0) + 1
    cache.set(key, attempts, WINDOW_SECONDS)


def clear(prefix, identifier):
    cache.delete(_key(prefix, identifier))
