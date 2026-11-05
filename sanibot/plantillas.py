"""Registro y consulta de las plantillas de WhatsApp de SaniBot en la cuenta de WhatsApp Business (Meta).

Uso (desde la carpeta del proyecto, con el entorno virtual activo y el .env configurado):

    python -m sanibot.plantillas registrar     # envía a revisión las plantillas que falten
    python -m sanibot.plantillas estado        # muestra si cada plantilla está aprobada, pendiente o rechazada
    python -m sanibot.plantillas ver           # imprime lo que se enviaría a Meta, sin enviarlo
    python -m sanibot.plantillas probar 3001234567   # envía la bienvenida a un número de prueba
    python -m sanibot.plantillas suscribir     # suscribe la app a la cuenta (si el webhook no recibe mensajes)

Requiere en el .env: SANIBOT_WHATSAPP_BUSINESS_ACCOUNT_ID y SANIBOT_WHATSAPP_ACCESS_TOKEN
(y SANIBOT_WHATSAPP_PHONE_NUMBER_ID para «probar»). El token nunca se imprime.
"""

import argparse
import json
import sys

import httpx

from sanibot.config import Settings
from sanibot.integrations.whatsapp import PLANTILLAS, CloudWhatsApp, WhatsAppError, error_from_response, normalize_phone

ESTADOS = {"APPROVED": "Aprobada ✅", "PENDING": "En revisión ⏳", "REJECTED": "Rechazada ❌",
           "PAUSED": "Pausada ⚠️", "DISABLED": "Deshabilitada ❌", "IN_APPEAL": "En apelación"}


class Meta:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        faltan = [n for n, v in (("SANIBOT_WHATSAPP_BUSINESS_ACCOUNT_ID", settings.whatsapp_business_account_id),
                                 ("SANIBOT_WHATSAPP_ACCESS_TOKEN", settings.whatsapp_access_token)) if not v]
        if faltan:
            raise SystemExit(f"Faltan en el .env: {', '.join(faltan)}")
        self.s = settings
        self.http = http or httpx.Client(timeout=20)
        self.base = f"{settings.whatsapp_api_url}/{settings.whatsapp_api_version}/{settings.whatsapp_business_account_id}"
        self.headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}

    def existentes(self) -> dict[str, dict]:
        r = self.http.get(f"{self.base}/message_templates", headers=self.headers,
                          params={"fields": "name,status,category,language,rejected_reason", "limit": 200})
        if r.status_code >= 400:
            raise error_from_response(r)
        return {(t["name"]): t for t in r.json().get("data", []) if t["name"] in PLANTILLAS}

    def suscribir(self) -> dict:
        r = self.http.post(f"{self.base}/subscribed_apps", headers=self.headers)
        if r.status_code >= 400:
            raise error_from_response(r)
        return r.json()

    def crear(self, definicion: dict) -> dict:
        r = self.http.post(f"{self.base}/message_templates", headers=self.headers, json=definicion)
        if r.status_code >= 400:
            raise error_from_response(r)
        return r.json()


def definiciones(settings: Settings) -> list[dict]:
    return [p.definicion_meta(settings.whatsapp_template_language, settings.verification_code_minutes)
            for p in PLANTILLAS.values()]


def cmd_ver(settings: Settings, _args) -> int:
    print(json.dumps(definiciones(settings), ensure_ascii=False, indent=2))
    return 0


def cmd_estado(settings: Settings, _args, meta: Meta | None = None) -> int:
    meta = meta or Meta(settings)
    existentes = meta.existentes()
    for nombre, p in PLANTILLAS.items():
        t = existentes.get(nombre)
        if not t:
            print(f"  {nombre:<24} no registrada")
            continue
        motivo = t.get("rejected_reason")
        extra = f" · motivo: {motivo}" if motivo and motivo != "NONE" else ""
        aviso = ""
        if t.get("category") and t["category"] != p.categoria:
            aviso = f" · Meta la reclasificó como {t['category']}"
        print(f"  {nombre:<24} {ESTADOS.get(t.get('status'), t.get('status'))}{extra}{aviso}")
    aprobadas = sum(1 for t in existentes.values() if t.get("status") == "APPROVED")
    print(f"\n{aprobadas} de {len(PLANTILLAS)} aprobadas.", end=" ")
    print("Ya puedes usar SANIBOT_WHATSAPP_MODE=cloud." if aprobadas == len(PLANTILLAS)
          else "Cuando estén todas aprobadas, cambia el .env a SANIBOT_WHATSAPP_MODE=cloud.")
    return 0


def cmd_registrar(settings: Settings, _args, meta: Meta | None = None) -> int:
    meta = meta or Meta(settings)
    existentes = meta.existentes()
    errores = 0
    for d in definiciones(settings):
        if d["name"] in existentes:
            print(f"  {d['name']:<24} ya estaba registrada ({ESTADOS.get(existentes[d['name']].get('status'), '?')})")
            continue
        try:
            res = meta.crear(d)
            print(f"  {d['name']:<24} enviada a revisión → {ESTADOS.get(res.get('status'), res.get('status'))}")
        except WhatsAppError as exc:
            errores += 1
            print(f"  {d['name']:<24} ERROR: {exc}")
    print("\nMeta suele revisar las plantillas en minutos (a veces hasta 24 h). "
          "Consulta el avance con: python -m sanibot.plantillas estado")
    return 1 if errores else 0


def cmd_suscribir(settings: Settings, _args, meta: Meta | None = None) -> int:
    res = (meta or Meta(settings)).suscribir()
    print("App suscrita a la cuenta de WhatsApp Business: los mensajes y estados llegarán al webhook."
          if res.get("success") else f"Respuesta de Meta: {res}")
    return 0


def cmd_probar(settings: Settings, args) -> int:
    try:
        wa = CloudWhatsApp(settings)
        wa.send_template(normalize_phone(args.telefono), "sanibot_bienvenida", ["prueba"])
    except (ValueError, WhatsAppError) as exc:
        print(f"No se envió: {getattr(exc, 'user_message', None) or exc}")
        return 1
    print("Mensaje enviado. Revisa WhatsApp en ese celular.")
    return 0


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sanibot.plantillas", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("registrar", help="envía a revisión las plantillas que falten")
    sub.add_parser("estado", help="muestra el estado de revisión de cada plantilla")
    sub.add_parser("ver", help="imprime las definiciones sin enviarlas")
    sub.add_parser("suscribir", help="suscribe la app a la cuenta de WhatsApp Business (webhook)")
    pr = sub.add_parser("probar", help="envía la bienvenida a un número registrado como destinatario de prueba")
    pr.add_argument("telefono")
    for stream in (sys.stdout, sys.stderr):  # consolas de Windows sin UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = ap.parse_args(argv)
    settings = settings or Settings()
    cmds = {"registrar": cmd_registrar, "estado": cmd_estado, "ver": cmd_ver, "probar": cmd_probar,
            "suscribir": cmd_suscribir}
    try:
        return cmds[args.cmd](settings, args)
    except WhatsAppError as exc:
        print(f"Error de Meta: {exc.user_message if exc.code in (190, 10, 100) else exc}")
        return 1
    except httpx.HTTPError as exc:
        print(f"No hay conexión con Meta: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
