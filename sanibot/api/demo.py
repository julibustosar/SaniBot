"""Consola de demostración (/demo): en una sola pantalla, la app del paciente, el chat de WhatsApp y el lado de Sanitas.

Solo existe en entornos locales (Settings.demo_active). No forma parte del producto: sirve para mostrar el flujo
completo en la sustentación sin manejar varias páginas a la vez.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from sanibot import timeutil
from sanibot.integrations.whatsapp import PLANTILLAS, ConsoleWhatsApp
from sanibot.models import Notificacion, Paciente, SolicitudEspera
from sanibot.services import requests_service
from sanibot.services.context import AppContext

from .deps import get_ctx
from .web import _csrf, _render
from .webhooks import handle_whatsapp_payload

router = APIRouter(prefix="/demo", include_in_schema=False)
log = logging.getLogger("sanibot.demo")


def demo_ctx(request: Request) -> AppContext:
    ctx: AppContext = get_ctx(request)
    if not ctx.settings.demo_active:
        raise HTTPException(404)
    return ctx


def _sanitas(ctx: AppContext, method: str, path: str, **kw) -> dict | None:
    try:
        r = httpx.request(method, f"{ctx.settings.sanitas_portal_url}{path}", timeout=5, **kw)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("Sanitas simulada no disponible: %s", exc)
        return None


@router.get("", response_class=HTMLResponse)
def demo_page(request: Request, ctx: AppContext = Depends(demo_ctx)):
    return _render(request, "demo.html", modo=ctx.settings.whatsapp_mode,
                   sanitas_url=ctx.settings.oauth_authorize_url.split("/oauth")[0])


def _chat_console(wa: ConsoleWhatsApp) -> list[dict]:
    salientes = [(m.sent_at, {"id": m.id, "to": m.to, "hora": timeutil.hora(m.sent_at), "plantilla": m.template,
                              "texto": m.preview(), "botones": [{"texto": b[0], "payload": b[1]} for b in m.buttons]})
                 for m in wa.outbox[-40:]]
    entrantes = [(r["at"], {"id": r["id"], "to": r["from"], "hora": timeutil.hora(r["at"]), "texto": r["texto"],
                            "botones": [], "propio": True}) for r in wa.inbox[-40:]]
    return [m for _, m in sorted(salientes + entrantes, key=lambda x: x[0])][-60:]


def _chat_cloud(ctx: AppContext) -> list[dict]:
    with ctx.db.session() as s:
        filas = s.execute(select(Notificacion, Paciente.telefono_whatsapp, Paciente.nombre)
                          .join(Paciente, Paciente.id == Notificacion.paciente_id)
                          .order_by(Notificacion.enviada_en.desc()).limit(40)).all()
    res = []
    for n, tel, nombre in reversed(filas):
        p = PLANTILLAS.get(n.plantilla)
        res.append({"id": n.wa_message_id, "to": tel or "", "nombre": nombre, "hora": timeutil.hora(n.enviada_en),
                    "plantilla": n.plantilla, "estado": n.estado_entrega, "error": n.detalle_error,
                    "texto": p.cuerpo if p else n.plantilla, "botones": []})
    return res


@router.get("/estado")
def demo_state(ctx: AppContext = Depends(demo_ctx)):
    consola = isinstance(ctx.whatsapp, ConsoleWhatsApp)
    with ctx.db.session() as s:
        activas = s.scalars(select(SolicitudEspera).where(SolicitudEspera.estado.in_(requests_service.ACTIVAS))).all()
        n_activas = len(activas)
    return {"modo": "console" if consola else "cloud",
            "chat": _chat_console(ctx.whatsapp) if consola else _chat_cloud(ctx),
            "busquedas_activas": n_activas,
            "sanitas": _sanitas(ctx, "GET", "/portal/resumen")}


@router.post("/whatsapp")
def demo_whatsapp(request: Request, csrf: str = Form(...), telefono: str = Form(...), boton: str = Form(""),
                  texto: str = Form(""), ctx: AppContext = Depends(demo_ctx)):
    _csrf(request, csrf)
    if not isinstance(ctx.whatsapp, ConsoleWhatsApp):
        raise HTTPException(409, "En modo cloud las respuestas se dan desde el celular")
    msg = ({"from": telefono, "id": "demo", "type": "button", "button": {"payload": boton, "text": boton}} if boton
           else {"from": telefono, "id": "demo", "type": "text", "text": {"body": texto}})
    ctx.whatsapp.record_reply(telefono, boton.split(":")[0].capitalize() if boton else texto)
    return {"resultados": handle_whatsapp_payload(ctx, {"entry": [{"changes": [{"value": {"messages": [msg]}}]}]})}


@router.post("/simulador")
def demo_simulator(request: Request, csrf: str = Form(...), accion: str = Form(...), eventos_por_hora: float = Form(120),
                   ctx: AppContext = Depends(demo_ctx)):
    _csrf(request, csrf)
    res = _sanitas(ctx, "POST", "/portal/simulador", data={"accion": accion, "eventos_por_hora": eventos_por_hora})
    if res is None:
        raise HTTPException(502, "La Sanitas simulada no responde")
    return res
