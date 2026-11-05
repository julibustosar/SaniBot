"""Motor de asignación: eventos de Sanitas, selección por prioridad, ofertas y respuestas (HU09, HU10, HU11, HU12, HU14, HU41).

Prioridad del MVP: orden de inscripción (la solicitud más antigua primero). Las reglas configurables de la HU19
(criterio clínico, pesos) se incorporan en el Sprint 6 sin cambiar este flujo: solo cambia `_ordenar`.
"""

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sanibot import timeutil
from sanibot.integrations.sanitas import Cupo, SanitasAuthError, SanitasError, SlotUnavailable
from sanibot.integrations.whatsapp import WhatsAppError
from sanibot.models import (
    OFERTA_ACTIVA,
    EstadoOferta,
    EstadoSolicitud,
    EstadoVinculo,
    EventoCupo,
    ModoReasignacion,
    OfertaCupo,
    Paciente,
    SolicitudEspera,
    TipoSolicitud,
)
from sanibot.services import accounts, reprogramming
from sanibot.services.audit import audit
from sanibot.services.context import AppContext
from sanibot.services.notifier import send, send_safe

log = logging.getLogger("sanibot.matching")
MAX_EVENT_ATTEMPTS = 5


# ------------------------------------------------------------------ eventos de Sanitas
def register_event(ctx: AppContext, payload: dict) -> bool:
    """Guarda el evento una sola vez (idempotencia, riesgo 3). Devuelve False si ya existía."""
    try:
        with ctx.db.session() as s:
            if s.get(EventoCupo, payload["id"]):
                return False
            s.add(EventoCupo(id=payload["id"], tipo=payload["type"], payload=payload))
        return True
    except IntegrityError:
        return False


def process_event(ctx: AppContext, event_id: str) -> None:
    with ctx.db.session() as s:
        ev = s.get(EventoCupo, event_id)
        if not ev or ev.procesado:
            return
        tipo, payload = ev.tipo, dict(ev.payload)
    try:
        if tipo == "slot.available":
            offer_slot(ctx, payload["slot_id"])
        elif tipo in ("appointment.cancelled", "appointment.updated"):
            sync_appointment(ctx, tipo, payload)
        ok, err = True, None
    except Exception as exc:  # el evento queda pendiente y el worker lo reintenta
        log.exception("Error procesando el evento %s", event_id)
        ok, err = False, f"{exc.__class__.__name__}: {exc}"
    with ctx.db.session() as s:
        ev = s.get(EventoCupo, event_id)
        ev.intentos += 1
        ev.procesado = ok or ev.intentos >= MAX_EVENT_ATTEMPTS
        ev.error = err


def retry_events(ctx: AppContext) -> None:
    with ctx.db.session() as s:
        ids = list(s.scalars(select(EventoCupo.id).where(EventoCupo.procesado.is_(False),
                                                         EventoCupo.intentos < MAX_EVENT_ATTEMPTS)))
    for i in ids:
        process_event(ctx, i)


# ------------------------------------------------------------------ compatibilidad y prioridad
def compatible(sol: SolicitudEspera, cupo: Cupo) -> bool:
    ahora = timeutil.now()
    if cupo.especialidad != sol.especialidad or cupo.sede != sol.sede:
        return False
    if cupo.inicio < ahora + timedelta(hours=sol.anticipacion_min_horas):
        return False
    if sol.fecha_limite and cupo.inicio > sol.fecha_limite:
        return False
    if sol.franjas and timeutil.franja(cupo.inicio) not in sol.franjas:
        return False
    if sol.tipo == TipoSolicitud.ADELANTAR_CITA and not (sol.cita_actual_fecha and cupo.inicio < sol.cita_actual_fecha):
        return False
    return True


def _ordenar(candidatos: list[SolicitudEspera]) -> list[SolicitudEspera]:
    return sorted(candidatos, key=lambda x: (x.creada_en, x.id))


def candidates_for(s: Session, cupo: Cupo) -> list[SolicitudEspera]:
    ya_ofrecidas = select(OfertaCupo.solicitud_id).where(OfertaCupo.cupo_id == cupo.id)
    q = (select(SolicitudEspera).join(Paciente).where(
        SolicitudEspera.estado == EstadoSolicitud.ACTIVA, SolicitudEspera.especialidad == cupo.especialidad,
        SolicitudEspera.sede == cupo.sede, SolicitudEspera.id.not_in(ya_ofrecidas)))
    res = []
    for sol in s.scalars(q):
        p = sol.paciente
        if p.puede_recibir_whatsapp and p.cuenta and p.cuenta.estado == EstadoVinculo.VIGENTE and compatible(sol, cupo):
            res.append(sol)
    return _ordenar(res)


# ------------------------------------------------------------------ ofertas
def offer_slot(ctx: AppContext, slot_id: str) -> str | None:
    """Ofrece un cupo liberado al mejor candidato. Devuelve el id de la oferta creada o None."""
    cupo = ctx.sanitas.get_slot(slot_id)
    if not cupo or cupo.estado != "free":
        return None
    with ctx.db.session() as s:
        if s.scalar(select(OfertaCupo.id).where(OfertaCupo.cupo_id == slot_id, OfertaCupo.estado.in_(OFERTA_ACTIVA))):
            return None
        ids = [c.id for c in candidates_for(s, cupo)]
    for sol_id in ids:
        result, oferta_id = _try_offer(ctx, sol_id, cupo)
        if result == "OFFERED":
            return oferta_id
        if result == "TAKEN":
            return None
    return None


def _try_offer(ctx: AppContext, sol_id: str, cupo: Cupo) -> tuple[str, str | None]:
    with ctx.db.session() as s:
        sol = s.get(SolicitudEspera, sol_id)
        if sol.estado != EstadoSolicitud.ACTIVA:
            return "SKIP", None
        paciente = sol.paciente
        try:
            token = accounts.get_access_token(ctx, s, paciente)
        except SanitasAuthError:
            return "SKIP", None
        # HU41: verificar la cita vigente en Sanitas antes de ofrecer (el paciente pudo cambiarla en el portal).
        if sol.tipo == TipoSolicitud.ADELANTAR_CITA:
            try:
                cita = ctx.sanitas.get_appointment(token, sol.cita_actual_id)
            except SanitasAuthError:
                accounts.invalidate_account(ctx, s, paciente)
                return "SKIP", None
            if not cita or cita.estado != "booked":
                _cerrar_por_cambio_externo(ctx, s, sol)
                return "SKIP", None
            if cita.inicio != sol.cita_actual_fecha:
                sol.cita_actual_fecha = cita.inicio
                if not compatible(sol, cupo):
                    return "SKIP", None
        try:
            hold_id = ctx.sanitas.hold_slot(cupo.id, ctx.settings.offer_window_minutes + 2)
        except SlotUnavailable:
            return "TAKEN", None
        oferta = OfertaCupo(solicitud_id=sol.id, cupo_id=cupo.id, cupo_inicio=cupo.inicio,
                            cupo_profesional=cupo.profesional, cupo_sede=cupo.sede, hold_id=hold_id,
                            expira_en=timeutil.now() + timedelta(minutes=ctx.settings.offer_window_minutes))
        s.add(oferta)
        try:
            s.flush()
        except IntegrityError:  # otro proceso creó una oferta para este cupo o esta solicitud (RNF04)
            s.rollback()
            ctx.sanitas.release_slot(cupo.id, hold_id)
            return "TAKEN", None
        sol.estado = EstadoSolicitud.CON_OFERTA
        audit(s, "sistema", "OFERTA_CREADA", paciente.id, oferta_id=oferta.id, cupo=cupo.id,
              regla="orden de inscripción", modo=sol.modo.value)
        oferta_id, modo = oferta.id, sol.modo
        if modo == ModoReasignacion.AUTOMATICO:
            oferta.estado, oferta.respondida_en = EstadoOferta.ACEPTADA, timeutil.now()
        else:
            try:
                send(ctx, s, paciente, "sanibot_oferta",
                     [timeutil.fecha_larga(cupo.inicio), timeutil.hora(cupo.inicio), cupo.sede,
                      str(ctx.settings.offer_window_minutes)],
                     buttons=[("Aceptar", f"ACEPTAR:{oferta.id}"), ("Rechazar", f"RECHAZAR:{oferta.id}")],
                     oferta_id=oferta.id)
            except WhatsAppError as exc:
                oferta.estado, oferta.detalle = EstadoOferta.CANCELADA, f"WhatsApp: {exc}"
                sol.estado = EstadoSolicitud.ACTIVA
                ctx.sanitas.release_slot(cupo.id, hold_id)
                audit(s, "sistema", "OFERTA_NO_ENVIADA", paciente.id, oferta_id=oferta.id)
                return "SKIP", None
    if modo == ModoReasignacion.AUTOMATICO:
        reprogramming.reprogram(ctx, oferta_id)
    return "OFFERED", oferta_id


def respond_offer(ctx: AppContext, phone: str, oferta_id: str, accept: bool) -> str:
    with ctx.db.session() as s:
        oferta = s.get(OfertaCupo, oferta_id)
        if not oferta or oferta.solicitud.paciente.telefono_whatsapp != phone:
            return "NO_ENCONTRADA"
        paciente = oferta.solicitud.paciente
        if oferta.estado != EstadoOferta.PENDIENTE or oferta.expira_en < timeutil.now():
            send_safe(ctx, s, paciente, "sanibot_aviso", ["Esta oferta ya no está disponible. Seguimos buscando por ti."])
            return "NO_DISPONIBLE"
        oferta.respondida_en = timeutil.now()
        if accept:
            oferta.estado = EstadoOferta.ACEPTADA
            audit(s, "paciente", "OFERTA_ACEPTADA", paciente.id, oferta_id=oferta.id)
        else:
            oferta.estado = EstadoOferta.RECHAZADA
            oferta.solicitud.estado = EstadoSolicitud.ACTIVA
            if oferta.hold_id:
                ctx.sanitas.release_slot(oferta.cupo_id, oferta.hold_id)
            audit(s, "paciente", "OFERTA_RECHAZADA", paciente.id, oferta_id=oferta.id)
        cupo_id = oferta.cupo_id
    if accept:
        return "REPROGRAMADA" if reprogramming.reprogram(ctx, oferta_id) else "FALLIDA"
    offer_slot(ctx, cupo_id)  # el cupo pasa al siguiente candidato
    return "RECHAZADA"


def expire_offers(ctx: AppContext) -> int:
    """HU14: las ofertas sin respuesta vencen y el cupo pasa al siguiente candidato."""
    with ctx.db.session() as s:
        vencidas = list(s.scalars(select(OfertaCupo).where(OfertaCupo.estado == EstadoOferta.PENDIENTE,
                                                           OfertaCupo.expira_en < timeutil.now())))
        cupos = []
        for of in vencidas:
            of.estado = EstadoOferta.EXPIRADA
            if of.solicitud.estado == EstadoSolicitud.CON_OFERTA:
                of.solicitud.estado = EstadoSolicitud.ACTIVA
            if of.hold_id:
                ctx.sanitas.release_slot(of.cupo_id, of.hold_id)
            audit(s, "sistema", "OFERTA_EXPIRADA", of.solicitud.paciente_id, oferta_id=of.id)
            send_safe(ctx, s, of.solicitud.paciente, "sanibot_aviso",
                      ["La oferta de cupo venció sin respuesta. Tu cita actual se mantiene y seguimos buscando."])
            cupos.append(of.cupo_id)
    for c in cupos:
        offer_slot(ctx, c)
    return len(vencidas)


# ------------------------------------------------------------------ sincronización con el portal (HU41 / riesgo 43)
def _cancel_offer(ctx: AppContext, s: Session, sol: SolicitudEspera) -> list[str]:
    liberados = []
    for of in sol.ofertas:
        if of.estado in OFERTA_ACTIVA:
            of.estado, of.detalle = EstadoOferta.CANCELADA, "cambio de la cita fuera de SaniBot"
            if of.hold_id:
                ctx.sanitas.release_slot(of.cupo_id, of.hold_id)
                liberados.append(of.cupo_id)
    return liberados


def _cerrar_por_cambio_externo(ctx: AppContext, s: Session, sol: SolicitudEspera) -> list[str]:
    liberados = _cancel_offer(ctx, s, sol)
    fecha = timeutil.fecha_larga(sol.cita_actual_fecha) if sol.cita_actual_fecha else "tu cita"
    sol.estado = EstadoSolicitud.CANCELADA
    audit(s, "sistema", "SOLICITUD_CERRADA_POR_PORTAL", sol.paciente_id, solicitud_id=sol.id)
    send_safe(ctx, s, sol.paciente, "sanibot_aviso",
              [f"Vimos que tu cita del {fecha} se canceló desde los canales de Sanitas, así que cerramos la búsqueda "
               "asociada. Puedes crear una nueva cuando quieras."])
    return liberados


def sync_appointment(ctx: AppContext, tipo: str, payload: dict) -> None:
    liberados: list[str] = []
    with ctx.db.session() as s:
        sols = list(s.scalars(select(SolicitudEspera).where(
            SolicitudEspera.cita_actual_id == payload.get("appointment_id"),
            SolicitudEspera.estado.in_([EstadoSolicitud.ACTIVA, EstadoSolicitud.CON_OFERTA,
                                        EstadoSolicitud.SUSPENDIDA]))))
        for sol in sols:
            if tipo == "appointment.cancelled":
                liberados += _cerrar_por_cambio_externo(ctx, s, sol)
            elif tipo == "appointment.updated" and payload.get("start"):
                sol.cita_actual_fecha = timeutil.parse_iso(payload["start"])
                for of in sol.ofertas:
                    if of.estado == EstadoOferta.PENDIENTE and of.cupo_inicio >= sol.cita_actual_fecha:
                        liberados += _cancel_offer(ctx, s, sol)
                        sol.estado = EstadoSolicitud.ACTIVA
                audit(s, "sistema", "SOLICITUD_ACTUALIZADA_POR_PORTAL", sol.paciente_id, solicitud_id=sol.id)
                send_safe(ctx, s, sol.paciente, "sanibot_aviso",
                          [f"Actualizamos tu búsqueda: tu cita ahora es el {timeutil.fecha_larga(sol.cita_actual_fecha)}. "
                           "Seguiremos buscando una fecha anterior."])
    for c in liberados:
        offer_slot(ctx, c)


def scan_free_slots(ctx: AppContext, solicitud_id: str, limit: int = 40) -> str | None:
    """Al crear una solicitud, revisa los cupos que ya están libres (no solo los que se liberen después)."""
    with ctx.db.session() as s:
        sol = s.get(SolicitudEspera, solicitud_id)
        especialidad = sol.especialidad
    try:
        cupos = ctx.sanitas.search_free_slots(especialidad, limit)
    except SanitasError:
        return None
    with ctx.db.session() as s:
        sol = s.get(SolicitudEspera, solicitud_id)
        compatibles = [c for c in cupos if compatible(sol, c)]
    for c in sorted(compatibles, key=lambda c: c.inicio):
        oferta = offer_slot(ctx, c.id)
        if oferta:
            return oferta
    return None
