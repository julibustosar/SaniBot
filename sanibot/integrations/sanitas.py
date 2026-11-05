"""Cliente de Sanitas: OAuth 2.0 (autorización delegada) y API de agendas tipo FHIR.

Toda la comunicación con Sanitas pasa por esta clase (patrón adaptador, RNF11 / riesgo 1): si la API cambia,
solo se modifica este archivo.
"""

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

import httpx

from sanibot.config import Settings
from sanibot.timeutil import parse_iso

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # formato de id de HL7 FHIR


class SanitasError(Exception):
    """Error de comunicación o respuesta inesperada de Sanitas."""


class SanitasAuthError(SanitasError):
    """El token del paciente es inválido, venció o fue revocado."""


class SlotUnavailable(SanitasError):
    """El cupo ya no está disponible."""


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str


@dataclass
class Cupo:
    id: str
    estado: str
    inicio: datetime
    especialidad: str
    sede: str
    profesional: str

    @classmethod
    def from_fhir(cls, d: dict) -> "Cupo":
        return cls(d["id"], d["status"], parse_iso(d["start"]), d["serviceType"], d["location"], d["practitioner"])


@dataclass
class Cita:
    id: str
    estado: str
    inicio: datetime
    especialidad: str
    sede: str
    profesional: str
    cupo_id: str

    @classmethod
    def from_fhir(cls, d: dict) -> "Cita":
        return cls(d["id"], d["status"], parse_iso(d["start"]), d["serviceType"], d["location"], d["practitioner"],
                   d.get("slot", ""))


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class SanitasClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.s = settings
        self.http = http or httpx.Client(timeout=settings.sanitas_timeout_seconds)

    # ------------------------------------------------------------ utilidades
    def _url(self, path: str, *ids: str) -> str:
        """Arma la URL de la API de Sanitas. Los identificadores se validan para que nadie pueda alterar la ruta."""
        for i in ids:
            if not _ID.fullmatch(str(i)):
                raise SanitasError("Identificador inválido")
        return self.s.sanitas_api_url.rstrip("/") + path.format(*ids)

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            r = self.http.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise SanitasError(f"Sanitas no disponible: {exc.__class__.__name__}") from exc
        if r.status_code == 401:
            raise SanitasAuthError("Token inválido o revocado")
        if r.status_code == 409:
            raise SlotUnavailable(r.json().get("detail", "Conflicto"))
        if r.status_code >= 400:
            raise SanitasError(f"Sanitas respondió {r.status_code}")
        return r

    @staticmethod
    def _bearer(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _service(self) -> dict:
        return {"X-Api-Key": self.s.sanitas_service_key}

    # ------------------------------------------------------------ OAuth 2.0
    def authorize_url(self, state: str, code_challenge: str) -> str:
        q = {"response_type": "code", "client_id": self.s.oauth_client_id, "redirect_uri": self.redirect_uri,
             "scope": self.s.oauth_scopes, "state": state, "code_challenge": code_challenge,
             "code_challenge_method": "S256"}
        return f"{self.s.oauth_authorize_url}?{urlencode(q)}"

    @property
    def redirect_uri(self) -> str:
        return self.s.base_url.rstrip("/") + "/auth/callback"

    def _token_request(self, data: dict) -> TokenSet:
        data |= {"client_id": self.s.oauth_client_id, "client_secret": self.s.oauth_client_secret}
        try:
            r = self.http.post(self.s.oauth_token_url, data=data)
        except httpx.HTTPError as exc:
            raise SanitasError("Proveedor de identidad no disponible") from exc
        if r.status_code in (400, 401):
            raise SanitasAuthError(r.json().get("error", "invalid_grant"))
        if r.status_code >= 400:
            raise SanitasError(f"Proveedor de identidad respondió {r.status_code}")
        j = r.json()
        return TokenSet(j["access_token"], j.get("refresh_token"), int(j.get("expires_in", 300)), j.get("scope", ""))

    def exchange_code(self, code: str, verifier: str) -> TokenSet:
        return self._token_request({"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": self.redirect_uri, "code_verifier": verifier})

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def userinfo(self, access_token: str) -> dict:
        return self._request("GET", self.s.oauth_userinfo_url, headers=self._bearer(access_token)).json()

    def revoke(self, refresh_token: str) -> None:
        try:
            self.http.post(self.s.oauth_revoke_url, data={"token": refresh_token, "token_type_hint": "refresh_token",
                                                          "client_id": self.s.oauth_client_id,
                                                          "client_secret": self.s.oauth_client_secret})
        except httpx.HTTPError:
            pass  # la revocación local ocurre de todas formas

    # ------------------------------------------------------------ citas del paciente
    def list_appointments(self, token: str) -> list[Cita]:
        j = self._request("GET", self._url("/Appointment"), headers=self._bearer(token)).json()
        return [Cita.from_fhir(e) for e in j.get("entry", [])]

    def get_appointment(self, token: str, appointment_id: str) -> Cita | None:
        try:
            return Cita.from_fhir(self._request("GET", self._url("/Appointment/{}", appointment_id),
                                                headers=self._bearer(token)).json())
        except SanitasError as exc:
            if isinstance(exc, SanitasAuthError):
                raise
            return None

    def book(self, token: str, slot_id: str, hold_id: str | None) -> Cita:
        r = self._request("POST", self._url("/Appointment"), headers=self._bearer(token),
                          json={"slot": slot_id, "hold_id": hold_id})
        return Cita.from_fhir(r.json())

    def cancel(self, token: str, appointment_id: str) -> None:
        self._request("POST", self._url("/Appointment/{}/$cancel", appointment_id), headers=self._bearer(token))

    # ------------------------------------------------------------ cupos (credencial de servicio)
    def get_slot(self, slot_id: str) -> Cupo | None:
        try:
            return Cupo.from_fhir(self._request("GET", self._url("/Slot/{}", slot_id), headers=self._service()).json())
        except SlotUnavailable:
            return None

    def search_free_slots(self, especialidad: str, limit: int = 40) -> list[Cupo]:
        r = self._request("GET", self._url("/Slot"), headers=self._service(),
                          params={"status": "free", "serviceType": especialidad, "limit": limit})
        return [Cupo.from_fhir(e) for e in r.json().get("entry", [])]

    def hold_slot(self, slot_id: str, minutes: int) -> str:
        r = self._request("POST", self._url("/Slot/{}/$hold", slot_id), headers=self._service(),
                          json={"minutes": minutes})
        return r.json()["hold_id"]

    def release_slot(self, slot_id: str, hold_id: str) -> None:
        try:
            self._request("POST", self._url("/Slot/{}/$release", slot_id), headers=self._service(),
                          json={"hold_id": hold_id})
        except SanitasError:
            pass  # el apartado vence solo en Sanitas
