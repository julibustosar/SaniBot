"""Arnés de pruebas: Sanitas simulada + SaniBot en el mismo proceso, con WhatsApp en modo consola."""

import json
import os
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

os.environ.setdefault("MOCK_AUTOCREATE", "false")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from mock_sanitas.app import MockConfig  # noqa: E402
from mock_sanitas.app import create_app as create_mock  # noqa: E402
from mock_sanitas.state import Appointment, SanitasState  # noqa: E402
from sanibot.config import Settings  # noqa: E402
from sanibot.integrations.whatsapp import ConsoleWhatsApp  # noqa: E402
from sanibot.main import build_context, create_app  # noqa: E402
from sanibot.models import Paciente  # noqa: E402
from sanibot.security.signatures import sign  # noqa: E402

WEBHOOK_SECRET = "secreto-de-prueba"


class Harness:
    def __init__(self, tmp_path, **overrides):
        self.state = SanitasState()
        cfg = MockConfig(seed=False, dispatch=False, redirect_uris="http://testserver/auth/callback",
                         webhook_secret=WEBHOOK_SECRET, service_key="svc", client_secret="cs")
        self.mock = TestClient(create_mock(self.state, cfg), base_url="http://sanitas")
        params = dict(base_url="http://testserver", database_url=f"sqlite:///{tmp_path}/sanibot.db",
                      token_encryption_keys=Fernet.generate_key().decode(), enable_workers=False,
                      oauth_client_secret="cs", sanitas_service_key="svc", sanitas_webhook_secret=WEBHOOK_SECRET,
                      oauth_authorize_url="http://sanitas/oauth/authorize", oauth_token_url="http://sanitas/oauth/token",
                      oauth_userinfo_url="http://sanitas/oauth/userinfo", oauth_revoke_url="http://sanitas/oauth/revoke",
                      sanitas_api_url="http://sanitas/fhir", whatsapp_mode="console", admin_api_key="adm")
        params.update(overrides)
        self.settings = Settings(**params)
        self.wa = ConsoleWhatsApp()
        self.ctx = build_context(self.settings, sanitas_http=self.mock, whatsapp=self.wa)
        self.app = create_app(ctx=self.ctx)
        self.client = TestClient(self.app)
        self.clients: dict[str, TestClient] = {}

    # ------------------------------------------------------------ datos de Sanitas
    def slot(self, especialidad="Medicina general", days=10, hour=9, estado="free"):
        inicio = (datetime.now(UTC) + timedelta(days=days)).replace(hour=hour + 5, minute=0, second=0, microsecond=0)
        return self.state.add_slot(especialidad, "Dra. Laura Méndez", inicio, estado)

    def appointment(self, username, especialidad="Medicina general", days=30, hour=9):
        if username not in self.state.patients:
            self.state.add_patient(username, username.title(), f"{username}@example.com")
        s = self.slot(especialidad, days, hour, estado="booked")
        a = Appointment(id=f"cita-{s.id[5:]}", slot_id=s.id, username=username)
        self.state.appointments[a.id] = a
        return a

    def flush(self):
        """Entrega a SaniBot los eventos pendientes de Sanitas (webhooks firmados), hasta que no queden."""
        results = []
        for _ in range(10):
            events = self.state.take_outbox()
            if not events:
                break
            for ev in events:
                body = json.dumps(ev).encode()
                r = self.client.post("/webhooks/sanitas", content=body,
                                     headers={"X-Sanitas-Signature": sign(body, WEBHOOK_SECRET),
                                              "Content-Type": "application/json"})
                assert r.status_code == 200, r.text
                results.append(r.json())
        return results

    # ------------------------------------------------------------ flujo del paciente
    def browser(self, username) -> TestClient:
        if username not in self.clients:
            self.clients[username] = TestClient(self.app)
        return self.clients[username]

    @staticmethod
    def csrf(html):
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    def link(self, username, phone="3001234567", verify_whatsapp=True) -> str:
        if username not in self.state.patients:
            self.state.add_patient(username, username.title(), f"{username}@example.com")
        b = self.browser(username)
        page = b.get("/consentimiento")
        r = b.post("/consentimiento", data={"csrf": self.csrf(page.text), "acepto": "si"}, follow_redirects=False)
        assert r.status_code == 303, r.text
        auth_url = r.headers["location"]
        form = self.mock.get(auth_url)
        assert form.status_code == 200, form.text
        hidden = dict(re.findall(r'type="hidden" name="(\w+)" value="([^"]*)"', form.text))
        r = self.mock.post("/oauth/authorize", data={**hidden, "username": username, "password": "sanitas123",
                                                     "decision": "allow"}, follow_redirects=False)
        cb = urlparse(r.headers["location"])
        r = b.get(f"{cb.path}?{cb.query}", follow_redirects=False)
        assert r.status_code == 303, r.text
        if verify_whatsapp:
            self.verify_whatsapp(username, phone)
        return self.paciente_id(username)

    def verify_whatsapp(self, username, phone):
        b = self.browser(username)
        page = b.get("/whatsapp")
        r = b.post("/whatsapp", data={"csrf": self.csrf(page.text), "telefono": phone, "acepto": "si"})
        assert r.status_code == 200, r.text
        msg = self.wa.outbox[-1]
        assert msg.template == "sanibot_verificacion"
        r = b.post("/whatsapp/verificar", data={"csrf": self.csrf(r.text), "codigo": msg.params[0]},
                   follow_redirects=False)
        assert r.status_code == 303, r.text

    def paciente_id(self, username) -> str:
        sub = self.state.patients[username].sub
        with self.ctx.db.session() as s:
            return s.query(Paciente).filter_by(sanitas_sub=sub).one().id

    def request(self, username, cita_id="", especialidad="", modo="CON_CONFIRMACION", **extra):
        b = self.browser(username)
        page = b.get("/solicitudes/nueva" + (f"?cita_id={cita_id}" if cita_id else ""))
        data = {"csrf": self.csrf(page.text), "cita_id": cita_id, "especialidad": especialidad, "modo": modo,
                "anticipacion": extra.pop("anticipacion", 12), **extra}
        return b.post("/solicitudes/nueva", data=data, follow_redirects=False)

    def press(self, phone, payload):
        body = {"entry": [{"changes": [{"value": {"messages": [
            {"from": phone, "id": "x", "type": "button", "button": {"payload": payload, "text": payload}}]}}]}]}
        return self.client.post("/webhooks/whatsapp", json=body).json()

    def text(self, phone, text):
        body = {"entry": [{"changes": [{"value": {"messages": [
            {"from": phone, "id": "x", "type": "text", "text": {"body": text}}]}}]}]}
        return self.client.post("/webhooks/whatsapp", json=body).json()

    def offers_to(self, phone):
        return [m for m in self.wa.outbox if m.to == phone and m.template == "sanibot_oferta_cupo"]


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


@pytest.fixture
def make_harness(tmp_path):
    def factory(**overrides):
        return Harness(tmp_path, **overrides)
    return factory
