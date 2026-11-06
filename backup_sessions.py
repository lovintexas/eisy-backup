#!/usr/bin/env python3

import secrets
import threading
import time

TIMEOUT = 3600

_sessions = {}
_lock = threading.RLock()


def create_session():
    token = secrets.token_urlsafe(32)
    now = time.monotonic()

    with _lock:
        _sessions[token] = now + TIMEOUT

    return token


def validate_session(token, refresh=False):
    if not token:
        return False

    now = time.monotonic()

    with _lock:
        expires = _sessions.get(token)

        if expires is None:
            return False

        if now >= expires:
            del _sessions[token]
            return False

        if refresh:
            _sessions[token] = now + TIMEOUT

        return True


def revoke_session(token):
    with _lock:
        _sessions.pop(token, None)


def revoke_all_sessions():
    with _lock:
        _sessions.clear()
