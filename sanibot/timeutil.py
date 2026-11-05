"""Utilidades de fecha y hora. Todo se guarda en UTC y se muestra en hora de Colombia (UTC-5, sin horario de verano)."""

from datetime import UTC, datetime, timedelta, timezone

BOGOTA = timezone(timedelta(hours=-5), "COT")
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def now() -> datetime:
    return datetime.now(UTC)


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Fecha sin zona horaria: {value}")
    return dt.astimezone(UTC)


def local(dt: datetime) -> datetime:
    return dt.astimezone(BOGOTA)


def fecha_larga(dt: datetime) -> str:
    d = local(dt)
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"


def hora(dt: datetime) -> str:
    d = local(dt)
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d} {'a. m.' if d.hour < 12 else 'p. m.'}"


def franja(dt: datetime) -> str:
    """Franja horaria local: 'manana' antes del mediodía, 'tarde' desde el mediodía."""
    return "manana" if local(dt).hour < 12 else "tarde"
