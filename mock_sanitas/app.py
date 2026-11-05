"""Sanitas simulada para el entorno académico.

Incluye: un proveedor OAuth 2.0 de prueba (con PKCE), una API de agendas con recursos tipo FHIR (Slot, Appointment),
un «portal» para simular lo que el paciente hace por fuera de SaniBot, los correos que Sanitas envía por su cuenta
y el envío de eventos firmados (webhooks) a SaniBot.
"""

import base64
import hashlib
import html
import json
import os
import secrets
import smtplib
import threading
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import httpx
import jwt
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from mock_sanitas.state import BOGOTA, ESPECIALIDADES, PROFESIONALES, SanitasState, seed
from sanibot.security.signatures import sign
from simulator.engine import DEMO, Simulador

DEMO_PASSWORD = "sanitas123"

# Pantalla de inicio de sesión del proveedor simulado. Se identifica siempre como entorno de pruebas.
LOGIN_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Iniciar sesión · Sanitas (entorno de pruebas)</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:flex;flex-direction:column;background:#f4f7fb;color:#14213a;
font:17px/1.6 "Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif}}
.head{{background:#0b2e59;color:#fff}}.head div,.main{{max-width:1000px;margin:0 auto;padding:0 24px}}
.head div{{display:flex;align-items:center;gap:16px;min-height:64px}}.head b{{font-size:1.15rem}}
.env{{margin-left:auto;background:#fff4d6;color:#6b4800;border-radius:4px;padding:3px 10px;font-size:.8rem;font-weight:700}}
.main{{flex:1;display:grid;grid-template-columns:1fr 420px;gap:56px;align-items:start;padding-top:48px;padding-bottom:48px}}
h1{{font-size:1.7rem;line-height:1.2;color:#0b2e59;margin:0 0 .4em}}p{{color:#52607a;margin:.5em 0}}
ul{{margin:16px 0;padding:0;list-style:none}}li{{padding:10px 0 10px 30px;border-bottom:1px solid #d6deea;position:relative}}
li::before{{content:"✓";position:absolute;left:4px;color:#1458b8;font-weight:700}}
form{{background:#fff;border:1px solid #d6deea;border-radius:10px;padding:28px}}
form h2{{margin:0 0 4px;font-size:1.2rem;color:#0b2e59}}
label{{display:block;font-weight:600;margin:16px 0 6px}}
select,input{{width:100%;font:inherit;padding:11px 13px;border:1.5px solid #aebbd0;border-radius:6px;background:#fff}}
.btns{{display:grid;gap:10px;margin-top:22px}}button{{font:inherit;font-weight:600;border:0;border-radius:6px;padding:12px;cursor:pointer;min-height:48px}}
.allow{{background:#1458b8;color:#fff}}.allow:hover{{background:#0e4592}}
.deny{{background:#fff;color:#1458b8;border:1.5px solid #1458b8}}
small{{display:block;color:#52607a;margin-top:14px;font-size:.85rem}}
@media (max-width:820px){{.main{{grid-template-columns:1fr;gap:24px;padding-top:28px}}}}
</style></head><body>
<header class="head"><div><b>Sanitas</b><span class="env">Entorno de pruebas · simulado</span></div></header>
<main class="main">
<section><h1>SaniBot solicita acceso a tus citas</h1>
<p><b>SaniBot</b> es un servicio complementario autorizado por la EPS. Si lo autorizas, podrá:</p><ul>{perms}</ul>
<p>No tendrá acceso a tu historia clínica ni a tu contraseña. Puedes retirar el permiso cuando quieras.</p></section>
<form method="post">{hidden}<h2>Inicia sesión</h2>
<label for="u">Usuario</label><select id="u" name="username">{opts}</select>
<label for="p">Contraseña</label><input id="p" type="password" name="password" value="{password}">
<div class="btns"><button class="allow" name="decision" value="allow">Autorizar a SaniBot</button>
<button class="deny" name="decision" value="deny">No autorizar</button></div>
<small>Contraseña de prueba: {password}. SaniBot nunca ve esta contraseña.</small></form>
</main></body></html>"""


class MockConfig:
    def __init__(self, **overrides):
        env = os.environ.get
        self.client_id = overrides.get("client_id", env("MOCK_CLIENT_ID", "sanibot"))
        self.client_secret = overrides.get("client_secret", env("MOCK_CLIENT_SECRET", "sanibot-secret"))
        self.redirect_uris = overrides.get("redirect_uris", env("MOCK_REDIRECT_URIS", "http://localhost:8000/auth/callback")).split(",")
        self.jwt_secret = overrides.get("jwt_secret", env("MOCK_JWT_SECRET", "secreto-jwt-sanitas-simulada-cambiar-0123456789"))
        self.service_key = overrides.get("service_key", env("MOCK_SERVICE_KEY", "clave-servicio-sanibot"))
        self.webhook_url = overrides.get("webhook_url", env("MOCK_SANIBOT_WEBHOOK_URL", "http://sanibot:8000/webhooks/sanitas"))
        self.webhook_secret = overrides.get("webhook_secret", env("MOCK_WEBHOOK_SECRET", "secreto-webhook-sanitas"))
        self.auth_mode = overrides.get("auth_mode", env("MOCK_AUTH_MODE", "builtin"))  # builtin | keycloak
        self.keycloak_jwks_url = overrides.get("keycloak_jwks_url", env("MOCK_KEYCLOAK_JWKS_URL", ""))
        self.smtp_host = overrides.get("smtp_host", env("MOCK_SMTP_HOST", ""))
        self.smtp_port = int(overrides.get("smtp_port", env("MOCK_SMTP_PORT", "1025")))
        self.seed = overrides.get("seed", env("MOCK_SEED", "true").lower() == "true")
        self.dispatch = overrides.get("dispatch", env("MOCK_DISPATCH_WEBHOOKS", "true").lower() == "true")
        self.access_ttl = int(overrides.get("access_ttl", env("MOCK_ACCESS_TTL_SECONDS", "300")))
        # > 0 enciende el simulador de agenda al arrancar (eventos por hora)
        self.simulador = float(overrides.get("simulador", env("MOCK_SIMULADOR_EVENTOS_POR_HORA", "0")) or 0)


class BookIn(BaseModel):
    slot: str
    hold_id: str | None = None


class HoldIn(BaseModel):
    minutes: int = 30


class ReleaseIn(BaseModel):
    hold_id: str


def deliver(cfg: MockConfig, event: dict, client: httpx.Client | None = None) -> bool:
    body = json.dumps(event).encode()
    headers = {"Content-Type": "application/json", "X-Sanitas-Signature": sign(body, cfg.webhook_secret)}
    c = client or httpx.Client(timeout=10)
    for attempt in range(3):
        try:
            r = c.post(cfg.webhook_url, content=body, headers=headers)
            if r.status_code < 500:
                return r.status_code < 300
        except httpx.HTTPError:
            pass
        time.sleep(0.5 * (2 ** attempt))
    return False


def create_app(state: SanitasState | None = None, cfg: MockConfig | None = None) -> FastAPI:
    cfg = cfg or MockConfig()
    state = state or SanitasState()
    if cfg.seed and not state.slots:
        seed(state)
    if cfg.smtp_host:
        def _smtp(mail):
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = "notificaciones@sanitas.example", mail.para, mail.asunto
            msg.set_content(mail.cuerpo)
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=5) as s:
                s.send_message(msg)
        state.mailer = _smtp

    app = FastAPI(title="Sanitas simulada", version="0.1.0")
    app.state.sanitas = state
    app.state.cfg = cfg
    simulador = Simulador(state)
    app.state.simulador = simulador
    if cfg.simulador > 0:
        simulador.iniciar(cfg.simulador)
    jwks_client = jwt.PyJWKClient(cfg.keycloak_jwks_url) if cfg.auth_mode == "keycloak" and cfg.keycloak_jwks_url else None

    if cfg.dispatch:
        def _loop():
            while True:
                for ev in state.take_outbox():
                    deliver(cfg, ev)
                time.sleep(1)
        threading.Thread(target=_loop, daemon=True).start()

    # ------------------------------------------------------------ autenticación
    def issue_access(username: str, scope: str) -> str:
        p = state.patients[username]
        claims = {"iss": "sanitas-simulada", "sub": p.sub, "preferred_username": username, "scope": scope,
                  "exp": datetime.now(UTC) + timedelta(seconds=cfg.access_ttl), "iat": datetime.now(UTC)}
        return jwt.encode(claims, cfg.jwt_secret, algorithm="HS256")

    def current_patient(authorization: str | None = Header(None)):
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Falta el token de acceso")
        token = authorization.split(" ", 1)[1]
        try:
            if jwks_client:
                key = jwks_client.get_signing_key_from_jwt(token).key
                claims = jwt.decode(token, key, algorithms=["RS256"], options={"verify_aud": False})
            else:
                claims = jwt.decode(token, cfg.jwt_secret, algorithms=["HS256"])
        except jwt.PyJWTError as exc:
            raise HTTPException(401, "Token inválido o vencido") from exc
        username = claims.get("preferred_username")
        if username not in state.patients or username in state.revoked_users:
            raise HTTPException(401, "Autorización revocada")
        return state.patients[username], set(str(claims.get("scope", "")).split())

    def require_scope(scope: str):
        def dep(ctx=Depends(current_patient)):
            patient, scopes = ctx
            if scope not in scopes:
                raise HTTPException(403, f"Falta el permiso {scope}")
            return patient
        return dep

    def service(x_api_key: str | None = Header(None)):
        if not x_api_key or not secrets.compare_digest(x_api_key, cfg.service_key):
            raise HTTPException(401, "Clave de servicio inválida")

    # ------------------------------------------------------------ OAuth 2.0 de prueba (código de autorización + PKCE)
    @app.get("/oauth/authorize", response_class=HTMLResponse)
    def authorize_form(client_id: str, redirect_uri: str, state_: str = "", code_challenge: str = "",
                       code_challenge_method: str = "", scope: str = "", request: Request = None):
        st = request.query_params.get("state", state_)
        if client_id != cfg.client_id or redirect_uri not in cfg.redirect_uris:
            raise HTTPException(400, "Cliente o URL de retorno no autorizados")
        if code_challenge_method != "S256" or not code_challenge:
            raise HTTPException(400, "PKCE (S256) es obligatorio")
        opts = "".join(f'<option value="{u}">{html.escape(p.nombre)} ({u})</option>'
                       for u, p in list(state.patients.items())[:3])
        hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in
                         {"client_id": client_id, "redirect_uri": redirect_uri, "state": st,
                          "code_challenge": code_challenge, "scope": scope}.items())
        nombres = {"citas.leer": "Consultar tus citas", "citas.reprogramar": "Reprogramar tus citas cuando tú lo autorices"}
        perms = "".join(f"<li>{html.escape(nombres.get(s, s))}</li>" for s in scope.split() if s.startswith("citas."))
        return LOGIN_HTML.format(perms=perms, hidden=hidden, opts=opts, password=DEMO_PASSWORD)

    @app.post("/oauth/authorize")
    def authorize_submit(client_id: str = Form(...), redirect_uri: str = Form(...), state: str = Form(""),
                         code_challenge: str = Form(...), scope: str = Form(""), username: str = Form(...),
                         password: str = Form(...), decision: str = Form("allow")):
        if client_id != cfg.client_id or redirect_uri not in cfg.redirect_uris:
            raise HTTPException(400, "Cliente no autorizado")
        sep = "&" if "?" in redirect_uri else "?"
        if decision != "allow":
            return RedirectResponse(f"{redirect_uri}{sep}error=access_denied&state={state}", status_code=303)
        if username not in app.state.sanitas.patients or password != DEMO_PASSWORD:
            raise HTTPException(401, "Credenciales inválidas")
        code = secrets.token_urlsafe(24)
        app.state.sanitas.auth_codes[code] = {"username": username, "scope": scope, "challenge": code_challenge,
                                              "redirect_uri": redirect_uri, "exp": time.time() + 300}
        app.state.sanitas.revoked_users.discard(username)
        return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=303)

    @app.post("/oauth/token")
    def token(grant_type: str = Form(...), code: str = Form(""), redirect_uri: str = Form(""),
              code_verifier: str = Form(""), refresh_token: str = Form(""), client_id: str = Form(""),
              client_secret: str = Form("")):
        if client_id != cfg.client_id or client_secret != cfg.client_secret:
            return JSONResponse({"error": "invalid_client"}, 401)
        if grant_type == "authorization_code":
            data = state.auth_codes.pop(code, None)
            if not data or data["exp"] < time.time() or data["redirect_uri"] != redirect_uri:
                return JSONResponse({"error": "invalid_grant"}, 400)
            digest = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
            if not secrets.compare_digest(digest, data["challenge"]):
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE inválido"}, 400)
            username, scope = data["username"], data["scope"]
        elif grant_type == "refresh_token":
            data = state.refresh_tokens.pop(refresh_token, None)
            if not data or data["username"] in state.revoked_users:
                return JSONResponse({"error": "invalid_grant"}, 400)
            username, scope = data["username"], data["scope"]
        else:
            return JSONResponse({"error": "unsupported_grant_type"}, 400)
        new_refresh = secrets.token_urlsafe(32)  # rotación del refresh token en cada uso
        state.refresh_tokens[new_refresh] = {"username": username, "scope": scope}
        return {"access_token": issue_access(username, scope), "token_type": "Bearer", "expires_in": cfg.access_ttl,
                "refresh_token": new_refresh, "scope": scope}

    @app.get("/oauth/userinfo")
    def userinfo(ctx=Depends(current_patient)):
        p, _ = ctx
        return {"sub": p.sub, "preferred_username": p.username, "name": p.nombre, "email": p.correo}

    @app.post("/oauth/revoke")
    def revoke(token: str = Form(...), client_id: str = Form(""), client_secret: str = Form("")):
        if client_id != cfg.client_id or client_secret != cfg.client_secret:
            return JSONResponse({"error": "invalid_client"}, 401)
        state.refresh_tokens.pop(token, None)
        return JSONResponse({}, 200)

    # ------------------------------------------------------------ API de agendas (tipo FHIR)
    @app.get("/fhir/Appointment")
    def my_appointments(patient=Depends(require_scope("citas.leer"))):
        with state.lock:
            items = [state.appointment_view(a) for a in state.appointments.values()
                     if a.username == patient.username and a.estado == "booked"]
        items.sort(key=lambda x: x["start"])
        return {"resourceType": "Bundle", "total": len(items), "entry": items}

    @app.get("/fhir/Appointment/{appointment_id}")
    def get_appointment(appointment_id: str, patient=Depends(require_scope("citas.leer"))):
        a = state.appointments.get(appointment_id)
        if not a or a.username != patient.username:
            raise HTTPException(404, "Cita no encontrada")
        return state.appointment_view(a)

    @app.post("/fhir/Appointment", status_code=201)
    def book(body: BookIn, patient=Depends(require_scope("citas.reprogramar"))):
        try:
            a = state.book(patient.username, body.slot, body.hold_id, source="api")
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return state.appointment_view(a)

    @app.post("/fhir/Appointment/{appointment_id}/$cancel")
    def cancel(appointment_id: str, patient=Depends(require_scope("citas.reprogramar"))):
        a = state.appointments.get(appointment_id)
        if not a or a.username != patient.username:
            raise HTTPException(404, "Cita no encontrada")
        return state.appointment_view(state.cancel(appointment_id, source="api"))

    @app.get("/fhir/Slot/{slot_id}", dependencies=[Depends(service)])
    def get_slot(slot_id: str):
        s = state.slots.get(slot_id)
        if not s:
            raise HTTPException(404, "Cupo no encontrado")
        return s.to_fhir()

    @app.get("/fhir/Slot", dependencies=[Depends(service)])
    def search_slots(status: str = "free", serviceType: str | None = None, limit: int = 50):  # noqa: N803 - nombre FHIR
        with state.lock:
            res = [s.to_fhir() for s in sorted(state.slots.values(), key=lambda s: s.inicio)
                   if s.to_fhir()["status"] == status and (not serviceType or s.especialidad == serviceType)]
        return {"resourceType": "Bundle", "total": len(res), "entry": res[:limit]}

    @app.post("/fhir/Slot/{slot_id}/$hold", dependencies=[Depends(service)])
    def hold(slot_id: str, body: HoldIn):
        try:
            s = state.hold(slot_id, body.minutes)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"hold_id": s.hold_id, "expires": s.hold_expira.isoformat()}

    @app.post("/fhir/Slot/{slot_id}/$release", dependencies=[Depends(service)])
    def release(slot_id: str, body: ReleaseIn):
        state.release(slot_id, body.hold_id)
        return {"ok": True}

    # ------------------------------------------------------------ portal de Sanitas (lo que ocurre por fuera de SaniBot)
    @app.post("/portal/appointments/{appointment_id}/cancel")
    def portal_cancel(appointment_id: str):
        try:
            return state.appointment_view(state.cancel(appointment_id, source="portal"))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/portal/appointments/{appointment_id}/reschedule")
    def portal_reschedule(appointment_id: str, slot_id: str = Form(...)):
        try:
            return state.appointment_view(state.reschedule(appointment_id, slot_id))
        except (LookupError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/portal/slots")
    def portal_open_slot(especialidad: str = Form(...), dias: int = Form(2), hora: int = Form(10)):
        if especialidad not in PROFESIONALES:
            raise HTTPException(400, "Especialidad desconocida")
        inicio = (datetime.now(BOGOTA) + timedelta(days=dias)).replace(hour=hora, minute=0, second=0, microsecond=0)
        s = state.open_slot(especialidad, PROFESIONALES[especialidad][0], inicio)
        return s.to_fhir()

    @app.post("/portal/revoke/{username}")
    def portal_revoke(username: str):
        state.revoked_users.add(username)
        for t in [t for t, d in state.refresh_tokens.items() if d["username"] == username]:
            state.refresh_tokens.pop(t)
        return {"ok": True}

    @app.get("/portal/emails")
    def portal_emails(para: str | None = None):
        return [{"para": e.para, "asunto": e.asunto, "cuerpo": e.cuerpo, "fecha": e.fecha.isoformat()}
                for e in state.emails if not para or e.para == para]

    @app.get("/portal/appointments")
    def portal_appointments(username: str | None = None, especialidad: str | None = None):
        with state.lock:
            res = [state.appointment_view(a) for a in state.appointments.values() if a.estado == "booked"
                   and (not username or a.username == username)
                   and (not especialidad or state.slots[a.slot_id].especialidad == especialidad)]
        return sorted(res, key=lambda x: x["start"])

    @app.get("/portal", response_class=HTMLResponse)
    def portal():
        rows = []
        for u in ("paciente1", "paciente2", "paciente3"):
            p = state.patients.get(u)
            if not p:
                continue
            for a in portal_appointments(username=u):
                d = datetime.fromisoformat(a["start"]).astimezone(BOGOTA)
                rows.append(f"<tr><td>{html.escape(p.nombre)}</td><td>{a['serviceType']}</td><td>{d:%d/%m %H:%M}</td>"
                            f"<td><form method=post action='/portal/appointments/{a['id']}/cancel'>"
                            "<button>Cancelar desde el portal</button></form></td></tr>")
        mails = "".join(f"<li><b>{html.escape(e.asunto)}</b> → {html.escape(e.para)} ({e.fecha.astimezone(BOGOTA):%H:%M:%S})"
                        f"<pre>{html.escape(e.cuerpo)}</pre></li>" for e in reversed(state.emails[-15:]))
        esp = "".join(f"<option>{e}</option>" for e in ESPECIALIDADES)
        return f"""<!doctype html><html lang="es"><meta charset="utf-8"><title>Portal Sanitas (simulado)</title>
<body style="font-family:sans-serif;max-width:900px;margin:24px auto;padding:0 16px"><h1>Portal de Sanitas (simulado)</h1>
<p>Acciones que ocurren por fuera de SaniBot. Cada cambio envía el correo correspondiente y un evento a SaniBot.</p>
<h2>Citas de los pacientes de demostración</h2><table border=1 cellpadding=6>{''.join(rows)}</table>
<h2>Simulador automático</h2><p>Estado: <b>{'encendido' if simulador.activo else 'apagado'}</b>
({simulador.params.eventos_por_hora:g} eventos por hora, {simulador.eventos} generados).</p>
<form method=post action="/portal/simulador/html">
<input name=eventos_por_hora type=number value={simulador.params.eventos_por_hora:g} min=1 max=3600 style="width:6em">
eventos por hora <button name=accion value=iniciar>Encender</button>
<button name=accion value=detener>Apagar</button></form>
<h2>Simular una cancelación de otro paciente</h2><form method=post action="/portal/simulate-cancel">
<select name=especialidad>{esp}</select> <button>Cancelar la cita más próxima de esa especialidad</button></form>
<h2>Abrir un cupo nuevo en la agenda</h2><form method=post action="/portal/slots"><select name=especialidad>{esp}</select>
en <input name=dias type=number value=3 min=1 style="width:4em"> días
a las <input name=hora type=number value=10 min=6 max=18 style="width:4em"> h
<button>Abrir cupo</button></form><h2>Correos enviados por Sanitas</h2><ul>{mails or '<li>Sin correos</li>'}</ul></body></html>"""

    @app.post("/portal/simulador/html")
    def simulador_html(accion: str = Form(...), eventos_por_hora: float = Form(120)):
        simulador_control(accion, eventos_por_hora)
        return RedirectResponse("/portal", status_code=303)

    @app.post("/portal/simulate-cancel")
    def simulate_cancel(especialidad: str = Form(...)):
        candidates = [a for a in portal_appointments(especialidad=especialidad) if a["patient"] not in DEMO]
        if not candidates:
            raise HTTPException(404, "No hay citas para cancelar")
        state.cancel(candidates[0]["id"], source="portal")
        return RedirectResponse("/portal", status_code=303)

    # ------------------------------------------------------------ simulador integrado y resumen para la consola de demo
    @app.get("/portal/simulador")
    def simulador_estado():
        return simulador.estado()

    @app.post("/portal/simulador")
    def simulador_control(accion: str = Form(...), eventos_por_hora: float = Form(120)):
        if accion == "iniciar":
            simulador.iniciar(eventos_por_hora)
        elif accion == "detener":
            simulador.detener()
        elif accion == "paso":
            simulador.paso()
        else:
            raise HTTPException(400, "Acción desconocida")
        return simulador.estado()

    @app.get("/portal/resumen")
    def resumen():
        """Lo que la consola de demostración muestra del lado de Sanitas: citas y correos de los pacientes demo."""
        correos_demo = {state.patients[u].correo for u in DEMO if u in state.patients}
        pacientes = []
        for u in sorted(DEMO):
            p = state.patients.get(u)
            if p:
                pacientes.append({"usuario": u, "nombre": p.nombre, "correo": p.correo,
                                  "citas": portal_appointments(username=u)})
        correos = [{"para": e.para, "asunto": e.asunto, "cuerpo": e.cuerpo, "fecha": e.fecha.isoformat()}
                   for e in reversed(state.emails) if e.para in correos_demo][:20]
        return {"pacientes": pacientes, "correos": correos, "simulador": simulador.estado()}

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/portal")

    @app.get("/health")
    def health():
        return {"status": "ok", "cupos": len(state.slots), "citas": len(state.appointments)}

    return app


app = create_app() if os.environ.get("MOCK_AUTOCREATE", "true").lower() == "true" else None
