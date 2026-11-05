"""Consentimiento, vinculación OAuth, tokens, verificación de WhatsApp y desvinculación (HU01, HU02, HU08, HU16, HU18, HU28)."""

import logging
import re
import secrets
import unicodedata
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from sanibot import timeutil
from sanibot.integrations.sanitas import SanitasAuthError, pkce_pair
from sanibot.integrations.whatsapp import WhatsAppError, normalize_phone
from sanibot.models import (
    OFERTA_ACTIVA,
    Consentimiento,
    CuentaVinculada,
    EstadoOferta,
    EstadoSolicitud,
    EstadoVinculo,
    Notificacion,
    OfertaCupo,
    Paciente,
    SolicitudEspera,
    VerificacionWhatsApp,
)
from sanibot.security.crypto import generate_code, hash_code
from sanibot.services.audit import audit
from sanibot.services.context import AppContext
from sanibot.services.notifier import send_safe

log = logging.getLogger("sanibot.accounts")

# Frases con las que el paciente se da de baja. No importan mayúsculas, tildes, signos ni espacios de más.
FRASES_BAJA = {"baja", "stop", "darme de baja", "dar de baja", "me doy de baja", "quiero darme de baja"}


def es_mensaje_de_baja(texto: str) -> bool:
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    palabras = re.sub(r"[^a-z ]", " ", sin_tildes.lower()).split()
    return " ".join(palabras) in FRASES_BAJA

FINALIDAD = ("SaniBot tratará sus datos de identificación, contacto de WhatsApp y citas en la sede Teusaquillo con la "
             "única finalidad de buscar, ofrecer y reprogramar citas según sus preferencias. No accede a su historia "
             "clínica. Puede revocar esta autorización en cualquier momento (Ley 1581 de 2012).")


class AccountError(Exception):
    pass


# ------------------------------------------------------------------ vinculación OAuth (PKCE)
def start_link(ctx: AppContext, session: dict) -> str:
    if not session.get("consent_at"):
        raise AccountError("Debe aceptar el consentimiento antes de vincular la cuenta")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    session["oauth"] = {"state": state, "verifier": verifier}
    return ctx.sanitas.authorize_url(state, challenge)


def complete_link(ctx: AppContext, session: dict, code: str, state: str) -> str:
    pending = session.pop("oauth", None)
    if not pending or not secrets.compare_digest(pending["state"], state):
        raise AccountError("La solicitud de vinculación no es válida o venció. Inténtelo de nuevo.")
    tokens = ctx.sanitas.exchange_code(code, pending["verifier"])
    info = ctx.sanitas.userinfo(tokens.access_token)
    with ctx.db.session() as s:
        paciente = s.scalar(select(Paciente).where(Paciente.sanitas_sub == info["sub"]))
        if not paciente:
            paciente = Paciente(sanitas_sub=info["sub"], nombre=info.get("name", ""))
            s.add(paciente)
            s.flush()
        cuenta = s.scalar(select(CuentaVinculada).where(CuentaVinculada.paciente_id == paciente.id))
        if not cuenta:
            cuenta = CuentaVinculada(paciente_id=paciente.id)
            s.add(cuenta)
        _store_tokens(ctx, cuenta, tokens)
        cuenta.estado = EstadoVinculo.VIGENTE
        s.add(Consentimiento(paciente_id=paciente.id, version=ctx.settings.consent_version, finalidad=FINALIDAD,
                             aceptado_en=timeutil.parse_iso(session.pop("consent_at"))))
        # HU18: al revincular, las solicitudes suspendidas vuelven a estar activas con su antigüedad original.
        for sol in s.scalars(select(SolicitudEspera).where(SolicitudEspera.paciente_id == paciente.id,
                                                           SolicitudEspera.estado == EstadoSolicitud.SUSPENDIDA)):
            if paciente.puede_recibir_whatsapp:
                sol.estado = EstadoSolicitud.ACTIVA
        audit(s, "paciente", "CUENTA_VINCULADA", paciente.id, scopes=tokens.scope)
        return paciente.id


def _store_tokens(ctx: AppContext, cuenta: CuentaVinculada, tokens) -> None:
    cuenta.access_token_cifrado = ctx.cipher.encrypt(tokens.access_token)
    cuenta.access_expira_en = timeutil.now() + timedelta(seconds=tokens.expires_in)
    if tokens.refresh_token:
        cuenta.refresh_token_cifrado = ctx.cipher.encrypt(tokens.refresh_token)
    cuenta.scopes = tokens.scope


def get_access_token(ctx: AppContext, s: Session, paciente: Paciente) -> str:
    """Devuelve un access token vigente; lo renueva con el refresh token si es necesario.

    Si Sanitas rechaza la renovación (token vencido o revocado), la cuenta queda inválida, las solicitudes se suspenden
    y se pide al paciente revincular su cuenta por WhatsApp (HU18 / riesgo 6).
    """
    cuenta = paciente.cuenta
    if not cuenta or cuenta.estado != EstadoVinculo.VIGENTE:
        raise SanitasAuthError("Cuenta no vinculada")
    if cuenta.access_expira_en and cuenta.access_expira_en > timeutil.now() + timedelta(seconds=30):
        return ctx.cipher.decrypt(cuenta.access_token_cifrado)
    try:
        if not cuenta.refresh_token_cifrado:
            raise SanitasAuthError("Sin refresh token")
        tokens = ctx.sanitas.refresh(ctx.cipher.decrypt(cuenta.refresh_token_cifrado))
    except SanitasAuthError:
        invalidate_account(ctx, s, paciente)
        raise
    _store_tokens(ctx, cuenta, tokens)
    s.flush()
    return tokens.access_token


def invalidate_account(ctx: AppContext, s: Session, paciente: Paciente) -> None:
    cuenta = paciente.cuenta
    if cuenta.estado != EstadoVinculo.VIGENTE:
        return
    cuenta.estado = EstadoVinculo.INVALIDA
    cuenta.access_token_cifrado = cuenta.refresh_token_cifrado = None
    for sol in s.scalars(select(SolicitudEspera).where(SolicitudEspera.paciente_id == paciente.id,
                                                       SolicitudEspera.estado.in_([EstadoSolicitud.ACTIVA,
                                                                                   EstadoSolicitud.CON_OFERTA]))):
        _cancel_active_offers(ctx, s, sol)
        sol.estado = EstadoSolicitud.SUSPENDIDA
    audit(s, "sistema", "CUENTA_INVALIDA", paciente.id)
    send_safe(ctx, s, paciente, "sanibot_aviso", [
        "Tu autorización con Sanitas venció o fue revocada. Abre la app de SaniBot y vuelve a vincular tu cuenta; "
        "conservarás tu lugar en la lista de espera."])


def _cancel_active_offers(ctx: AppContext, s: Session, sol: SolicitudEspera) -> None:
    for of in s.scalars(select(OfertaCupo).where(OfertaCupo.solicitud_id == sol.id,
                                                 OfertaCupo.estado.in_(OFERTA_ACTIVA))):
        if of.hold_id:
            ctx.sanitas.release_slot(of.cupo_id, of.hold_id)
        of.estado = EstadoOferta.CANCELADA


def unlink(ctx: AppContext, paciente_id: str) -> None:
    """HU16: desvincula la cuenta, revoca la autorización en Sanitas y cancela las solicitudes."""
    with ctx.db.session() as s:
        paciente = s.get(Paciente, paciente_id)
        cuenta = paciente.cuenta
        if cuenta and cuenta.refresh_token_cifrado:
            ctx.sanitas.revoke(ctx.cipher.decrypt(cuenta.refresh_token_cifrado))
        if cuenta:
            cuenta.access_token_cifrado = cuenta.refresh_token_cifrado = None
            cuenta.estado = EstadoVinculo.REVOCADA
        for sol in s.scalars(select(SolicitudEspera).where(SolicitudEspera.paciente_id == paciente_id)):
            if sol.estado in (EstadoSolicitud.ACTIVA, EstadoSolicitud.CON_OFERTA, EstadoSolicitud.SUSPENDIDA):
                _cancel_active_offers(ctx, s, sol)
                sol.estado = EstadoSolicitud.CANCELADA
        for c in s.scalars(select(Consentimiento).where(Consentimiento.paciente_id == paciente_id,
                                                        Consentimiento.revocado_en.is_(None))):
            c.revocado_en = timeutil.now()
        audit(s, "paciente", "CUENTA_DESVINCULADA", paciente_id)


# ------------------------------------------------------------------ WhatsApp (HU08)
CODIGO_EN_MENSAJE = re.compile(r"\bSANIBOT\W*(\d{6})\b", re.IGNORECASE)


def start_whatsapp_verification(ctx: AppContext, paciente_id: str, phone_raw: str, opt_in: bool) -> tuple[str, str]:
    """Genera el código que el paciente debe ENVIAR a SaniBot desde su WhatsApp.

    Así se comprueba que el número es suyo sin que SaniBot tenga que iniciar la conversación (Meta solo permite
    plantillas de autenticación a empresas verificadas) y, al escribir el paciente primero, se abre la ventana
    de 24 horas en la que SaniBot puede responderle.
    """
    if not opt_in:
        raise AccountError("Para usar SaniBot debe autorizar los mensajes de WhatsApp: es el único canal de ofertas.")
    phone = normalize_phone(phone_raw)
    code = generate_code()
    with ctx.db.session() as s:
        for anterior in s.scalars(select(VerificacionWhatsApp).where(VerificacionWhatsApp.paciente_id == paciente_id,
                                                                     VerificacionWhatsApp.usado.is_(False))):
            anterior.usado = True  # solo vale el código más reciente
        s.add(VerificacionWhatsApp(paciente_id=paciente_id, telefono=phone,
                                   codigo_hash=hash_code(code, ctx.settings.secret_key),
                                   expira_en=timeutil.now() + timedelta(minutes=ctx.settings.verification_code_minutes)))
        audit(s, "paciente", "WHATSAPP_VERIFICACION_INICIADA", paciente_id, telefono_final=phone[-4:])
    return phone, code


def confirm_whatsapp_message(ctx: AppContext, from_phone: str, text: str) -> str | None:
    """Procesa un mensaje entrante «SANIBOT 123456». Devuelve el resultado, o None si no es un código."""
    m = CODIGO_EN_MENSAJE.search(text or "")
    if not m:
        return None
    with ctx.db.session() as s:
        v = s.scalar(select(VerificacionWhatsApp).where(VerificacionWhatsApp.telefono == from_phone,
                                                        VerificacionWhatsApp.usado.is_(False))
                     .order_by(VerificacionWhatsApp.expira_en.desc()))
        if not v:
            return "SIN_VERIFICACION"  # el número que escribe no es el que el paciente registró en la página
        if v.expira_en < timeutil.now() or v.intentos >= ctx.settings.verification_max_attempts:
            return "CODIGO_VENCIDO"
        v.intentos += 1
        if not secrets.compare_digest(v.codigo_hash, hash_code(m.group(1), ctx.settings.secret_key)):
            return "CODIGO_INCORRECTO"
        _mark_verified(ctx, s, v)
        return "VERIFICADO"


def whatsapp_verified(ctx: AppContext, paciente_id: str) -> bool:
    with ctx.db.session() as s:
        p = s.get(Paciente, paciente_id)
        return bool(p and p.puede_recibir_whatsapp)


def _mark_verified(ctx: AppContext, s, v: VerificacionWhatsApp) -> None:
    v.usado = True
    paciente_id = v.paciente_id
    paciente = s.get(Paciente, paciente_id)
    now = timeutil.now()
    paciente.telefono_whatsapp, paciente.whatsapp_verificado_en = v.telefono, now
    paciente.opt_in_whatsapp_en, paciente.opt_out_whatsapp_en = now, None
    audit(s, "paciente", "WHATSAPP_VERIFICADO", paciente_id, telefono_final=v.telefono[-4:])
    nombre = (paciente.nombre or "").split(" ")[0] or "paciente"
    # Si la bienvenida falla, el número igual queda verificado: el paciente ya demostró que es suyo.
    send_safe(ctx, s, paciente, "sanibot_bienvenida", [nombre])


MENSAJE_BAJA = ("Listo, te diste de baja de SaniBot. No te enviaremos más mensajes y tus búsquedas de citas quedaron "
                "pausadas; tus citas en Sanitas no cambian. Si quieres volver a usar SaniBot, actívalo de nuevo desde su página.")
MENSAJE_YA_DE_BAJA = ("Ya estás dado de baja de SaniBot y no recibirás mensajes. Si quieres volver a usarlo, actívalo de "
                      "nuevo desde su página.")


def opt_out(ctx: AppContext, phone: str) -> str | None:
    """El paciente escribió BAJA: deja de recibir mensajes, sus solicitudes se suspenden y se le confirma por WhatsApp.

    Devuelve "BAJA", "YA_DE_BAJA" o None si el número no pertenece a ningún paciente (en ese caso no se responde).
    """
    with ctx.db.session() as s:
        paciente = s.scalar(select(Paciente).where(Paciente.telefono_whatsapp == phone))
        if not paciente:
            return None
        if paciente.opt_out_whatsapp_en:
            resultado, texto = "YA_DE_BAJA", MENSAJE_YA_DE_BAJA
        else:
            paciente.opt_out_whatsapp_en = timeutil.now()
            for sol in s.scalars(select(SolicitudEspera).where(SolicitudEspera.paciente_id == paciente.id,
                                                               SolicitudEspera.estado.in_([EstadoSolicitud.ACTIVA,
                                                                                           EstadoSolicitud.CON_OFERTA]))):
                _cancel_active_offers(ctx, s, sol)
                sol.estado = EstadoSolicitud.SUSPENDIDA
            audit(s, "paciente", "WHATSAPP_BAJA", paciente.id)
            resultado, texto = "BAJA", MENSAJE_BAJA
        # El paciente acaba de escribir, así que la ventana de 24 horas está abierta: se responde sin plantilla.
        # La confirmación es la única respuesta permitida después de la baja; si falla, la baja igual queda hecha.
        try:
            wa_id = ctx.whatsapp.send_session(phone, texto)
            s.add(Notificacion(paciente_id=paciente.id, plantilla="confirmacion_baja", wa_message_id=wa_id))
        except WhatsAppError as exc:
            log.warning("No se pudo confirmar la baja al paciente %s: %s", paciente.id, exc)
        return resultado
