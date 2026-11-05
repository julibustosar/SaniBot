"""Reprogramación atómica (HU13) e interruptor de seguridad (HU25).

Regla central: la cita nueva se reserva ANTES de cancelar la anterior. Si la reserva falla, el paciente conserva su cita
original (riesgo 5). Si la cancelación de la anterior falla, se reintenta después: es preferible que el paciente tenga
dos citas por unos minutos a que se quede sin ninguna (riesgo 40).
"""

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sanibot import timeutil
from sanibot.integrations.sanitas import SanitasAuthError, SanitasError
from sanibot.models import (
    EstadoInterruptor,
    EstadoOferta,
    EstadoSolicitud,
    OfertaCupo,
    RegistroAuditoria,
    TipoSolicitud,
)
from sanibot.services import accounts, requests_service
from sanibot.services.audit import audit
from sanibot.services.context import AppContext
from sanibot.services.notifier import send_safe

log = logging.getLogger("sanibot.reprogramming")


# ------------------------------------------------------------------ interruptor de seguridad
def breaker_state(s: Session) -> EstadoInterruptor:
    st = s.get(EstadoInterruptor, 1)
    if not st:
        st = EstadoInterruptor(id=1, abierto=False)
        s.add(st)
        s.flush()
    return st


def breaker_register_success(ctx: AppContext, s: Session) -> None:
    desde = timeutil.now() - timedelta(minutes=1)
    n = s.scalar(select(func.count()).select_from(RegistroAuditoria)
                 .where(RegistroAuditoria.accion == "REPROGRAMACION_OK", RegistroAuditoria.fecha >= desde))
    if n >= ctx.settings.breaker_max_per_minute:
        st = breaker_state(s)
        if not st.abierto:
            st.abierto, st.abierto_en = True, timeutil.now()
            st.motivo = f"{n} reprogramaciones en el último minuto (umbral {ctx.settings.breaker_max_per_minute})"
            audit(s, "sistema", "INTERRUPTOR_ABIERTO", motivo=st.motivo)
            log.error("ALERTA: interruptor de seguridad abierto: %s", st.motivo)


def breaker_close(ctx: AppContext, actor: str) -> None:
    with ctx.db.session() as s:
        st = breaker_state(s)
        st.abierto, st.motivo = False, None
        audit(s, actor, "INTERRUPTOR_CERRADO")


# ------------------------------------------------------------------ reprogramación
def _fail(ctx: AppContext, s: Session, oferta: OfertaCupo, motivo: str) -> None:
    oferta.estado, oferta.detalle = EstadoOferta.FALLIDA, motivo
    if oferta.hold_id:
        ctx.sanitas.release_slot(oferta.cupo_id, oferta.hold_id)
    sol = oferta.solicitud
    if sol.estado == EstadoSolicitud.CON_OFERTA:
        sol.estado = EstadoSolicitud.ACTIVA
    audit(s, "sistema", "REPROGRAMACION_FALLIDA", sol.paciente_id, oferta_id=oferta.id, motivo=motivo)
    send_safe(ctx, s, sol.paciente, "sanibot_aviso", [
        "No fue posible completar el cambio de tu cita. Tu cita actual se mantiene y seguimos buscando."])


def reprogram(ctx: AppContext, oferta_id: str) -> bool:
    with ctx.db.session() as s:
        oferta = s.get(OfertaCupo, oferta_id)
        if not oferta or oferta.estado != EstadoOferta.ACEPTADA:
            return False
        sol, paciente = oferta.solicitud, oferta.solicitud.paciente
        if breaker_state(s).abierto:
            _fail(ctx, s, oferta, "interruptor de seguridad abierto")
            return False
        try:
            token = accounts.get_access_token(ctx, s, paciente)
        except SanitasAuthError:
            _fail(ctx, s, oferta, "autorización vencida")
            return False
        try:
            if sol.tipo == TipoSolicitud.CITA_NUEVA and requests_service.cita_activa_de(ctx, token, sol.especialidad):
                # El paciente consiguió una cita de esa especialidad por otro canal mientras esperaba.
                _fail(ctx, s, oferta, f"ya tiene una cita activa de {sol.especialidad}")
                sol.estado = EstadoSolicitud.CUMPLIDA
                return False
            nueva = ctx.sanitas.book(token, oferta.cupo_id, oferta.hold_id,
                                     sol.cita_actual_id if sol.tipo == TipoSolicitud.ADELANTAR_CITA else None)
        except SanitasError as exc:
            _fail(ctx, s, oferta, f"no se pudo reservar el cupo: {exc}")
            return False
        oferta.cita_nueva_id, oferta.hold_id = nueva.id, None
        if sol.tipo == TipoSolicitud.ADELANTAR_CITA:
            oferta.cita_anterior_id, oferta.cita_anterior_fecha = sol.cita_actual_id, sol.cita_actual_fecha
        s.commit()  # la cita nueva queda registrada antes de tocar la anterior

        if oferta.cita_anterior_id:
            try:
                ctx.sanitas.cancel(token, oferta.cita_anterior_id)
            except SanitasError:
                oferta.cancelacion_anterior_pendiente = True
                audit(s, "sistema", "CANCELACION_ANTERIOR_PENDIENTE", paciente.id, cita=oferta.cita_anterior_id)
        oferta.estado = EstadoOferta.REPROGRAMADA
        sol.estado = EstadoSolicitud.CUMPLIDA
        sol.cita_actual_id, sol.cita_actual_fecha = nueva.id, nueva.inicio
        audit(s, "sistema", "REPROGRAMACION_OK", paciente.id, oferta_id=oferta.id, cita_nueva=nueva.id,
              cita_anterior=oferta.cita_anterior_id)
        breaker_register_success(ctx, s)
        send_safe(ctx, s, paciente, "sanibot_cita_agendada",
                  [timeutil.fecha_larga(nueva.inicio), timeutil.hora(nueva.inicio), nueva.sede])
        return True


def retry_pending_cancellations(ctx: AppContext) -> int:
    done = 0
    with ctx.db.session() as s:
        for of in s.scalars(select(OfertaCupo).where(OfertaCupo.cancelacion_anterior_pendiente.is_(True))):
            try:
                token = accounts.get_access_token(ctx, s, of.solicitud.paciente)
                ctx.sanitas.cancel(token, of.cita_anterior_id)
            except SanitasError:
                continue
            of.cancelacion_anterior_pendiente = False
            audit(s, "sistema", "CANCELACION_ANTERIOR_OK", of.solicitud.paciente_id, cita=of.cita_anterior_id)
            done += 1
    return done
