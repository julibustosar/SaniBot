"""Sesión web firmada en una cookie y protección CSRF para los formularios."""

import secrets

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

COOKIE = "sanibot_session"
MAX_AGE = 60 * 60 * 8


class SessionManager:
    def __init__(self, secret_key: str, secure: bool):
        self._s = URLSafeTimedSerializer(secret_key, salt="sanibot-session")
        self.secure = secure

    def load(self, request: Request) -> dict:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return {}
        try:
            data = self._s.loads(raw, max_age=MAX_AGE)
        except BadSignature:
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, response: Response, data: dict) -> None:
        response.set_cookie(COOKIE, self._s.dumps(data), max_age=MAX_AGE, httponly=True, samesite="lax",
                            secure=self.secure)

    def clear(self, response: Response) -> None:
        response.delete_cookie(COOKIE)


def ensure_csrf(session: dict) -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf(session: dict, token: str | None) -> bool:
    expected = session.get("csrf")
    return bool(expected and token and secrets.compare_digest(expected, token))
