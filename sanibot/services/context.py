"""Contenedor de dependencias compartidas por los servicios."""

from dataclasses import dataclass

from sanibot.config import Settings
from sanibot.db import Database
from sanibot.integrations.sanitas import SanitasClient
from sanibot.integrations.whatsapp import WhatsAppClient
from sanibot.security.crypto import TokenCipher

ESPECIALIDADES = ["Medicina general", "Odontología", "Pediatría", "Ginecología", "Dermatología"]


@dataclass
class AppContext:
    settings: Settings
    db: Database
    sanitas: SanitasClient
    whatsapp: WhatsAppClient
    cipher: TokenCipher
