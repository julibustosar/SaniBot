"""Registro de auditoría (HU24). Nunca se registran tokens, contraseñas ni códigos de verificación."""

import logging

from sqlalchemy.orm import Session

from sanibot.models import RegistroAuditoria

log = logging.getLogger("sanibot.audit")
_PROHIBIDOS = ("token", "password", "contrasena", "codigo", "secret")


def audit(s: Session, actor: str, accion: str, paciente_id: str | None = None, **detalle) -> None:
    limpio = {k: v for k, v in detalle.items() if not any(p in k.lower() for p in _PROHIBIDOS)}
    s.add(RegistroAuditoria(actor=actor, accion=accion, paciente_id=paciente_id, detalle=limpio))
    log.info("auditoria %s %s paciente=%s %s", actor, accion, paciente_id, limpio)
