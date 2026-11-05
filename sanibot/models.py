"""Modelo de datos de SaniBot (ver diagrama de clases y modelo entidad-relación)."""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, event, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from sanibot.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class TZDateTime(TypeDecorator):
    """DateTime que siempre guarda y devuelve UTC con zona horaria (SQLite no la conserva)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            raise ValueError("Las fechas deben tener zona horaria")
        return value.astimezone(UTC) if value is not None else None

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class EstadoVinculo(enum.StrEnum):
    VIGENTE = "VIGENTE"
    INVALIDA = "INVALIDA"
    REVOCADA = "REVOCADA"


class TipoSolicitud(enum.StrEnum):
    ADELANTAR_CITA = "ADELANTAR_CITA"
    CITA_NUEVA = "CITA_NUEVA"


class ModoReasignacion(enum.StrEnum):
    AUTOMATICO = "AUTOMATICO"
    CON_CONFIRMACION = "CON_CONFIRMACION"
    HIBRIDO = "HIBRIDO"  # HU15 (Could): planeado para el Sprint 7


class EstadoSolicitud(enum.StrEnum):
    ACTIVA = "ACTIVA"
    CON_OFERTA = "CON_OFERTA"
    SUSPENDIDA = "SUSPENDIDA"
    CUMPLIDA = "CUMPLIDA"
    VENCIDA = "VENCIDA"
    CANCELADA = "CANCELADA"


class EstadoOferta(enum.StrEnum):
    PENDIENTE = "PENDIENTE"
    ACEPTADA = "ACEPTADA"
    RECHAZADA = "RECHAZADA"
    EXPIRADA = "EXPIRADA"
    CANCELADA = "CANCELADA"
    REPROGRAMADA = "REPROGRAMADA"
    FALLIDA = "FALLIDA"


# Estados en los que una oferta "ocupa" el cupo: la base de datos impide dos ofertas activas por cupo (RNF04 / riesgo 4).
OFERTA_ACTIVA = (EstadoOferta.PENDIENTE.value, EstadoOferta.ACEPTADA.value)


class Paciente(Base):
    __tablename__ = "paciente"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    sanitas_sub: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(120), default="")
    telefono_whatsapp: Mapped[str | None] = mapped_column(String(20), index=True)
    whatsapp_verificado_en: Mapped[datetime | None] = mapped_column(TZDateTime())
    opt_in_whatsapp_en: Mapped[datetime | None] = mapped_column(TZDateTime())
    opt_out_whatsapp_en: Mapped[datetime | None] = mapped_column(TZDateTime())
    creado_en: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)

    cuenta: Mapped["CuentaVinculada | None"] = relationship(back_populates="paciente", uselist=False)
    solicitudes: Mapped[list["SolicitudEspera"]] = relationship(back_populates="paciente")

    @property
    def puede_recibir_whatsapp(self) -> bool:
        return bool(self.telefono_whatsapp and self.whatsapp_verificado_en and self.opt_in_whatsapp_en
                    and not self.opt_out_whatsapp_en)


class Consentimiento(Base):
    __tablename__ = "consentimiento"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paciente_id: Mapped[str] = mapped_column(ForeignKey("paciente.id"), index=True)
    version: Mapped[str] = mapped_column(String(10))
    finalidad: Mapped[str] = mapped_column(Text)
    aceptado_en: Mapped[datetime] = mapped_column(TZDateTime())
    revocado_en: Mapped[datetime | None] = mapped_column(TZDateTime())


class CuentaVinculada(Base):
    __tablename__ = "cuenta_vinculada"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paciente_id: Mapped[str] = mapped_column(ForeignKey("paciente.id"), unique=True)
    refresh_token_cifrado: Mapped[str | None] = mapped_column(Text)
    access_token_cifrado: Mapped[str | None] = mapped_column(Text)
    access_expira_en: Mapped[datetime | None] = mapped_column(TZDateTime())
    scopes: Mapped[str] = mapped_column(String(200), default="")
    estado: Mapped[EstadoVinculo] = mapped_column(Enum(EstadoVinculo, native_enum=False), default=EstadoVinculo.VIGENTE)
    actualizado_en: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, onupdate=utcnow)

    paciente: Mapped[Paciente] = relationship(back_populates="cuenta")


class VerificacionWhatsApp(Base):
    __tablename__ = "verificacion_whatsapp"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paciente_id: Mapped[str] = mapped_column(ForeignKey("paciente.id"), index=True)
    telefono: Mapped[str] = mapped_column(String(20))
    codigo_hash: Mapped[str] = mapped_column(String(64))
    expira_en: Mapped[datetime] = mapped_column(TZDateTime())
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    usado: Mapped[bool] = mapped_column(Boolean, default=False)


class SolicitudEspera(Base):
    __tablename__ = "solicitud_espera"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paciente_id: Mapped[str] = mapped_column(ForeignKey("paciente.id"), index=True)
    tipo: Mapped[TipoSolicitud] = mapped_column(Enum(TipoSolicitud, native_enum=False))
    especialidad: Mapped[str] = mapped_column(String(80), index=True)
    sede: Mapped[str] = mapped_column(String(80))
    cita_actual_id: Mapped[str | None] = mapped_column(String(64), index=True)
    cita_actual_fecha: Mapped[datetime | None] = mapped_column(TZDateTime())
    fecha_limite: Mapped[datetime | None] = mapped_column(TZDateTime())
    franjas: Mapped[list[str]] = mapped_column(JSON, default=list)
    anticipacion_min_horas: Mapped[int] = mapped_column(Integer, default=24)
    modo: Mapped[ModoReasignacion] = mapped_column(Enum(ModoReasignacion, native_enum=False),
                                                   default=ModoReasignacion.CON_CONFIRMACION)
    estado: Mapped[EstadoSolicitud] = mapped_column(Enum(EstadoSolicitud, native_enum=False),
                                                    default=EstadoSolicitud.ACTIVA, index=True)
    creada_en: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    actualizada_en: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, onupdate=utcnow)

    paciente: Mapped[Paciente] = relationship(back_populates="solicitudes")
    ofertas: Mapped[list["OfertaCupo"]] = relationship(back_populates="solicitud")


class EventoCupo(Base):
    """Eventos recibidos de Sanitas. El id externo es único: un evento duplicado se ignora (idempotencia)."""

    __tablename__ = "evento_cupo"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tipo: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    recibido_en: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    procesado: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class OfertaCupo(Base):
    __tablename__ = "oferta_cupo"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    solicitud_id: Mapped[str] = mapped_column(ForeignKey("solicitud_espera.id"), index=True)
    cupo_id: Mapped[str] = mapped_column(String(64), index=True)
    cupo_inicio: Mapped[datetime] = mapped_column(TZDateTime())
    cupo_profesional: Mapped[str] = mapped_column(String(120), default="")
    cupo_sede: Mapped[str] = mapped_column(String(80), default="")
    hold_id: Mapped[str | None] = mapped_column(String(64))
    estado: Mapped[EstadoOferta] = mapped_column(Enum(EstadoOferta, native_enum=False), default=EstadoOferta.PENDIENTE)
    creada_en: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    expira_en: Mapped[datetime] = mapped_column(TZDateTime())
    respondida_en: Mapped[datetime | None] = mapped_column(TZDateTime())
    cita_nueva_id: Mapped[str | None] = mapped_column(String(64))
    cita_anterior_id: Mapped[str | None] = mapped_column(String(64))
    cita_anterior_fecha: Mapped[datetime | None] = mapped_column(TZDateTime())
    cancelacion_anterior_pendiente: Mapped[bool] = mapped_column(Boolean, default=False)
    detalle: Mapped[str | None] = mapped_column(Text)

    solicitud: Mapped[SolicitudEspera] = relationship(back_populates="ofertas")

    __table_args__ = (
        Index("uq_oferta_activa_por_cupo", "cupo_id", unique=True,
              sqlite_where=text("estado IN ('PENDIENTE','ACEPTADA')"),
              postgresql_where=text("estado IN ('PENDIENTE','ACEPTADA')")),
        Index("uq_oferta_activa_por_solicitud", "solicitud_id", unique=True,
              sqlite_where=text("estado IN ('PENDIENTE','ACEPTADA')"),
              postgresql_where=text("estado IN ('PENDIENTE','ACEPTADA')")),
    )


class Notificacion(Base):
    __tablename__ = "notificacion"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paciente_id: Mapped[str] = mapped_column(ForeignKey("paciente.id"), index=True)
    oferta_id: Mapped[str | None] = mapped_column(ForeignKey("oferta_cupo.id"))
    plantilla: Mapped[str] = mapped_column(String(80))
    wa_message_id: Mapped[str | None] = mapped_column(String(120), index=True)
    estado_entrega: Mapped[str] = mapped_column(String(20), default="ENVIADA")
    detalle_error: Mapped[str | None] = mapped_column(String(300))
    enviada_en: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class RegistroAuditoria(Base):
    """Log de auditoría de solo inserción (HU24): no se permite actualizar ni borrar registros."""

    __tablename__ = "registro_auditoria"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fecha: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(120))
    accion: Mapped[str] = mapped_column(String(60), index=True)
    paciente_id: Mapped[str | None] = mapped_column(String(36), index=True)
    detalle: Mapped[dict] = mapped_column(JSON, default=dict)


class EstadoInterruptor(Base):
    """Interruptor de seguridad (HU25 / riesgo 37). Fila única con id=1."""

    __tablename__ = "estado_interruptor"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    abierto: Mapped[bool] = mapped_column(Boolean, default=False)
    abierto_en: Mapped[datetime | None] = mapped_column(TZDateTime())
    motivo: Mapped[str | None] = mapped_column(Text)


@event.listens_for(RegistroAuditoria, "before_update")
def _auditoria_inmutable_update(*_):
    raise PermissionError("El log de auditoría no se puede modificar")


@event.listens_for(RegistroAuditoria, "before_delete")
def _auditoria_inmutable_delete(*_):
    raise PermissionError("El log de auditoría no se puede borrar")
