"""Dependencias comunes de la capa web."""

import secrets

from fastapi import Header, HTTPException, Request

from sanibot.services.context import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def require_admin(request: Request, x_admin_key: str | None = Header(None)) -> str:
    expected = request.app.state.ctx.settings.admin_api_key
    if not x_admin_key or not secrets.compare_digest(x_admin_key, expected):
        raise HTTPException(401, "Clave de administración inválida")
    return "admin"
