"""Webhooks firmados de Sanitas (eventos de agenda) y de WhatsApp (respuestas y estados de entrega)."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from sanibot.integrations.whatsapp import ERRORES_CONOCIDOS, parse_webhook
from sanibot.models import Notificacion
from sanibot.security.signatures import verify
from sanibot.services import accounts, matching
from sanibot.services.context import AppContext

from .deps import get_ctx

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = logging.getLogger("sanibot.webhooks")


@router.post("/sanitas")
async def sanitas_webhook(request: Request, ctx: AppContext = Depends(get_ctx)):
    body = await request.body()
    if not verify(body, ctx.settings.sanitas_webhook_secret, request.headers.get("X-Sanitas-Signature")):
        raise HTTPException(401, "Firma inválida")
    try:
        payload = json.loads(body)
        payload["id"], payload["type"]
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, "Evento mal formado") from exc
    nuevo = matching.register_event(ctx, payload)
    if nuevo:
        # El evento ya quedó guardado; si el procesamiento falla, el worker lo reintenta.
        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(matching.process_event, ctx, payload["id"])
    return {"recibido": True, "duplicado": not nuevo}


@router.get("/whatsapp", response_class=PlainTextResponse)
def whatsapp_verify(request: Request, ctx: AppContext = Depends(get_ctx)):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == ctx.settings.whatsapp_verify_token:
        return q.get("hub.challenge", "")
    raise HTTPException(403, "Token de verificación inválido")


def handle_whatsapp_payload(ctx: AppContext, payload: dict) -> list[str]:
    messages, statuses = parse_webhook(payload)
    results = []
    for m in messages:
        if m["kind"] == "button" and ":" in m["value"]:
            action, oferta_id = m["value"].split(":", 1)
            if action in ("ACEPTAR", "RECHAZAR"):
                results.append(matching.respond_offer(ctx, m["from"], oferta_id, accept=action == "ACEPTAR"))
        elif m["kind"] == "text" and accounts.es_mensaje_de_baja(m["value"]):
            results.append(accounts.opt_out(ctx, m["from"]) or "IGNORADO")
        elif m["kind"] == "text":
            resultado = accounts.confirm_whatsapp_message(ctx, m["from"], m["value"])
            if resultado:
                results.append(resultado)
    if statuses:
        with ctx.db.session() as s:
            for st in statuses:
                n = s.scalar(select(Notificacion).where(Notificacion.wa_message_id == st["id"]))
                if n and st["status"]:
                    n.estado_entrega = st["status"].upper()
                    if st.get("error_code"):
                        n.detalle_error = ERRORES_CONOCIDOS.get(st["error_code"], f"Código {st['error_code']}")[:300]
    return results


@router.post("/whatsapp")
async def whatsapp_webhook(request: Request, ctx: AppContext = Depends(get_ctx)):
    body = await request.body()
    if ctx.settings.whatsapp_mode == "cloud" and not verify(body, ctx.settings.whatsapp_app_secret,
                                                             request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "Firma inválida")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "JSON inválido") from exc
    from starlette.concurrency import run_in_threadpool

    results = await run_in_threadpool(handle_whatsapp_payload, ctx, payload)
    return {"ok": True, "resultados": results}
