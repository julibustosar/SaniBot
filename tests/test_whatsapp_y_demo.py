"""Plantillas de WhatsApp, manejo de errores de Meta, consola de demostración, simulador integrado y lanzador."""

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from mock_sanitas.app import MockConfig
from mock_sanitas.app import create_app as create_mock
from mock_sanitas.state import SanitasState, seed
from sanibot import plantillas
from sanibot.config import Settings
from sanibot.db import Database
from sanibot.integrations.whatsapp import PLANTILLAS, CloudWhatsApp, WhatsAppError
from sanibot.models import Notificacion, Paciente
from simulator.engine import DEMO, Simulador

P1 = "573001110001"
VAR = re.compile(r"\{\{(\d+)\}\}")


# ------------------------------------------------------------------ definiciones de plantillas (reglas de Meta)
@pytest.mark.parametrize("p", [p for p in PLANTILLAS.values() if p.categoria == "UTILITY"], ids=lambda p: p.nombre)
def test_plantillas_de_utilidad_cumplen_las_reglas_de_meta(p):
    d = p.definicion_meta("es")
    assert re.fullmatch(r"[a-z0-9_]+", d["name"]) and d["category"] == "UTILITY"
    cuerpo = d["components"][0]
    variables = [int(x) for x in VAR.findall(cuerpo["text"])]
    assert variables == list(range(1, len(variables) + 1))  # consecutivas desde {{1}}
    assert len(cuerpo["example"]["body_text"][0]) == len(variables)  # un ejemplo por variable
    assert not cuerpo["text"].strip().startswith("{{") and not cuerpo["text"].strip().endswith("}}")
    assert len(cuerpo["text"]) <= 1024
    assert "http" not in cuerpo["text"].lower()  # sin enlaces (riesgo 13)


def test_oferta_tiene_botones_de_respuesta_rapida():
    d = PLANTILLAS["sanibot_oferta_cupo"].definicion_meta("es")
    botones = next(c for c in d["components"] if c["type"] == "BUTTONS")["buttons"]
    assert [(b["type"], b["text"]) for b in botones] == [("QUICK_REPLY", "Aceptar"), ("QUICK_REPLY", "Rechazar")]


def test_plantilla_de_autenticacion_con_boton_copiar_codigo():
    d = PLANTILLAS["sanibot_verificacion"].definicion_meta("es", minutos_codigo=10)
    assert d["category"] == "AUTHENTICATION"
    tipos = {c["type"]: c for c in d["components"]}
    assert tipos["BODY"] == {"type": "BODY", "add_security_recommendation": True}
    assert tipos["FOOTER"]["code_expiration_minutes"] == 10
    assert tipos["BUTTONS"]["buttons"][0]["otp_type"] == "COPY_CODE"


def _meta_falsa(existentes=(), fallar=None):
    enviados = []

    def handler(req: httpx.Request):
        assert req.headers["Authorization"] == "Bearer tok"
        if req.method == "GET":
            return httpx.Response(200, json={"data": [{"name": n, "status": "APPROVED", "category": "UTILITY"}
                                                      for n in existentes]})
        body = json.loads(req.content)
        if body["name"] == fallar:
            return httpx.Response(400, json={"error": {"code": 100, "message": "Invalid parameter",
                                                       "error_data": {"details": "texto no permitido"}}})
        enviados.append(body)
        return httpx.Response(200, json={"id": "1", "status": "PENDING", "category": body["category"]})

    return enviados, httpx.Client(transport=httpx.MockTransport(handler))


def _settings(**kw):
    base = dict(whatsapp_business_account_id="waba", whatsapp_access_token="tok", whatsapp_phone_number_id="pn",
                _env_file=None)
    base.update(kw)
    return Settings(**base)


def test_registrar_envia_solo_las_que_faltan(capsys):
    s = _settings()
    enviados, http = _meta_falsa(existentes=["sanibot_bienvenida"])
    assert plantillas.cmd_registrar(s, None, plantillas.Meta(s, http)) == 0
    nombres = [d["name"] for d in enviados]
    assert "sanibot_bienvenida" not in nombres and len(nombres) == len(PLANTILLAS) - 1
    assert "ya estaba registrada" in capsys.readouterr().out


def test_registrar_reporta_errores_de_meta(capsys):
    s = _settings()
    _, http = _meta_falsa(fallar="sanibot_aviso")
    assert plantillas.cmd_registrar(s, None, plantillas.Meta(s, http)) == 1
    assert "texto no permitido" in capsys.readouterr().out


def test_estado_indica_cuando_cambiar_a_modo_cloud(capsys):
    s = _settings()
    _, http = _meta_falsa(existentes=list(PLANTILLAS))
    plantillas.cmd_estado(s, None, plantillas.Meta(s, http))
    out = capsys.readouterr().out
    assert f"{len(PLANTILLAS)} de {len(PLANTILLAS)} aprobadas" in out and "SANIBOT_WHATSAPP_MODE=cloud" in out


def test_sin_waba_id_el_comando_explica_que_falta():
    with pytest.raises(SystemExit, match="BUSINESS_ACCOUNT_ID"):
        plantillas.Meta(_settings(whatsapp_business_account_id=""))


# ------------------------------------------------------------------ envío real y errores de Meta
def test_cloud_envia_codigo_en_cuerpo_y_boton():
    capturado = {}

    def handler(req):
        capturado.update(json.loads(req.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    wa = CloudWhatsApp(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    assert wa.send_template(P1, "sanibot_verificacion", ["123456"]) == "wamid.X"
    comps = capturado["template"]["components"]
    assert comps[0]["parameters"][0]["text"] == "123456"
    assert comps[1] == {"type": "button", "sub_type": "url", "index": "0",
                        "parameters": [{"type": "text", "text": "123456"}]}


def test_numero_no_permitido_da_un_mensaje_claro():
    def handler(req):
        return httpx.Response(400, json={"error": {"code": 131030, "message": "Recipient phone number not in allowed list"}})

    wa = CloudWhatsApp(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(WhatsAppError) as exc:
        wa.send_template(P1, "sanibot_bienvenida", ["Ana"])
    assert exc.value.code == 131030 and "destinatarios permitidos" in exc.value.user_message


def test_la_web_muestra_el_error_de_whatsapp_sin_romperse(h):
    h.link("paciente1", verify_whatsapp=False)
    b = h.browser("paciente1")
    page = b.get("/whatsapp")
    h.wa.fail_next = 131030
    r = b.post("/whatsapp", data={"csrf": h.csrf(page.text), "telefono": "3001110001", "acepto": "si"})
    assert r.status_code == 502 and "destinatarios permitidos" in r.text


def test_si_falla_la_bienvenida_el_numero_igual_queda_verificado(h):
    h.link("paciente1", verify_whatsapp=False)
    b = h.browser("paciente1")
    page = b.get("/whatsapp")
    r = b.post("/whatsapp", data={"csrf": h.csrf(page.text), "telefono": "3001110001", "acepto": "si"})
    codigo = h.wa.outbox[-1].params[0]
    h.wa.fail_next = True  # la bienvenida falla
    r = b.post("/whatsapp/verificar", data={"csrf": h.csrf(r.text), "codigo": codigo}, follow_redirects=False)
    assert r.status_code == 303
    with h.ctx.db.session() as s:
        assert s.scalar(select(Paciente)).puede_recibir_whatsapp


def test_estado_fallido_de_meta_queda_registrado(h):
    h.link("paciente1", phone=P1)
    with h.ctx.db.session() as s:
        wa_id = s.scalar(select(Notificacion).where(Notificacion.plantilla == "sanibot_bienvenida")).wa_message_id
    body = {"entry": [{"changes": [{"value": {"statuses": [{"id": wa_id, "status": "failed",
                                                             "errors": [{"code": 131030}]}]}}]}]}
    h.client.post("/webhooks/whatsapp", json=body)
    with h.ctx.db.session() as s:
        n = s.scalar(select(Notificacion).where(Notificacion.wa_message_id == wa_id))
        assert n.estado_entrega == "FAILED" and "destinatarios permitidos" in n.detalle_error


# ------------------------------------------------------------------ consola de demostración
def test_consola_demo_muestra_chat_y_permite_responder(h):
    cita = h.appointment("paciente1", days=30)
    otra = h.appointment("otra", days=10)
    h.link("paciente1", phone=P1)
    h.request("paciente1", cita_id=cita.id)
    h.state.cancel(otra.id, source="portal")
    h.flush()
    b = h.browser("paciente1")
    page = b.get("/demo")
    assert page.status_code == 200 and "Consola de demostración" in page.text
    estado = b.get("/demo/estado").json()
    assert estado["modo"] == "console" and estado["busquedas_activas"] == 1
    oferta = next(m for m in estado["chat"] if m["plantilla"] == "sanibot_oferta_cupo")
    csrf = re.search(r'const CSRF = "([^"]+)"', page.text).group(1)
    r = b.post("/demo/whatsapp", data={"csrf": csrf, "telefono": P1,
                                       "boton": oferta["botones"][0]["payload"]})
    assert r.json()["resultados"] == ["REPROGRAMADA"]
    chat = b.get("/demo/estado").json()["chat"]
    assert any(m.get("propio") and m["texto"] == "Aceptar" for m in chat)  # la respuesta se ve en el chat
    assert chat[-1]["plantilla"] == "sanibot_confirmacion"


def test_consola_demo_exige_csrf(h):
    r = h.client.post("/demo/whatsapp", data={"csrf": "x", "telefono": P1, "texto": "BAJA"})
    assert r.status_code == 400


def test_consola_demo_no_existe_fuera_de_local(make_harness):
    h = make_harness(demo_enabled=False)
    assert h.client.get("/demo").status_code == 404
    assert not Settings(base_url="https://sanibot.example.org", _env_file=None).demo_active


def test_consola_demo_en_modo_cloud_muestra_estados_de_entrega(h, monkeypatch):
    h.link("paciente1", phone=P1)
    monkeypatch.setattr(h.ctx, "whatsapp", object())  # ya no es la consola: se comporta como modo cloud
    estado = h.client.get("/demo/estado").json()
    assert estado["modo"] == "cloud"
    assert [m["plantilla"] for m in estado["chat"]] == ["sanibot_verificacion", "sanibot_bienvenida"]
    assert all(m["estado"] == "ENVIADA" for m in estado["chat"])
    assert all("123456" not in m["texto"] for m in estado["chat"])  # el código nunca se guarda


# ------------------------------------------------------------------ simulador integrado
@pytest.fixture
def sanitas():
    st = SanitasState()
    seed(st)
    return st


def test_simulador_no_toca_las_citas_de_los_pacientes_demo(sanitas):
    demo_antes = {a.id for a in sanitas.appointments.values() if a.username in DEMO}
    sim = Simulador(sanitas, semilla=1)
    eventos = [sim.paso() for _ in range(60)]
    assert sum(1 for e in eventos if e) >= 50
    assert {e["tipo"] for e in eventos if e} >= {"cancelacion", "apertura"}
    assert all(sanitas.appointments[i].estado == "booked" for i in demo_antes)
    assert any(ev["type"] == "slot.available" for ev in sanitas.outbox)  # SaniBot recibe los eventos


def test_simulador_se_enciende_y_apaga_desde_el_portal(sanitas):
    mock = TestClient(create_mock(sanitas, MockConfig(seed=False, dispatch=False)))
    r = mock.post("/portal/simulador", data={"accion": "iniciar", "eventos_por_hora": 3600})
    assert r.json()["activo"] and r.json()["eventos_por_hora"] == 3600
    assert mock.post("/portal/simulador", data={"accion": "paso"}).json()["eventos"] >= 1
    mock.post("/portal/simulador", data={"accion": "detener"})
    mock.app.state.simulador._thread.join(timeout=3)
    assert not mock.get("/portal/simulador").json()["activo"]
    resumen = mock.get("/portal/resumen").json()
    assert {p["usuario"] for p in resumen["pacientes"]} == DEMO
    assert all(c["para"].split("@")[0] in {"ana.gomez", "luis.perez", "maria.rodriguez"} for c in resumen["correos"])


# ------------------------------------------------------------------ base de datos y lanzador
def test_columnas_nuevas_se_agregan_a_una_base_existente(tmp_path):
    url = f"sqlite:///{tmp_path}/vieja.db"
    db = Database(url)
    db.create_all()
    with db.engine.begin() as c:
        c.execute(text("ALTER TABLE notificacion DROP COLUMN detalle_error"))
    Database(url).create_all()
    cols = [r[1] for r in create_engine(url).connect().execute(text("PRAGMA table_info(notificacion)"))]
    assert "detalle_error" in cols


def test_lanzador_sincroniza_los_secretos_de_la_sanitas_simulada():
    import iniciar

    env = iniciar.entorno({"SANIBOT_OAUTH_CLIENT_SECRET": "cs1", "SANIBOT_SANITAS_WEBHOOK_SECRET": "wh1"}, 120)
    assert env["MOCK_CLIENT_SECRET"] == "cs1" and env["MOCK_WEBHOOK_SECRET"] == "wh1"
    assert env["MOCK_SANIBOT_WEBHOOK_URL"].endswith(":8000/webhooks/sanitas")
    assert env["MOCK_SIMULADOR_EVENTOS_POR_HORA"] == "120"
    assert env["MOCK_REDIRECT_URIS"] == env["SANIBOT_BASE_URL"] + "/auth/callback"
