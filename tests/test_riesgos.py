"""Pruebas de los controles definidos en la matriz de riesgos."""

import json
import threading

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from sanibot import timeutil
from sanibot.integrations.sanitas import SanitasError
from sanibot.models import (
    EstadoOferta,
    EstadoSolicitud,
    EstadoVinculo,
    OfertaCupo,
    Paciente,
    RegistroAuditoria,
    SolicitudEspera,
)
from sanibot.security.signatures import sign
from sanibot.services import matching, worker
from sanibot.services.notifier import PrivacyViolation, check_privacy
from tests.conftest import WEBHOOK_SECRET

P1, P2, P3 = "573001110001", "573001110002", "573001110003"


def _base(h, n=1):
    citas = [h.appointment(f"paciente{i}", days=30 + i) for i in range(1, n + 1)]
    phones = [P1, P2, P3][:n]
    for i, c in enumerate(citas, start=1):
        h.link(f"paciente{i}", phone=phones[i - 1])
        h.request(f"paciente{i}", cita_id=c.id)
    otra = h.appointment("otra", days=10)
    return citas, otra


# ------------------------------------------------------------------ riesgo 5: pérdida de la cita original
def test_si_la_reserva_falla_el_paciente_conserva_su_cita(h, monkeypatch):
    (c1,), otra = _base(h)
    h.state.cancel(otra.id, source="portal")
    h.flush()
    oferta = h.offers_to(P1)[0]

    def fail(*a, **k):
        raise SanitasError("caída simulada")
    monkeypatch.setattr(h.ctx.sanitas, "book", fail)
    res = h.press(P1, oferta.buttons[0][1])
    assert res["resultados"] == ["FALLIDA"]
    assert h.state.appointments[c1.id].estado == "booked"  # cita original intacta
    assert h.state.slots[otra.slot_id].estado == "free"  # el apartado se liberó
    assert "se mantiene" in h.wa.last_to(P1).preview()
    with h.ctx.db.session() as s:
        assert s.scalar(select(SolicitudEspera)).estado == EstadoSolicitud.ACTIVA


def test_si_la_cancelacion_anterior_falla_se_reintenta(h, monkeypatch):
    (c1,), otra = _base(h)
    h.state.cancel(otra.id, source="portal")
    h.flush()
    original_cancel = h.ctx.sanitas.cancel
    calls = {"n": 0}

    def flaky(token, appointment_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SanitasError("timeout")
        return original_cancel(token, appointment_id)
    monkeypatch.setattr(h.ctx.sanitas, "cancel", flaky)
    h.press(P1, h.offers_to(P1)[0].buttons[0][1])
    assert h.state.appointments[c1.id].estado == "booked"  # temporalmente tiene dos citas, nunca cero
    with h.ctx.db.session() as s:
        assert s.scalar(select(OfertaCupo)).cancelacion_anterior_pendiente
    worker.tick(h.ctx)
    assert h.state.appointments[c1.id].estado == "cancelled"


# ------------------------------------------------------------------ riesgo 4: doble asignación
def test_la_base_de_datos_impide_dos_ofertas_activas_para_un_cupo(h):
    (c1,), _ = _base(h)
    with h.ctx.db.session() as s:
        sol = s.scalar(select(SolicitudEspera))
        kw = dict(solicitud_id=sol.id, cupo_id="slot-x", cupo_inicio=timeutil.now(), expira_en=timeutil.now())
        s.add(OfertaCupo(**kw))
    with pytest.raises(IntegrityError):
        with h.ctx.db.session() as s:
            other = SolicitudEspera(paciente_id=sol.paciente_id, tipo=sol.tipo, especialidad="x", sede="x")
            s.add(other)
            s.flush()
            s.add(OfertaCupo(**{**kw, "solicitud_id": other.id}))


def test_eventos_concurrentes_para_el_mismo_cupo_generan_una_sola_oferta(h):
    _base(h, n=3)
    otra = list(a for a in h.state.appointments.values() if a.username == "otra")[0]
    h.state.cancel(otra.id, source="portal")
    h.state.take_outbox()
    errors = []

    def run():
        try:
            matching.offer_slot(h.ctx, otra.slot_id)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with h.ctx.db.session() as s:
        activas = s.scalars(select(OfertaCupo).where(OfertaCupo.estado == EstadoOferta.PENDIENTE)).all()
    assert len(activas) == 1


# ------------------------------------------------------------------ riesgo 3: idempotencia y firma de eventos
def test_evento_duplicado_se_ignora(h):
    _base(h)
    otra = next(a for a in h.state.appointments.values() if a.username == "otra")
    h.state.cancel(otra.id, source="portal")
    events = h.state.take_outbox()
    for ev in events + events:
        body = json.dumps(ev).encode()
        h.client.post("/webhooks/sanitas", content=body, headers={"X-Sanitas-Signature": sign(body, WEBHOOK_SECRET)})
    assert len(h.offers_to(P1)) == 1


def test_webhook_sin_firma_valida_es_rechazado(h):
    body = json.dumps({"id": "evt-1", "type": "slot.available", "slot_id": "x"}).encode()
    assert h.client.post("/webhooks/sanitas", content=body).status_code == 401
    assert h.client.post("/webhooks/sanitas", content=body,
                         headers={"X-Sanitas-Signature": "sha256=deadbeef"}).status_code == 401


def test_evento_que_falla_queda_pendiente_y_se_reintenta(h, monkeypatch):
    _base(h)
    otra = next(a for a in h.state.appointments.values() if a.username == "otra")
    h.state.cancel(otra.id, source="portal")
    real = h.ctx.sanitas.get_slot

    def down(*a, **k):
        raise SanitasError("caída")
    monkeypatch.setattr(h.ctx.sanitas, "get_slot", down)
    h.flush()
    assert h.offers_to(P1) == []
    assert h.client.get("/health").json()["eventos_pendientes"] >= 1
    monkeypatch.setattr(h.ctx.sanitas, "get_slot", real)
    worker.tick(h.ctx)
    assert len(h.offers_to(P1)) == 1


# ------------------------------------------------------------------ riesgo 43: sincronización con el portal (HU41)
def test_cancelar_en_el_portal_cierra_la_busqueda(h):
    (c1,), _ = _base(h)
    h.state.cancel(c1.id, source="portal")
    h.flush()
    with h.ctx.db.session() as s:
        assert s.scalar(select(SolicitudEspera)).estado == EstadoSolicitud.CANCELADA
    assert "cerramos la búsqueda" in h.wa.last_to(P1).preview()


def test_reprogramar_en_el_portal_actualiza_la_busqueda(h):
    (c1,), _ = _base(h)
    nuevo = h.slot(days=20)
    h.state.reschedule(c1.id, nuevo.id)
    h.flush()
    with h.ctx.db.session() as s:
        sol = s.scalar(select(SolicitudEspera))
        assert sol.estado == EstadoSolicitud.ACTIVA
        assert abs((sol.cita_actual_fecha - nuevo.inicio).total_seconds()) < 1
    # un cupo entre la fecha nueva (día 20) y la anterior (día 31) ya no se ofrece
    tarde = h.appointment("otra2", days=25)
    h.state.cancel(tarde.id, source="portal")
    h.flush()
    assert h.offers_to(P1) == []


def test_si_la_cita_cambio_sin_evento_se_verifica_antes_de_ofrecer(h):
    (c1,), otra = _base(h)
    h.state.appointments[c1.id].estado = "cancelled"  # cambio sin evento (p. ej. evento perdido)
    h.state.cancel(otra.id, source="portal")
    h.flush()
    assert h.offers_to(P1) == []
    with h.ctx.db.session() as s:
        assert s.scalar(select(SolicitudEspera)).estado == EstadoSolicitud.CANCELADA


# ------------------------------------------------------------------ riesgo 6: tokens revocados (HU18)
def test_token_revocado_suspende_y_la_revinculacion_reactiva(h):
    (c1,), otra = _base(h)
    h.mock.post("/portal/revoke/paciente1")
    with h.ctx.db.session() as s:
        s.scalar(select(Paciente)).cuenta.access_expira_en = timeutil.now()
    h.state.cancel(otra.id, source="portal")
    h.flush()
    assert h.offers_to(P1) == []
    assert "vuelve a vincular" in h.wa.last_to(P1).preview()
    with h.ctx.db.session() as s:
        assert s.scalar(select(SolicitudEspera)).estado == EstadoSolicitud.SUSPENDIDA
        assert s.scalar(select(Paciente)).cuenta.estado == EstadoVinculo.INVALIDA
    h.link("paciente1", verify_whatsapp=False)
    with h.ctx.db.session() as s:
        assert s.scalar(select(SolicitudEspera)).estado == EstadoSolicitud.ACTIVA


# ------------------------------------------------------------------ riesgos 19 y 42: WhatsApp
def test_baja_detiene_los_mensajes(h):
    (c1,), otra = _base(h)
    assert h.text(P1, "baja")["resultados"] == ["BAJA"]
    confirmacion = h.wa.last_to(P1)
    assert confirmacion.template == "sesion" and "te diste de baja" in confirmacion.params[0]
    n = len(h.wa.outbox)
    h.state.cancel(otra.id, source="portal")
    h.flush()
    assert len(h.wa.outbox) == n
    with h.ctx.db.session() as s:
        assert s.scalar(select(SolicitudEspera)).estado == EstadoSolicitud.SUSPENDIDA


@pytest.mark.parametrize("texto", ["BAJA", "baja", "Baja.", "  bAjA  ", "Darme de baja", "Quiero darme de baja!", "STOP"])
def test_baja_reconoce_mayusculas_minusculas_y_signos(h, texto):
    _base(h)
    assert h.text(P1, texto)["resultados"] == ["BAJA"]


def test_mensajes_que_solo_mencionan_baja_no_dan_de_baja(h):
    _base(h)
    assert h.text(P1, "la fiebre no baja")["resultados"] == []


def test_baja_repetida_responde_que_ya_esta_de_baja(h):
    _base(h)
    h.text(P1, "BAJA")
    assert h.text(P1, "baja")["resultados"] == ["YA_DE_BAJA"]
    assert "Ya estás dado de baja" in h.wa.last_to(P1).params[0]


def test_si_falla_la_confirmacion_la_baja_igual_queda_hecha(h):
    _base(h)
    h.wa.fail_next = True
    assert h.text(P1, "BAJA")["resultados"] == ["BAJA"]
    with h.ctx.db.session() as s:
        assert s.scalar(select(SolicitudEspera)).estado == EstadoSolicitud.SUSPENDIDA


def test_si_whatsapp_falla_el_cupo_pasa_al_siguiente(h):
    _base(h, n=2)
    otra = next(a for a in h.state.appointments.values() if a.username == "otra")
    h.wa.fail_next = True
    h.state.cancel(otra.id, source="portal")
    h.flush()
    assert h.offers_to(P1) == [] and len(h.offers_to(P2)) == 1


def test_no_se_puede_crear_busqueda_sin_whatsapp_verificado(h):
    c1 = h.appointment("paciente1", days=30)
    h.link("paciente1", verify_whatsapp=False)
    r = h.request("paciente1", cita_id=c1.id)
    assert r.status_code == 400 and "WhatsApp" in r.text


def test_un_tercero_no_puede_responder_la_oferta(h):
    _base(h)
    otra = next(a for a in h.state.appointments.values() if a.username == "otra")
    h.state.cancel(otra.id, source="portal")
    h.flush()
    payload = h.offers_to(P1)[0].buttons[0][1]
    assert h.press("573009999999", payload)["resultados"] == ["NO_ENCONTRADA"]


def test_firma_de_whatsapp_en_modo_cloud(make_harness):
    h = make_harness(whatsapp_mode="cloud", whatsapp_app_secret="app-secret",
                     whatsapp_phone_number_id="1", whatsapp_access_token="t")
    body = json.dumps({"entry": []}).encode()
    assert h.client.post("/webhooks/whatsapp", content=body).status_code == 401
    ok = h.client.post("/webhooks/whatsapp", content=body,
                       headers={"X-Hub-Signature-256": sign(body, "app-secret")})
    assert ok.status_code == 200
    r = h.client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "sanibot-verify-token",
                                                   "hub.challenge": "123"})
    assert r.text == "123"


# ------------------------------------------------------------------ riesgo 37: interruptor de seguridad (HU25)
def test_interruptor_detiene_reprogramaciones_masivas(make_harness):
    h = make_harness(breaker_max_per_minute=1)
    citas = [h.appointment(f"paciente{i}", days=30 + i) for i in (1, 2)]
    h.link("paciente1", phone=P1)
    h.link("paciente2", phone=P2)
    h.request("paciente1", cita_id=citas[0].id, modo="AUTOMATICO")
    h.request("paciente2", cita_id=citas[1].id, modo="AUTOMATICO")
    for i in range(2):
        otra = h.appointment(f"otra{i}", days=10 + i)
        h.state.cancel(otra.id, source="portal")
        h.flush()
    estado = h.client.get("/admin/interruptor", headers={"X-Admin-Key": "adm"}).json()
    assert estado["abierto"]
    assert h.client.get("/health").json()["status"] == "degradado"
    assert h.state.appointments[citas[1].id].estado == "booked"  # el segundo no se tocó
    assert h.client.post("/admin/interruptor/cerrar", headers={"X-Admin-Key": "adm"}).json() == {"abierto": False}
    assert h.client.get("/admin/interruptor").status_code == 401


# ------------------------------------------------------------------ riesgo 13: privacidad y auditoría
def test_los_mensajes_no_incluyen_especialidad_ni_enlaces():
    with pytest.raises(PrivacyViolation):
        check_privacy(["Tu cita de Odontología"])
    with pytest.raises(PrivacyViolation):
        check_privacy(["entra a https://sitio-falso.example"])
    check_privacy(["lunes 3 de noviembre", "9:00 a. m.", "Teusaquillo"])


def test_auditoria_es_inmutable_y_no_guarda_secretos(h):
    _base(h)
    with h.ctx.db.session() as s:
        registros = s.scalars(select(RegistroAuditoria)).all()
        assert registros
        assert all("token" not in json.dumps(r.detalle).lower() for r in registros)
    with pytest.raises(PermissionError):
        with h.ctx.db.session() as s:
            r = s.scalar(select(RegistroAuditoria))
            r.accion = "ALTERADO"
    api = h.client.get("/admin/auditoria", headers={"X-Admin-Key": "adm"}).json()
    assert {"CUENTA_VINCULADA", "SOLICITUD_CREADA", "WHATSAPP_VERIFICADO"} <= {a["accion"] for a in api}


def test_desvincular_revoca_y_cancela(h):
    (c1,), _ = _base(h)
    b = h.browser("paciente1")
    page = b.get("/inicio")
    b.post("/cuenta/desvincular", data={"csrf": h.csrf(page.text)})
    assert not h.state.refresh_tokens  # revocado en Sanitas
    with h.ctx.db.session() as s:
        assert s.scalar(select(SolicitudEspera)).estado == EstadoSolicitud.CANCELADA
        cuenta = s.scalar(select(Paciente)).cuenta
        assert cuenta.estado == EstadoVinculo.REVOCADA and cuenta.refresh_token_cifrado is None


def test_formularios_exigen_csrf(h):
    h.appointment("paciente1", days=30)
    h.link("paciente1", phone=P1)
    r = h.browser("paciente1").post("/solicitudes/nueva", data={"csrf": "falso", "especialidad": "Odontología"})
    assert r.status_code == 400


def test_callback_con_state_invalido(h):
    h.state.add_patient("paciente1", "Ana", "a@example.com")
    r = h.client.get("/auth/callback?code=abc&state=otro")
    assert r.status_code == 400 and "no es válida" in r.text


def test_metricas(h):
    (c1,), otra = _base(h)
    h.state.cancel(otra.id, source="portal")
    h.flush()
    h.press(P1, h.offers_to(P1)[0].buttons[0][1])
    m = h.client.get("/admin/metricas", headers={"X-Admin-Key": "adm"}).json()
    assert m["cupos_recuperados"] == 1 and m["tasa_aceptacion"] == 1.0
    assert 20 <= m["dias_adelantados_promedio"] <= 22
