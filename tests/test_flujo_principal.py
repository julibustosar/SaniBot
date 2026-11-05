"""Flujo extremo a extremo del MVP: vinculación → solicitud → cupo liberado → oferta por WhatsApp → reprogramación."""

from sqlalchemy import select

from sanibot.models import (
    CuentaVinculada,
    EstadoOferta,
    EstadoSolicitud,
    OfertaCupo,
    RegistroAuditoria,
    SolicitudEspera,
)

P1 = "573001110001"
P2 = "573001110002"


def _setup_cita_y_ocupada(h):
    """Paciente 1 tiene cita en 30 días; la agenda está llena y otra persona tiene una cita en 10 días."""
    cita_p1 = h.appointment("paciente1", days=30)
    cita_otra = h.appointment("otra", days=10)
    return cita_p1, cita_otra


def test_flujo_completo_con_confirmacion(h):
    cita_p1, cita_otra = _setup_cita_y_ocupada(h)
    h.link("paciente1", phone=P1)
    r = h.request("paciente1", cita_id=cita_p1.id)
    assert r.status_code == 303, r.text
    assert h.offers_to(P1) == []  # no hay cupos libres todavía

    # Otra persona cancela desde el portal: Sanitas envía el correo y el evento a SaniBot.
    h.state.cancel(cita_otra.id, source="portal")
    h.flush()
    ofertas = h.offers_to(P1)
    assert len(ofertas) == 1
    oferta = ofertas[0]
    assert [b[0] for b in oferta.buttons] == ["Aceptar", "Rechazar"]
    assert all("Medicina" not in p for p in oferta.params)  # privacidad: sin especialidad (RNF05)
    slot = h.state.slots[cita_otra.slot_id]
    assert slot.estado == "held"  # el cupo queda apartado mientras el paciente responde

    # El paciente acepta con el botón de WhatsApp.
    res = h.press(P1, oferta.buttons[0][1])
    assert res["resultados"] == ["REPROGRAMADA"]
    nuevas = [a for a in h.state.appointments.values() if a.username == "paciente1" and a.estado == "booked"]
    assert len(nuevas) == 1 and nuevas[0].slot_id == slot.id
    assert h.state.appointments[cita_p1.id].estado == "cancelled"

    # Sanitas envió, por su proceso propio, el correo de confirmación de la nueva y el de cancelación de la anterior.
    asuntos = [e.asunto for e in h.state.emails if e.para == "paciente1@example.com"]
    assert any("Confirmación" in a for a in asuntos) and any("Cancelación" in a for a in asuntos)
    confirm = h.wa.last_to(P1)
    assert confirm.template == "sanibot_confirmacion" and "correo" in confirm.preview()

    with h.ctx.db.session() as s:
        sol = s.scalar(select(SolicitudEspera))
        assert sol.estado == EstadoSolicitud.CUMPLIDA
        assert s.scalar(select(OfertaCupo)).estado == EstadoOferta.REPROGRAMADA
        acciones = [a.accion for a in s.scalars(select(RegistroAuditoria))]
    assert "REPROGRAMACION_OK" in acciones and "OFERTA_ACEPTADA" in acciones
    h.flush()  # el cupo anterior liberado no tiene candidatos: no pasa nada


def test_modo_automatico_reprograma_sin_preguntar(h):
    cita_p1, cita_otra = _setup_cita_y_ocupada(h)
    h.link("paciente1", phone=P1)
    h.request("paciente1", cita_id=cita_p1.id, modo="AUTOMATICO")
    h.state.cancel(cita_otra.id, source="portal")
    h.flush()
    assert h.offers_to(P1) == []
    assert h.wa.last_to(P1).template == "sanibot_confirmacion"
    assert h.state.appointments[cita_p1.id].estado == "cancelled"


def test_rechazo_pasa_al_siguiente_por_orden_de_inscripcion(h):
    c1 = h.appointment("paciente1", days=30)
    c2 = h.appointment("paciente2", days=32)
    otra = h.appointment("otra", days=10)
    h.link("paciente1", phone=P1)
    h.link("paciente2", phone=P2)
    h.request("paciente1", cita_id=c1.id)  # se inscribe primero
    h.request("paciente2", cita_id=c2.id)
    h.state.cancel(otra.id, source="portal")
    h.flush()
    assert len(h.offers_to(P1)) == 1 and h.offers_to(P2) == []
    h.press(P1, h.offers_to(P1)[0].buttons[1][1])  # rechaza
    assert len(h.offers_to(P2)) == 1
    # el primero no vuelve a recibir el mismo cupo
    assert len(h.offers_to(P1)) == 1


def test_oferta_vencida_pasa_al_siguiente(h):
    from datetime import timedelta

    from sanibot import timeutil
    from sanibot.services import worker

    c1 = h.appointment("paciente1", days=30)
    c2 = h.appointment("paciente2", days=32)
    otra = h.appointment("otra", days=10)
    h.link("paciente1", phone=P1)
    h.link("paciente2", phone=P2)
    h.request("paciente1", cita_id=c1.id)
    h.request("paciente2", cita_id=c2.id)
    h.state.cancel(otra.id, source="portal")
    h.flush()
    with h.ctx.db.session() as s:
        of = s.scalar(select(OfertaCupo))
        of.expira_en = timeutil.now() - timedelta(seconds=1)
    worker.tick(h.ctx)
    assert len(h.offers_to(P2)) == 1
    assert "venció" in h.wa.last_to(P1).preview()
    # responder una oferta vencida no hace nada
    res = h.press(P1, h.offers_to(P1)[0].buttons[0][1])
    assert res["resultados"] == ["NO_DISPONIBLE"]


def test_cita_nueva_recibe_cupo_libre_existente(h):
    h.state.add_patient("paciente1", "Ana", "ana@example.com")
    libre = h.slot("Odontología", days=5, hour=8)
    h.link("paciente1", phone=P1)
    r = h.request("paciente1", especialidad="Odontología", manana="1")
    assert r.status_code == 303
    ofertas = h.offers_to(P1)
    assert len(ofertas) == 1
    h.press(P1, ofertas[0].buttons[0][1])
    assert h.state.slots[libre.id].estado == "booked"


def test_preferencias_filtran_cupos(h):
    c1 = h.appointment("paciente1", days=30)
    h.link("paciente1", phone=P1)
    h.request("paciente1", cita_id=c1.id, manana="1")  # solo mañanas
    tarde = h.appointment("otra", days=10, hour=15)
    h.state.cancel(tarde.id, source="portal")
    h.flush()
    assert h.offers_to(P1) == []
    manana = h.appointment("otra2", days=11, hour=8)
    h.state.cancel(manana.id, source="portal")
    h.flush()
    assert len(h.offers_to(P1)) == 1


def test_no_ofrece_cupos_posteriores_a_la_cita_actual(h):
    c1 = h.appointment("paciente1", days=10)
    h.link("paciente1", phone=P1)
    h.request("paciente1", cita_id=c1.id)
    despues = h.appointment("otra", days=20)
    h.state.cancel(despues.id, source="portal")
    h.flush()
    assert h.offers_to(P1) == []


def test_tokens_cifrados_en_reposo(h):
    h.appointment("paciente1", days=30)
    h.link("paciente1", phone=P1)
    raw_tokens = list(h.state.refresh_tokens)
    with h.ctx.db.session() as s:
        cuenta = s.scalar(select(CuentaVinculada))
        assert cuenta.refresh_token_cifrado not in raw_tokens
        assert h.ctx.cipher.decrypt(cuenta.refresh_token_cifrado) in raw_tokens
        assert "eyJ" not in (cuenta.access_token_cifrado or "")[:3]
