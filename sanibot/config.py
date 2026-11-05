"""Configuración de SaniBot, leída de variables de entorno (prefijo SANIBOT_)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SANIBOT_", env_file=".env", extra="ignore")

    # Aplicación
    base_url: str = "http://localhost:8000"
    secret_key: str = "cambiar-en-produccion-clave-de-sesion"
    database_url: str = "sqlite:///./sanibot.db"
    admin_api_key: str = "cambiar-admin-key"
    sede: str = "Teusaquillo"
    consent_version: str = "1.0"
    enable_workers: bool = True
    worker_interval_seconds: int = 15
    secure_cookies: bool = False

    # Cifrado de tokens (RNF01). Lista separada por comas de llaves Fernet; la primera cifra, todas descifran (rotación).
    token_encryption_keys: str = ""

    # Sanitas: OAuth 2.0 (autorización delegada) y API de agendas
    oauth_client_id: str = "sanibot"
    oauth_client_secret: str = "sanibot-secret"
    oauth_authorize_url: str = "http://localhost:8100/oauth/authorize"
    oauth_token_url: str = "http://sanitas:8100/oauth/token"
    oauth_userinfo_url: str = "http://sanitas:8100/oauth/userinfo"
    oauth_revoke_url: str = "http://sanitas:8100/oauth/revoke"
    oauth_scopes: str = "openid offline_access citas.leer citas.reprogramar"
    sanitas_api_url: str = "http://sanitas:8100/fhir"
    sanitas_service_key: str = "clave-servicio-sanibot"
    sanitas_webhook_secret: str = "secreto-webhook-sanitas"
    sanitas_timeout_seconds: float = 10.0

    # WhatsApp (RNF13: único canal, bot verificado provisto por Sanitas)
    whatsapp_mode: str = "cloud"  # API de WhatsApp Cloud de Meta (las pruebas automáticas usan un doble en memoria)
    whatsapp_api_url: str = "https://graph.facebook.com"
    whatsapp_api_version: str = "v25.0"
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""  # WABA: solo para registrar y consultar plantillas
    whatsapp_numero_visible: str = ""  # número del bot con indicativo (p. ej. 15551234567), para el enlace wa.me
    whatsapp_access_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = "sanibot-verify-token"
    whatsapp_template_language: str = "es"

    # Reglas de negocio
    offer_window_minutes: int = 30
    breaker_max_per_minute: int = 20
    verification_code_minutes: int = 10
    verification_max_attempts: int = 5

    @property
    def is_local(self) -> bool:
        return any(h in self.base_url for h in ("localhost", "127.0.0.1", "testserver"))

    def check_production(self) -> None:
        """Impide arrancar fuera de localhost con los secretos de desarrollo (riesgos 9 y 39)."""
        local = self.is_local
        defaults = {"secret_key": "cambiar-en-produccion-clave-de-sesion", "admin_api_key": "cambiar-admin-key",
                    "oauth_client_secret": "sanibot-secret", "sanitas_webhook_secret": "secreto-webhook-sanitas"}
        inseguros = [k for k, v in defaults.items() if getattr(self, k) == v]
        if inseguros and not local:
            raise RuntimeError(f"Configure valores propios para: {', '.join(inseguros)}")
        if not local and not self.secure_cookies:
            raise RuntimeError("En producción SANIBOT_SECURE_COOKIES debe ser true (HTTPS)")


@lru_cache
def get_settings() -> Settings:
    return Settings()
