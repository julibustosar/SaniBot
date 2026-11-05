"""Envío de mensajes de WhatsApp con control de privacidad (RNF05 / HU32 / riesgo 13)."""

import logging

from sqlalchemy.orm import Session

from sanibot.integrations.whatsapp import WhatsAppError
from sanibot.models import Notificacion, Paciente
from sanibot.services.context import ESPECIALIDADES, AppContext

log = logging.getLogger("sanibot.notifier")


class PrivacyViolation(ValueError):
    pass


def check_privacy(params: list[str]) -> None:
    for p in params:
        low = p.lower()
        if any(e.lower() in low for e in ESPECIALIDADES) or "http" in low:
            raise PrivacyViolation("Los mensajes no pueden incluir la especialidad ni enlaces")


def send(ctx: AppContext, s: Session, paciente: Paciente, template: str, params: list[str],
         buttons: list[tuple[str, str]] | None = None, oferta_id: str | None = None) -> str | None:
    """Envía una plantilla al paciente. Devuelve el id del mensaje o None si el paciente no recibe mensajes."""
    check_privacy(params)
    if not paciente.puede_recibir_whatsapp:
        log.info("Paciente %s sin autorización de WhatsApp; no se envía %s", paciente.id, template)
        return None
    wa_id = ctx.whatsapp.send_template(paciente.telefono_whatsapp, template, params, buttons)
    s.add(Notificacion(paciente_id=paciente.id, oferta_id=oferta_id, plantilla=template, wa_message_id=wa_id))
    return wa_id


def send_safe(ctx: AppContext, s: Session, paciente: Paciente, template: str, params: list[str]) -> None:
    """Para avisos informativos: un fallo de WhatsApp no debe interrumpir el proceso principal."""
    try:
        send(ctx, s, paciente, template, params)
    except WhatsAppError as exc:
        log.warning("No se pudo enviar %s al paciente %s: %s", template, paciente.id, exc)
