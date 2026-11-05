"""Solicitudes de espera: adelantar una cita existente o pedir una cita nueva (HU04, HU05, HU06, HU07, HU16)."""

from datetime import datetime

from sqlalchemy import select

from sanibot import timeutil
from sanibot.integrations.sanitas import SanitasAuthError
from sanibot.models import (
    OFERTA_ACTIVA,
    EstadoOferta,
    EstadoSolicitud,
    EstadoVinculo,
    ModoReasignacion,
    Paciente,
    SolicitudEspera,
    TipoSolicitud,
)
from sanibot.services import accounts, matching
from sanibot.services.audit import audit
from sanibot.services.context import ESPECIALIDADES, AppContext
from sanibot.services.notifier import send_safe

ACTIVAS = (EstadoSolicitud.ACTIVA, EstadoSolicitud.CON_OFERTA, EstadoSolicitud.SUSPENDIDA)
FRANJAS_VALIDAS = {"manana", "tarde"}


class RequestError(Exception):
    pass


def create_request(ctx: AppContext, paciente_id: str, *, tipo: TipoSolicitud, cita_id: str | None = None,
                   especialidad: str | None = None, franjas: list[str] | None = None,
                   fecha_limite: datetime | None = None, anticipacion_min_horas: int = 24,
                   modo: ModoReasignacion = ModoReasignacion.CON_CONFIRMACION) -> str:
    franjas = sorted(set(franjas or []))
    if not set(franjas) <= FRANJAS_VALIDAS:
        raise RequestError("Franja horaria no válida")
    if modo == ModoReasignacion.HIBRIDO:
        raise RequestError("El modo híbrido estará disponible en una versión posterior")
    if not 0 <= anticipacion_min_horas <= 168:
        raise RequestError("La anticipación mínima debe estar entre 0 y 168 horas")
    if fecha_limite and fecha_limite < timeutil.now():
        raise RequestError("La fecha límite ya pasó")
    with ctx.db.session() as s:
        paciente = s.get(Paciente, paciente_id)
        if not paciente.cuenta or paciente.cuenta.estado != EstadoVinculo.VIGENTE:
            raise RequestError("Debe vincular su cuenta de Sanitas")
        if not paciente.puede_recibir_whatsapp:
            raise RequestError("Debe verificar su WhatsApp y autorizar los mensajes de SaniBot: es el único canal de ofertas")
        token = accounts.get_access_token(ctx, s, paciente)
        sol = SolicitudEspera(paciente_id=paciente.id, tipo=tipo, sede=ctx.settings.sede, franjas=franjas,
                              fecha_limite=fecha_limite, anticipacion_min_horas=anticipacion_min_horas, modo=modo)
        if tipo == TipoSolicitud.ADELANTAR_CITA:
            cita = ctx.sanitas.get_appointment(token, cita_id or "")
            if not cita or cita.estado != "booked":
                raise RequestError("La cita no existe o ya no está vigente")
            if cita.sede != ctx.settings.sede:
                raise RequestError("SaniBot solo opera para citas de la sede " + ctx.settings.sede)
            duplicada = s.scalar(select(SolicitudEspera.id).where(SolicitudEspera.cita_actual_id == cita.id,
                                                                 SolicitudEspera.estado.in_(ACTIVAS)))
            if duplicada:
                raise RequestError("Ya tiene una búsqueda activa para esta cita")
            sol.especialidad, sol.cita_actual_id, sol.cita_actual_fecha = cita.especialidad, cita.id, cita.inicio
        else:
            if especialidad not in ESPECIALIDADES:
                raise RequestError("Especialidad no válida")
            duplicada = s.scalar(select(SolicitudEspera.id).where(
                SolicitudEspera.paciente_id == paciente.id, SolicitudEspera.tipo == TipoSolicitud.CITA_NUEVA,
                SolicitudEspera.especialidad == especialidad, SolicitudEspera.estado.in_(ACTIVAS)))
            if duplicada:
                raise RequestError("Ya tiene una búsqueda activa para esa especialidad")
            sol.especialidad = especialidad
        s.add(sol)
        s.flush()
        audit(s, "paciente", "SOLICITUD_CREADA", paciente.id, solicitud_id=sol.id, tipo=tipo.value, modo=modo.value)
        sol_id = sol.id
    matching.scan_free_slots(ctx, sol_id)
    return sol_id


def cancel_request(ctx: AppContext, paciente_id: str, solicitud_id: str) -> None:
    with ctx.db.session() as s:
        sol = s.get(SolicitudEspera, solicitud_id)
        if not sol or sol.paciente_id != paciente_id:
            raise RequestError("Solicitud no encontrada")
        if sol.estado not in ACTIVAS:
            return
        for of in sol.ofertas:
            if of.estado in OFERTA_ACTIVA:
                of.estado = EstadoOferta.CANCELADA
                if of.hold_id:
                    ctx.sanitas.release_slot(of.cupo_id, of.hold_id)
        sol.estado = EstadoSolicitud.CANCELADA
        audit(s, "paciente", "SOLICITUD_CANCELADA", paciente_id, solicitud_id=sol.id)


def expire_requests(ctx: AppContext) -> int:
    """Las solicitudes cuya fecha límite pasó quedan vencidas."""
    n = 0
    with ctx.db.session() as s:
        for sol in s.scalars(select(SolicitudEspera).where(SolicitudEspera.estado == EstadoSolicitud.ACTIVA,
                                                           SolicitudEspera.fecha_limite.is_not(None),
                                                           SolicitudEspera.fecha_limite < timeutil.now())):
            sol.estado = EstadoSolicitud.VENCIDA
            audit(s, "sistema", "SOLICITUD_VENCIDA", sol.paciente_id, solicitud_id=sol.id)
            send_safe(ctx, s, sol.paciente, "sanibot_aviso",
                      ["Tu búsqueda llegó a la fecha límite que definiste sin encontrar un cupo. Puedes crear una nueva."])
            n += 1
    return n


def list_appointments(ctx: AppContext, paciente_id: str):
    with ctx.db.session() as s:
        paciente = s.get(Paciente, paciente_id)
        try:
            token = accounts.get_access_token(ctx, s, paciente)
        except SanitasAuthError:
            return None
    return ctx.sanitas.list_appointments(token)
