"""Integración con WhatsApp: único canal de comunicación de SaniBot con el paciente (RNF13).

Los mensajes que inicia SaniBot usan plantillas aprobadas por la plataforma (categoría utilidad / autenticación),
desde la cuenta verificada que provee Sanitas. Ningún mensaje incluye la especialidad, datos clínicos ni enlaces
de inicio de sesión (RNF05 / riesgos 11 y 13).

Las plantillas se definen una sola vez en PLANTILLAS: de ahí salen el texto de respaldo (mensaje de sesión) y la
solicitud de registro ante Meta (python -m sanibot.plantillas registrar).
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from sanibot.config import Settings

log = logging.getLogger("sanibot.whatsapp")

# Códigos de error de la API de WhatsApp Cloud que conviene explicar con palabras.
ERRORES_CONOCIDOS = {
    131030: "Ese número no está en la lista de destinatarios permitidos del número de prueba de Meta. "
            "Agrégalo en el panel de Meta (WhatsApp → Configuración de la API → «Para») y vuelve a intentarlo.",
    132001: "La plantilla todavía no existe o no está aprobada en Meta. Revisa su estado con "
            "«python -m sanibot.plantillas estado».",
    132000: "La cantidad de datos enviados no coincide con la plantilla registrada en Meta.",
    132012: "El formato de los datos no coincide con la plantilla registrada en Meta.",
    131026: "WhatsApp no pudo entregar el mensaje a ese número (puede que no tenga WhatsApp).",
    131047: "Pasaron más de 24 horas desde el último mensaje del paciente; solo se pueden enviar plantillas.",
    190: "El token de acceso de Meta venció o no es válido. Genera uno nuevo y actualiza el archivo .env.",
    10: "La aplicación de Meta no tiene permiso para enviar mensajes con este número.",
    100: "Meta rechazó la solicitud por un parámetro inválido (revisa el Phone Number ID del .env).",
}


# Plantilla inexistente, no aprobada o con parámetros distintos a los registrados.
PLANTILLA_NO_DISPONIBLE = {132001, 132000, 132012, 132015, 132016}


class WhatsAppError(Exception):
    def __init__(self, message: str, code: int | None = None, user_message: str | None = None):
        super().__init__(message)
        self.code = code
        self.user_message = user_message or "No pudimos enviar el mensaje de WhatsApp en este momento. Intenta de nuevo."


@dataclass(frozen=True)
class Plantilla:
    nombre: str
    categoria: str  # UTILITY | AUTHENTICATION
    cuerpo: str  # con variables {{1}}, {{2}}...
    ejemplos: tuple[str, ...] = ()
    pie: str | None = None
    botones: tuple[str, ...] = ()  # respuestas rápidas

    def preview(self, params: list[str]) -> str:
        texto = re.sub(r"\{\{(\d+)\}\}", lambda m: params[int(m.group(1)) - 1] if int(m.group(1)) <= len(params)
                       else m.group(0), self.cuerpo)
        return f"{texto}\n\n{self.pie}" if self.pie else texto

    def definicion_meta(self, idioma: str, minutos_codigo: int = 10) -> dict:
        """Cuerpo de POST /{WABA_ID}/message_templates."""
        if self.categoria == "AUTHENTICATION":
            # Meta fija el texto de las plantillas de autenticación; solo se eligen estas opciones.
            componentes = [{"type": "BODY", "add_security_recommendation": True},
                           {"type": "FOOTER", "code_expiration_minutes": minutos_codigo},
                           {"type": "BUTTONS", "buttons": [{"type": "OTP", "otp_type": "COPY_CODE",
                                                            "text": "Copiar código"}]}]
            return {"name": self.nombre, "language": idioma, "category": "AUTHENTICATION",
                    "message_send_ttl_seconds": minutos_codigo * 60, "components": componentes}
        cuerpo: dict = {"type": "BODY", "text": self.cuerpo}
        if self.ejemplos:
            cuerpo["example"] = {"body_text": [list(self.ejemplos)]}
        componentes = [cuerpo]
        if self.pie:
            componentes.append({"type": "FOOTER", "text": self.pie})
        if self.botones:
            componentes.append({"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": b} for b in self.botones]})
        return {"name": self.nombre, "language": idioma, "category": self.categoria, "components": componentes}


PLANTILLAS: dict[str, Plantilla] = {p.nombre: p for p in (
    Plantilla("sanibot_bienvenida", "UTILITY",
              "Hola {{1}}, soy SaniBot, el bot complementario y autorizado de EPS Sanitas. Te avisaré por aquí "
              "cuando encuentre una cita más pronta. Nunca te pediré contraseñas ni pagos. Escribe BAJA si no "
              "quieres recibir más mensajes.",
              ejemplos=("Ana",)),
    Plantilla("sanibot_oferta_cupo", "UTILITY",
              "¡Hay un cupo disponible antes de tu cita! 📅 {{1}} a las {{2}}, sede {{3}}. Tienes {{4}} minutos "
              "para responder. Si no respondes, tu cita actual se mantiene.",
              ejemplos=("martes 3 de noviembre", "9:00 a. m.", "Teusaquillo", "30"), botones=("Aceptar", "Rechazar")),
    Plantilla("sanibot_confirmacion", "UTILITY",
              "Listo. Tu nueva cita es el {{1}} a las {{2}}, sede {{3}}. Sanitas te enviará por correo, como "
              "siempre, la cancelación de tu cita anterior y la confirmación de la nueva.",
              ejemplos=("martes 3 de noviembre", "9:00 a. m.", "Teusaquillo")),
    Plantilla("sanibot_aviso", "UTILITY",
              # Meta no acepta un cuerpo que empiece o termine en una variable.
              "Aviso de SaniBot: {{1}} Si tienes dudas, comunícate con Sanitas por sus canales oficiales.",
              ejemplos=("La oferta de cupo venció sin respuesta. Tu cita actual se mantiene y seguimos buscando.",)),
)}


def error_from_response(r: httpx.Response) -> WhatsAppError:
    code, detalle = None, r.text[:300]
    try:
        err = r.json().get("error", {})
        code = err.get("code")
        detalle = err.get("error_data", {}).get("details") or err.get("message") or detalle
    except (ValueError, AttributeError):
        pass
    return WhatsAppError(f"WhatsApp respondió {r.status_code} (código {code}): {detalle}", code,
                         ERRORES_CONOCIDOS.get(code))


@dataclass
class OutgoingMessage:
    to: str
    template: str
    params: list[str]
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (texto, payload)
    id: str = field(default_factory=lambda: f"wamid.{uuid.uuid4().hex}")
    sent_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def preview(self) -> str:
        p = PLANTILLAS.get(self.template)
        return p.preview(self.params) if p else json.dumps(self.params, ensure_ascii=False)


class WhatsAppClient:
    def send_template(self, to: str, template: str, params: list[str],
                      buttons: list[tuple[str, str]] | None = None) -> str:
        raise NotImplementedError


class ConsoleWhatsApp(WhatsAppClient):
    """Doble de pruebas: guarda los mensajes en memoria. Solo lo usan las pruebas automáticas."""

    def __init__(self):
        self.outbox: list[OutgoingMessage] = []
        self.inbox: list[dict] = []  # respuestas simuladas en las pruebas
        self.fail_next: bool | int = False  # True o un código de error de Meta para simular fallos

    def send_template(self, to, template, params, buttons=None):
        if self.fail_next:
            code = self.fail_next if isinstance(self.fail_next, int) and self.fail_next is not True else None
            self.fail_next = False
            raise WhatsAppError("Fallo simulado de envío", code, ERRORES_CONOCIDOS.get(code))
        msg = OutgoingMessage(to, template, params, buttons or [])
        self.outbox.append(msg)
        log.info("WhatsApp [%s] -> %s: %s", template, to[-4:].rjust(len(to), "*"), msg.preview())
        return msg.id

    def record_reply(self, frm: str, texto: str) -> None:
        self.inbox.append({"from": frm, "texto": texto, "at": datetime.now(UTC), "id": f"in.{uuid.uuid4().hex}"})
        del self.inbox[:-100]

    def last_to(self, to: str) -> OutgoingMessage | None:
        return next((m for m in reversed(self.outbox) if m.to == to), None)


class CloudWhatsApp(WhatsAppClient):
    """API de WhatsApp Cloud (Meta). Con el número de prueba solo se puede enviar a destinatarios registrados."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        if not settings.whatsapp_phone_number_id or not settings.whatsapp_access_token:
            raise ValueError("Configure SANIBOT_WHATSAPP_PHONE_NUMBER_ID y SANIBOT_WHATSAPP_ACCESS_TOKEN")
        self.s = settings
        self.http = http or httpx.Client(timeout=10)

    def send_template(self, to, template, params, buttons=None):
        components: list[dict] = []
        if params:
            components.append({"type": "body", "parameters": [{"type": "text", "text": p} for p in params]})
        for i, (_, payload) in enumerate(buttons or []):
            components.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                               "parameters": [{"type": "payload", "payload": payload}]})
        body = {"messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {"name": template, "language": {"code": self.s.whatsapp_template_language},
                             "components": components}}
        try:
            return self._post(body, template)
        except WhatsAppError as exc:
            if exc.code not in PLANTILLA_NO_DISPONIBLE or template not in PLANTILLAS:
                raise
            # La plantilla aún no está aprobada. Si el paciente escribió en las últimas 24 horas (por ejemplo, al
            # verificar su número), WhatsApp permite responderle con un mensaje normal con botones.
            log.info("Plantilla %s no disponible (código %s): se envía como mensaje de sesión", template, exc.code)
            return self.send_session(to, PLANTILLAS[template].preview(params), buttons)

    def send_session(self, to, text, buttons=None):
        """Mensaje dentro de la ventana de 24 horas: texto o botones de respuesta, sin plantilla."""
        if buttons:
            body = {"messaging_product": "whatsapp", "to": to, "type": "interactive",
                    "interactive": {"type": "button", "body": {"text": text[:1024]},
                                    "action": {"buttons": [{"type": "reply", "reply": {"id": payload, "title": label[:20]}}
                                                           for label, payload in buttons[:3]]}}}
        else:
            body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4096]}}
        return self._post(body, "mensaje de sesión")

    def _post(self, body: dict, descripcion: str) -> str:
        url = f"{self.s.whatsapp_api_url}/{self.s.whatsapp_api_version}/{self.s.whatsapp_phone_number_id}/messages"
        try:
            r = self.http.post(url, json=body, headers={"Authorization": f"Bearer {self.s.whatsapp_access_token}"})
        except httpx.HTTPError as exc:
            raise WhatsAppError("WhatsApp no disponible", user_message="No hay conexión con WhatsApp en este momento. "
                                "Revisa tu conexión a internet e intenta de nuevo.") from exc
        if r.status_code >= 400:
            exc = error_from_response(r)
            log.warning("Envío de %s falló: %s", descripcion, exc)
            raise exc
        return r.json()["messages"][0]["id"]


def normalize_phone(raw: str) -> str:
    """Normaliza a formato internacional sin '+'. Los celulares colombianos de 10 dígitos reciben el prefijo 57."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10 and digits.startswith("3"):
        digits = "57" + digits
    if not 11 <= len(digits) <= 15:
        raise ValueError("Número de WhatsApp inválido")
    return digits


def parse_webhook(payload: dict) -> tuple[list[dict], list[dict]]:
    """Extrae mensajes entrantes y estados de entrega del webhook de WhatsApp Cloud.

    Devuelve (mensajes, estados). Cada mensaje: {"from", "kind": "button"|"text", "value", "id"}.
    """
    messages, statuses = [], []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for m in value.get("messages", []):
                kind, val = None, None
                if m.get("type") == "button":
                    kind, val = "button", m.get("button", {}).get("payload")
                elif m.get("type") == "interactive":
                    kind, val = "button", m.get("interactive", {}).get("button_reply", {}).get("id")
                elif m.get("type") == "text":
                    kind, val = "text", m.get("text", {}).get("body", "")
                if kind:
                    messages.append({"from": m.get("from", ""), "kind": kind, "value": val or "", "id": m.get("id")})
            for st in value.get("statuses", []):
                errores = st.get("errors") or [{}]
                statuses.append({"id": st.get("id"), "status": st.get("status"), "error_code": errores[0].get("code")})
    return messages, statuses
