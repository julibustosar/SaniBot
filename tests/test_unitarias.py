"""Pruebas unitarias de utilidades y reglas de compatibilidad."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from sanibot import timeutil
from sanibot.integrations.sanitas import Cupo
from sanibot.integrations.whatsapp import normalize_phone, parse_webhook
from sanibot.models import ModoReasignacion, SolicitudEspera, TipoSolicitud
from sanibot.security.crypto import TokenCipher
from sanibot.services.matching import compatible


def _sol(**kw):
    base = dict(tipo=TipoSolicitud.CITA_NUEVA, especialidad="Pediatría", sede="Teusaquillo", franjas=[],
                anticipacion_min_horas=24, modo=ModoReasignacion.CON_CONFIRMACION, fecha_limite=None,
                cita_actual_fecha=None)
    base.update(kw)
    return SolicitudEspera(**base)


def _cupo(dias=5, hora_local=9, esp="Pediatría"):
    inicio = (datetime.now(UTC) + timedelta(days=dias)).replace(hour=hora_local + 5, minute=0, second=0, microsecond=0)
    return Cupo("s1", "free", inicio, esp, "Teusaquillo", "Dra. X")


def test_compatibilidad_basica():
    assert compatible(_sol(), _cupo())
    assert not compatible(_sol(), _cupo(esp="Odontología"))
    assert not compatible(_sol(anticipacion_min_horas=24 * 10), _cupo(dias=5))
    assert not compatible(_sol(fecha_limite=datetime.now(UTC) + timedelta(days=2)), _cupo(dias=5))
    assert compatible(_sol(franjas=["manana"]), _cupo(hora_local=8))
    assert not compatible(_sol(franjas=["manana"]), _cupo(hora_local=15))
    assert compatible(_sol(franjas=["tarde"]), _cupo(hora_local=12))


def test_compatibilidad_adelantar_solo_antes_de_la_cita():
    cita = datetime.now(UTC) + timedelta(days=10)
    sol = _sol(tipo=TipoSolicitud.ADELANTAR_CITA, cita_actual_fecha=cita)
    assert compatible(sol, _cupo(dias=5))
    assert not compatible(sol, _cupo(dias=12))


def test_hora_local_bogota():
    dt = datetime(2026, 11, 3, 14, 0, tzinfo=UTC)  # 9:00 a. m. en Bogotá
    assert timeutil.hora(dt) == "9:00 a. m."
    assert timeutil.fecha_larga(dt) == "martes 3 de noviembre"
    assert timeutil.franja(dt) == "manana"


@pytest.mark.parametrize("raw,expected", [("300 123 4567", "573001234567"), ("+57 300-123-4567", "573001234567"),
                                          ("573001234567", "573001234567")])
def test_normaliza_telefono(raw, expected):
    assert normalize_phone(raw) == expected


def test_telefono_invalido():
    with pytest.raises(ValueError):
        normalize_phone("12345")


def test_rotacion_de_llaves():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = TokenCipher(old).encrypt("secreto")
    rotated = TokenCipher(f"{new},{old}")
    assert rotated.decrypt(token) == "secreto"
    assert TokenCipher(new).decrypt(rotated.rotate(token)) == "secreto"
    with pytest.raises(ValueError):
        TokenCipher(new).decrypt(token)


def test_parse_webhook_de_whatsapp():
    payload = {"entry": [{"changes": [{"value": {
        "messages": [{"from": "57300", "id": "a", "type": "button", "button": {"payload": "ACEPTAR:1"}},
                     {"from": "57300", "id": "b", "type": "interactive", "interactive": {"button_reply": {"id": "RECHAZAR:2"}}},
                     {"from": "57300", "id": "c", "type": "text", "text": {"body": "BAJA"}}],
        "statuses": [{"id": "wamid.1", "status": "read"}]}}]}]}
    msgs, statuses = parse_webhook(payload)
    assert [m["value"] for m in msgs] == ["ACEPTAR:1", "RECHAZAR:2", "BAJA"]
    assert statuses == [{"id": "wamid.1", "status": "read", "error_code": None}]
