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
        # Cada arranque del servidor invalida las sesiones anteriores: al reiniciar, nadie queda con sesión abierta.
        self.arranque = secrets.token_hex(8)

    def load(self, request: Request) -> dict:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return {}
        try:
            data = self._s.loads(raw, max_age=MAX_AGE)
        except BadSignature:
            return {}
        if not isinstance(data, dict) or data.get("_arranque") != self.arranque:
            return {}
        return data

    def save(self, response: Response, data: dict) -> None:
        valor = self._s.dumps({**data, "_arranque": self.arranque})
        response.set_cookie(COOKIE, valor, max_age=MAX_AGE, httponly=True, samesite="lax", secure=self.secure)

    def clear(self, response: Response) -> None:
        response.delete_cookie(COOKIE)


def ensure_csrf(session: dict) -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf(session: dict, token: str | None) -> bool:
    expected = session.get("csrf")
    return bool(expected and token and secrets.compare_digest(expected, token))
