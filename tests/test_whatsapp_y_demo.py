"""Plantillas de WhatsApp, manejo de errores de Meta, simulador integrado y lanzador."""

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
    d = PLANTILLAS["sanibot_oferta"].definicion_meta("es")
    botones = next(c for c in d["components"] if c["type"] == "BUTTONS")["buttons"]
    assert [(b["type"], b["text"]) for b in botones] == [("QUICK_REPLY", "Aceptar"), ("QUICK_REPLY", "Rechazar")]


def test_no_se_usan_plantillas_de_autenticacion():
    # Meta solo permite plantillas de autenticación a empresas verificadas: el paciente envía el código.
    assert all(p.categoria == "UTILITY" for p in PLANTILLAS.values())


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
def test_cloud_envia_la_oferta_con_parametros_y_botones():
    capturado = {}

    def handler(req):
        capturado.update(json.loads(req.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    wa = CloudWhatsApp(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    params = ["martes", "9:00 a. m.", "Teusaquillo", "30"]
    assert wa.send_template(P1, "sanibot_oferta", params,
                            [("Aceptar", "ACEPTAR:1"), ("Rechazar", "RECHAZAR:1")]) == "wamid.X"
    comps = capturado["template"]["components"]
    assert [p["text"] for p in comps[0]["parameters"]] == params
    assert comps[1]["sub_type"] == "quick_reply" and comps[1]["parameters"][0]["payload"] == "ACEPTAR:1"


def test_numero_no_permitido_da_un_mensaje_claro():
    def handler(req):
        return httpx.Response(400, json={"error": {"code": 131030, "message": "Recipient phone number not in allowed list"}})

    wa = CloudWhatsApp(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(WhatsAppError) as exc:
        wa.send_template(P1, "sanibot_bienvenida", ["Ana"])
    assert exc.value.code == 131030 and "destinatarios permitidos" in exc.value.user_message


def _iniciar_verificacion(h, telefono="3001110001"):
    h.link("paciente1", verify_whatsapp=False)
    b = h.browser("paciente1")
    page = b.get("/whatsapp")
    r = b.post("/whatsapp", data={"csrf": h.csrf(page.text), "telefono": telefono, "acepto": "si"})
    assert r.status_code == 200 and not h.wa.outbox  # SaniBot no inicia la conversación
    return b, re.search(r"SANIBOT \d{6}", r.text).group(0)


def _mensaje_entrante(h, desde, texto):
    msg = {"from": desde, "id": "wamid.in", "type": "text", "text": {"body": texto}}
    return h.client.post("/webhooks/whatsapp", json={"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}).json()


def test_la_pagina_muestra_el_enlace_de_whatsapp_con_el_mensaje_listo(make_harness):
    h = make_harness(whatsapp_numero_visible="+1 555 012 3456")
    b, mensaje = _iniciar_verificacion(h)
    r = b.post("/whatsapp", data={"csrf": h.csrf(b.get("/whatsapp").text), "telefono": "3001110001", "acepto": "si"})
    assert "https://wa.me/15550123456?text=SANIBOT%20" in r.text


def test_el_codigo_solo_vale_desde_el_numero_registrado(h):
    b, mensaje = _iniciar_verificacion(h)
    assert _mensaje_entrante(h, "573009999999", mensaje)["resultados"] == ["SIN_VERIFICACION"]
    assert _mensaje_entrante(h, "573001110001", "SANIBOT 000000")["resultados"] == ["CODIGO_INCORRECTO"]
    assert b.get("/whatsapp/estado").json() == {"verificado": False}
    assert _mensaje_entrante(h, "573001110001", "hola, " + mensaje.lower())["resultados"] == ["VERIFICADO"]
    assert b.get("/whatsapp/estado").json() == {"verificado": True}


def test_codigo_vence_tras_varios_intentos(h):
    _, mensaje = _iniciar_verificacion(h)
    for _ in range(h.ctx.settings.verification_max_attempts):
        _mensaje_entrante(h, "573001110001", "SANIBOT 000000")
    assert _mensaje_entrante(h, "573001110001", mensaje)["resultados"] == ["CODIGO_VENCIDO"]


def test_si_falla_la_bienvenida_el_numero_igual_queda_verificado(h):
    _, mensaje = _iniciar_verificacion(h)
    h.wa.fail_next = True  # la bienvenida falla
    assert _mensaje_entrante(h, "573001110001", mensaje)["resultados"] == ["VERIFICADO"]
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


def test_si_la_plantilla_no_esta_aprobada_envia_mensaje_de_sesion_con_botones():
    enviados = []

    def handler(req):
        body = json.loads(req.content)
        enviados.append(body)
        if body["type"] == "template":
            return httpx.Response(404, json={"error": {"code": 132001, "message": "Template name does not exist"}})
        return httpx.Response(200, json={"messages": [{"id": "wamid.S"}]})

    wa = CloudWhatsApp(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    params = ["martes", "9:00 a. m.", "Teusaquillo", "30"]
    assert wa.send_template(P1, "sanibot_oferta", params,
                            [("Aceptar", "ACEPTAR:1"), ("Rechazar", "RECHAZAR:1")]) == "wamid.S"
    sesion = enviados[-1]
    assert sesion["type"] == "interactive"
    assert "martes a las 9:00 a. m., sede Teusaquillo" in sesion["interactive"]["body"]["text"]
    assert [b["reply"]["id"] for b in sesion["interactive"]["action"]["buttons"]] == ["ACEPTAR:1", "RECHAZAR:1"]
    assert wa.send_template(P1, "sanibot_bienvenida", ["Ana"]) == "wamid.S"
    assert enviados[-1]["type"] == "text" and "Hola Ana" in enviados[-1]["text"]["body"]


def test_otros_errores_de_meta_no_se_disfrazan():
    def handler(req):
        return httpx.Response(400, json={"error": {"code": 131030, "message": "not in allowed list"}})

    wa = CloudWhatsApp(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(WhatsAppError):
        wa.send_template(P1, "sanibot_bienvenida", ["Ana"])


def test_respuesta_con_boton_interactivo_acepta_la_oferta():
    from sanibot.integrations.whatsapp import parse_webhook

    boton = {"type": "button_reply", "button_reply": {"id": "ACEPTAR:9", "title": "Aceptar"}}
    mensaje = {"from": P1, "id": "w1", "type": "interactive", "interactive": boton}
    payload = {"entry": [{"changes": [{"value": {"messages": [mensaje]}}]}]}
    msgs, _ = parse_webhook(payload)
    assert msgs == [{"from": P1, "kind": "button", "value": "ACEPTAR:9", "id": "w1"}]
