"""Endpoints internos: auditoría, interruptor de seguridad, salud y métricas (HU23, HU24, HU25)."""

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text

from sanibot import timeutil
from sanibot.models import EstadoOferta, EventoCupo, OfertaCupo, RegistroAuditoria, SolicitudEspera
from sanibot.services import reprogramming, worker
from sanibot.services.context import AppContext

from .deps import get_ctx, require_admin

router = APIRouter(tags=["administración"])


@router.get("/health")
def health(ctx: AppContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        s.execute(text("SELECT 1"))
        abierto = reprogramming.breaker_state(s).abierto
        pendientes = s.scalar(select(func.count()).select_from(EventoCupo).where(EventoCupo.procesado.is_(False)))
    return {"status": "degradado" if abierto else "ok", "interruptor_abierto": abierto, "eventos_pendientes": pendientes}


@router.get("/admin/metricas", dependencies=[Depends(require_admin)])
def metrics(ctx: AppContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        por_estado = dict(s.execute(select(OfertaCupo.estado, func.count()).group_by(OfertaCupo.estado)).all())
        solicitudes = dict(s.execute(select(SolicitudEspera.estado, func.count())
                                     .group_by(SolicitudEspera.estado)).all())
        reprogramadas = list(s.scalars(select(OfertaCupo).where(OfertaCupo.estado == EstadoOferta.REPROGRAMADA)))
    # HU27: días que se adelantaron las citas existentes, cupos recuperados y tasa de aceptación.
    dias = [(o.cita_anterior_fecha - o.cupo_inicio).total_seconds() / 86400 for o in reprogramadas
            if o.cita_anterior_fecha]
    respondidas = sum(por_estado.get(k, 0) for k in (EstadoOferta.REPROGRAMADA, EstadoOferta.RECHAZADA,
                                                      EstadoOferta.EXPIRADA, EstadoOferta.FALLIDA))
    return {"ofertas": {str(k): v for k, v in por_estado.items()},
            "solicitudes": {str(k): v for k, v in solicitudes.items()},
            "cupos_recuperados": len(reprogramadas),
            "dias_adelantados_promedio": round(sum(dias) / len(dias), 1) if dias else None,
            "tasa_aceptacion": round(por_estado.get(EstadoOferta.REPROGRAMADA, 0) / respondidas, 2) if respondidas else None}


@router.get("/admin/auditoria", dependencies=[Depends(require_admin)])
def auditoria(paciente_id: str | None = None, accion: str | None = None, horas: int = 72, limit: int = 200,
              ctx: AppContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        q = select(RegistroAuditoria).where(RegistroAuditoria.fecha >= timeutil.now() - timedelta(hours=horas))
        if paciente_id:
            q = q.where(RegistroAuditoria.paciente_id == paciente_id)
        if accion:
            q = q.where(RegistroAuditoria.accion == accion)
        rows = s.scalars(q.order_by(RegistroAuditoria.id.desc()).limit(min(limit, 1000)))
        return [{"id": r.id, "fecha": r.fecha.isoformat(), "actor": r.actor, "accion": r.accion,
                 "paciente_id": r.paciente_id, "detalle": r.detalle} for r in rows]


@router.get("/admin/interruptor", dependencies=[Depends(require_admin)])
def interruptor(ctx: AppContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        st = reprogramming.breaker_state(s)
        return {"abierto": st.abierto, "abierto_en": st.abierto_en and st.abierto_en.isoformat(), "motivo": st.motivo}


@router.post("/admin/interruptor/cerrar")
def cerrar_interruptor(actor: str = Depends(require_admin), ctx: AppContext = Depends(get_ctx)):
    reprogramming.breaker_close(ctx, actor)
    return {"abierto": False}


@router.post("/admin/tick", dependencies=[Depends(require_admin)])
def run_tick(ctx: AppContext = Depends(get_ctx)):
    return worker.tick(ctx)
