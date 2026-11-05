"""Aplicación web de SaniBot (vinculación, WhatsApp, solicitudes, historial) y simulador de WhatsApp en modo consola."""

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from sanibot import timeutil
from sanibot.integrations.sanitas import SanitasError
from sanibot.integrations.whatsapp import ConsoleWhatsApp, WhatsAppError
from sanibot.models import (
    EstadoVinculo,
    ModoReasignacion,
    OfertaCupo,
    Paciente,
    SolicitudEspera,
    TipoSolicitud,
)
from sanibot.security.sessions import check_csrf, ensure_csrf
from sanibot.services import accounts, requests_service
from sanibot.services.context import ESPECIALIDADES, AppContext

from .deps import get_ctx
from .webhooks import handle_whatsapp_payload

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
templates.env.filters["fecha"] = lambda d: timeutil.fecha_larga(d) if d else ""
templates.env.filters["hora"] = lambda d: timeutil.hora(d) if d else ""

ESTADOS_TEXTO = {"ACTIVA": "Buscando", "CON_OFERTA": "Oferta enviada", "SUSPENDIDA": "Suspendida",
                 "CUMPLIDA": "Cita reprogramada", "VENCIDA": "Vencida", "CANCELADA": "Cancelada",
                 "PENDIENTE": "Esperando tu respuesta", "ACEPTADA": "Aceptada", "RECHAZADA": "Rechazada",
                 "EXPIRADA": "Venció sin respuesta", "REPROGRAMADA": "Cita reprogramada", "FALLIDA": "No se pudo completar"}
templates.env.globals["estado_texto"] = lambda e: ESTADOS_TEXTO.get(str(e), str(e))


def _session(request: Request) -> dict:
    if not hasattr(request.state, "sess"):
        request.state.sess = request.app.state.sessions.load(request)
    return request.state.sess


def _render(request: Request, name: str, status: int = 200, **ctx_vars) -> Response:
    sess = _session(request)
    csrf = ensure_csrf(sess)
    ctx: AppContext = request.app.state.ctx
    resp = templates.TemplateResponse(request, name, {"csrf": csrf, "sede": ctx.settings.sede,
                                                     "dev_whatsapp": ctx.settings.whatsapp_mode == "console",
                                                     "demo": ctx.settings.demo_active,
                                                     "logged": bool(sess.get("paciente_id")), **ctx_vars},
                                      status_code=status)
    request.app.state.sessions.save(resp, sess)
    return resp


def _redirect(request: Request, url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    request.app.state.sessions.save(resp, _session(request))
    return resp


def _csrf(request: Request, token: str) -> None:
    if not check_csrf(_session(request), token):
        raise HTTPException(400, "Formulario vencido. Recargue la página e intente de nuevo.")


def _paciente_id(request: Request) -> str:
    pid = _session(request).get("paciente_id")
    if not pid:
        raise HTTPException(303, headers={"Location": "/"})
    return pid


# ------------------------------------------------------------------ inicio y vinculación
@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    if _session(request).get("paciente_id"):
        return _redirect(request, "/inicio")
    return _render(request, "landing.html")


@router.get("/consentimiento", response_class=HTMLResponse)
def consent_form(request: Request):
    return _render(request, "consent.html", finalidad=accounts.FINALIDAD)


@router.post("/consentimiento")
def consent_submit(request: Request, csrf: str = Form(...), acepto: str = Form(""),
                   ctx: AppContext = Depends(get_ctx)):
    _csrf(request, csrf)
    if acepto != "si":
        return _render(request, "consent.html", 400, finalidad=accounts.FINALIDAD,
                       error="Debe aceptar la autorización para continuar.")
    sess = _session(request)
    sess["consent_at"] = timeutil.now().isoformat()
    url = accounts.start_link(ctx, sess)
    return _redirect(request, url)


@router.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "", error: str = "",
                  ctx: AppContext = Depends(get_ctx)):
    if error or not code:
        return _render(request, "message.html", 400, titulo="No se vinculó la cuenta",
                       texto="Rechazaste los permisos en Sanitas o la vinculación se canceló. Puedes intentarlo de nuevo.",
                       enlace=("/consentimiento", "Intentar de nuevo"))
    sess = _session(request)
    try:
        pid = accounts.complete_link(ctx, sess, code, state)
    except (accounts.AccountError, SanitasError) as exc:
        return _render(request, "message.html", 400, titulo="No se vinculó la cuenta", texto=str(exc),
                       enlace=("/consentimiento", "Intentar de nuevo"))
    sess["paciente_id"] = pid
    with ctx.db.session() as s:
        verificado = s.get(Paciente, pid).puede_recibir_whatsapp
    return _redirect(request, "/inicio" if verificado else "/whatsapp")


# ------------------------------------------------------------------ WhatsApp
@router.get("/whatsapp", response_class=HTMLResponse)
def whatsapp_form(request: Request):
    _paciente_id(request)
    return _render(request, "whatsapp.html", paso_wa="numero")


@router.post("/whatsapp")
def whatsapp_submit(request: Request, csrf: str = Form(...), telefono: str = Form(...), acepto: str = Form(""),
                    ctx: AppContext = Depends(get_ctx)):
    _csrf(request, csrf)
    pid = _paciente_id(request)
    try:
        phone = accounts.start_whatsapp_verification(ctx, pid, telefono, acepto == "si")
    except (accounts.AccountError, ValueError) as exc:
        return _render(request, "whatsapp.html", 400, paso_wa="numero", error=str(exc))
    except WhatsAppError as exc:
        return _render(request, "whatsapp.html", 502, paso_wa="numero", error=exc.user_message)
    return _render(request, "whatsapp.html", paso_wa="codigo", telefono_final=phone[-4:],
                   modo_cloud=ctx.settings.whatsapp_mode == "cloud")


@router.post("/whatsapp/verificar")
def whatsapp_verify(request: Request, csrf: str = Form(...), codigo: str = Form(...),
                    ctx: AppContext = Depends(get_ctx)):
    _csrf(request, csrf)
    pid = _paciente_id(request)
    try:
        accounts.confirm_whatsapp(ctx, pid, codigo)
    except accounts.AccountError as exc:
        return _render(request, "whatsapp.html", 400, paso_wa="codigo", error=str(exc))
    return _redirect(request, "/inicio")


# ------------------------------------------------------------------ panel del paciente
@router.get("/inicio", response_class=HTMLResponse)
def home(request: Request, ctx: AppContext = Depends(get_ctx)):
    pid = _paciente_id(request)
    with ctx.db.session() as s:
        p = s.get(Paciente, pid)
        nombre, whatsapp_ok = p.nombre, p.puede_recibir_whatsapp
        vigente = bool(p.cuenta and p.cuenta.estado == EstadoVinculo.VIGENTE)
        solicitudes = list(s.scalars(select(SolicitudEspera).where(SolicitudEspera.paciente_id == pid)
                                     .order_by(SolicitudEspera.creada_en.desc())))
    citas, error = None, None
    if vigente:
        try:
            citas = requests_service.list_appointments(ctx, pid)
        except SanitasError:
            error = "Sanitas no responde en este momento. Tus datos y búsquedas están a salvo; intenta más tarde."
    con_busqueda = {x.cita_actual_id for x in solicitudes if x.estado in requests_service.ACTIVAS}
    return _render(request, "home.html", nombre=nombre, whatsapp_ok=whatsapp_ok, vigente=vigente,
                   citas=citas or [], con_busqueda=con_busqueda, error=error,
                   solicitudes=[x for x in solicitudes if x.estado in requests_service.ACTIVAS])


@router.get("/solicitudes/nueva", response_class=HTMLResponse)
def new_request_form(request: Request, cita_id: str = ""):
    _paciente_id(request)
    return _render(request, "request_form.html", cita_id=cita_id, especialidades=ESPECIALIDADES)


@router.post("/solicitudes/nueva")
def new_request_submit(request: Request, csrf: str = Form(...), cita_id: str = Form(""), especialidad: str = Form(""),
                       manana: str = Form(""), tarde: str = Form(""), fecha_limite: str = Form(""),
                       anticipacion: int = Form(24), modo: str = Form("CON_CONFIRMACION"),
                       ctx: AppContext = Depends(get_ctx)):
    _csrf(request, csrf)
    pid = _paciente_id(request)
    franjas = [f for f, v in (("manana", manana), ("tarde", tarde)) if v]
    limite = None
    if fecha_limite:
        limite = datetime.fromisoformat(fecha_limite).replace(hour=23, minute=59, tzinfo=timeutil.BOGOTA)
    try:
        requests_service.create_request(
            ctx, pid, tipo=TipoSolicitud.ADELANTAR_CITA if cita_id else TipoSolicitud.CITA_NUEVA,
            cita_id=cita_id or None, especialidad=especialidad or None, franjas=franjas, fecha_limite=limite,
            anticipacion_min_horas=anticipacion, modo=ModoReasignacion(modo))
    except (requests_service.RequestError, ValueError, SanitasError) as exc:
        return _render(request, "request_form.html", 400, cita_id=cita_id, especialidades=ESPECIALIDADES,
                       error=str(exc))
    return _redirect(request, "/listo")


@router.get("/listo", response_class=HTMLResponse)
def done(request: Request, ctx: AppContext = Depends(get_ctx)):
    _paciente_id(request)
    return _render(request, "done.html", minutos=ctx.settings.offer_window_minutes)


@router.post("/solicitudes/{solicitud_id}/cancelar")
def cancel_request(request: Request, solicitud_id: str, csrf: str = Form(...), ctx: AppContext = Depends(get_ctx)):
    _csrf(request, csrf)
    requests_service.cancel_request(ctx, _paciente_id(request), solicitud_id)
    return _redirect(request, "/inicio")


@router.get("/historial", response_class=HTMLResponse)
def history(request: Request, ctx: AppContext = Depends(get_ctx)):
    pid = _paciente_id(request)
    with ctx.db.session() as s:
        ofertas = list(s.scalars(select(OfertaCupo).join(SolicitudEspera).where(SolicitudEspera.paciente_id == pid)
                                 .order_by(OfertaCupo.creada_en.desc()).limit(50)))
        filas = [{"creada": o.creada_en, "cupo": o.cupo_inicio, "sede": o.cupo_sede, "estado": o.estado.value,
                  "especialidad": o.solicitud.especialidad} for o in ofertas]
    return _render(request, "history.html", ofertas=filas)


@router.post("/cuenta/desvincular")
def unlink(request: Request, csrf: str = Form(...), ctx: AppContext = Depends(get_ctx)):
    _csrf(request, csrf)
    accounts.unlink(ctx, _paciente_id(request))
    request.state.sess = {}
    return _redirect(request, "/?desvinculada=1")


@router.post("/salir")
def logout(request: Request, csrf: str = Form(...)):
    _csrf(request, csrf)
    request.state.sess = {}
    return _redirect(request, "/")


# ------------------------------------------------------------------ simulador de WhatsApp (solo modo consola)
def _console(ctx: AppContext) -> ConsoleWhatsApp:
    if not isinstance(ctx.whatsapp, ConsoleWhatsApp):
        raise HTTPException(404)
    return ctx.whatsapp


@router.get("/dev/whatsapp", response_class=HTMLResponse)
def dev_whatsapp(request: Request, ctx: AppContext = Depends(get_ctx)):
    wa = _console(ctx)
    msgs = list(reversed(wa.outbox[-40:]))
    return _render(request, "dev_whatsapp.html", mensajes=msgs, ahora=timeutil.now(), delta=timedelta)


@router.post("/dev/whatsapp/responder")
def dev_whatsapp_reply(request: Request, csrf: str = Form(...), telefono: str = Form(...), boton: str = Form(""),
                       texto: str = Form(""), ctx: AppContext = Depends(get_ctx)):
    _csrf(request, csrf)
    _console(ctx)
    msg = ({"from": telefono, "id": "dev", "type": "button", "button": {"payload": boton, "text": boton}} if boton
           else {"from": telefono, "id": "dev", "type": "text", "text": {"body": texto}})
    ctx.whatsapp.record_reply(telefono, boton.split(":")[0].capitalize() if boton else texto)
    handle_whatsapp_payload(ctx, {"entry": [{"changes": [{"value": {"messages": [msg]}}]}]})
    return _redirect(request, "/dev/whatsapp")
