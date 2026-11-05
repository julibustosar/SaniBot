"""Estado en memoria de la Sanitas simulada: pacientes, cupos (Slot), citas (Appointment), correos y eventos."""

import random
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

BOGOTA = timezone(timedelta(hours=-5))
ESPECIALIDADES = ["Medicina general", "Odontología", "Pediatría", "Ginecología", "Dermatología"]
PROFESIONALES = {
    "Medicina general": ["Dra. Laura Méndez", "Dr. Andrés Rojas"],
    "Odontología": ["Dra. Paula Castro", "Dr. Felipe Mora"],
    "Pediatría": ["Dra. Camila Torres", "Dr. Julián Ríos"],
    "Ginecología": ["Dra. Sofía Herrera", "Dra. Natalia Vargas"],
    "Dermatología": ["Dr. Mateo Silva", "Dra. Valentina Ruiz"],
}


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Patient:
    username: str
    nombre: str
    correo: str
    sub: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class Slot:
    id: str
    especialidad: str
    profesional: str
    sede: str
    inicio: datetime
    estado: str = "free"  # free | held | booked
    hold_id: str | None = None
    hold_expira: datetime | None = None

    def refresh_hold(self) -> None:
        if self.estado == "held" and self.hold_expira and self.hold_expira < now():
            self.estado, self.hold_id, self.hold_expira = "free", None, None

    def to_fhir(self) -> dict:
        self.refresh_hold()
        return {"resourceType": "Slot", "id": self.id, "status": self.estado, "start": self.inicio.isoformat(),
                "serviceType": self.especialidad, "location": self.sede, "practitioner": self.profesional}


@dataclass
class Appointment:
    id: str
    slot_id: str
    username: str
    estado: str = "booked"  # booked | cancelled


@dataclass
class Email:
    para: str
    asunto: str
    cuerpo: str
    fecha: datetime = field(default_factory=now)


class SanitasState:
    def __init__(self, sede: str = "Teusaquillo"):
        self.lock = threading.RLock()
        self.sede = sede
        self.patients: dict[str, Patient] = {}
        self.slots: dict[str, Slot] = {}
        self.appointments: dict[str, Appointment] = {}
        self.emails: list[Email] = []
        self.outbox: list[dict] = []  # eventos de webhook pendientes de entregar a SaniBot
        self.auth_codes: dict[str, dict] = {}
        self.refresh_tokens: dict[str, dict] = {}
        self.revoked_users: set[str] = set()
        self.mailer = None  # función opcional (Email) -> None para enviar por SMTP

    # ------------------------------------------------------------ datos
    def add_patient(self, username: str, nombre: str, correo: str) -> Patient:
        p = Patient(username, nombre, correo)
        self.patients[username] = p
        return p

    def patient_by_sub(self, sub: str) -> Patient | None:
        return next((p for p in self.patients.values() if p.sub == sub), None)

    def add_slot(self, especialidad: str, profesional: str, inicio: datetime, estado: str = "free") -> Slot:
        s = Slot(id=f"slot-{uuid.uuid4().hex[:10]}", especialidad=especialidad, profesional=profesional,
                 sede=self.sede, inicio=inicio.astimezone(UTC), estado=estado)
        self.slots[s.id] = s
        return s

    def appointment_view(self, a: Appointment) -> dict:
        s = self.slots[a.slot_id]
        return {"resourceType": "Appointment", "id": a.id, "status": a.estado, "start": s.inicio.isoformat(),
                "serviceType": s.especialidad, "location": s.sede, "practitioner": s.profesional, "slot": s.id,
                "patient": a.username}

    # ------------------------------------------------------------ operaciones con efectos (correos y eventos)
    def emit(self, tipo: str, **data) -> dict:
        ev = {"id": f"evt-{uuid.uuid4().hex}", "type": tipo, "occurred_at": now().isoformat(), **data}
        self.outbox.append(ev)
        return ev

    def send_email(self, para: str, asunto: str, cuerpo: str) -> None:
        mail = Email(para, asunto, cuerpo)
        self.emails.append(mail)
        if self.mailer:
            try:
                self.mailer(mail)
            except Exception:  # noqa: S110 - el correo es mejor esfuerzo en el simulador
                pass

    def _detalle_cita(self, s: Slot) -> str:
        d = s.inicio.astimezone(BOGOTA)
        return (f"Especialidad: {s.especialidad}\nProfesional: {s.profesional}\nSede: {s.sede}\n"
                f"Fecha: {d:%d/%m/%Y}\nHora: {d:%H:%M}")

    def book(self, username: str, slot_id: str, hold_id: str | None = None, source: str = "api") -> Appointment:
        with self.lock:
            s = self.slots.get(slot_id)
            if not s:
                raise LookupError("Cupo inexistente")
            s.refresh_hold()
            if s.estado == "booked":
                raise ValueError("El cupo ya fue asignado")
            if s.estado == "held" and s.hold_id != hold_id:
                raise ValueError("El cupo está apartado por otro proceso")
            s.estado, s.hold_id, s.hold_expira = "booked", None, None
            a = Appointment(id=f"cita-{uuid.uuid4().hex[:10]}", slot_id=s.id, username=username)
            self.appointments[a.id] = a
            p = self.patients[username]
            # Proceso propio de Sanitas: correo de confirmación con toda la información de la cita.
            self.send_email(p.correo, "Confirmación de su cita - EPS Sanitas",
                            f"Hola {p.nombre}, su cita fue agendada.\n\n{self._detalle_cita(s)}")
            self.emit("appointment.booked", appointment_id=a.id, patient_sub=p.sub, source=source)
            return a

    def cancel(self, appointment_id: str, source: str = "api") -> Appointment:
        with self.lock:
            a = self.appointments.get(appointment_id)
            if not a:
                raise LookupError("Cita inexistente")
            if a.estado == "cancelled":
                return a
            a.estado = "cancelled"
            s = self.slots[a.slot_id]
            s.estado, s.hold_id, s.hold_expira = "free", None, None
            p = self.patients[a.username]
            # Proceso propio de Sanitas: correo de cancelación con la información de la cita.
            self.send_email(p.correo, "Cancelación de su cita - EPS Sanitas",
                            f"Hola {p.nombre}, su cita fue cancelada.\n\n{self._detalle_cita(s)}")
            self.emit("appointment.cancelled", appointment_id=a.id, patient_sub=p.sub, source=source)
            self.emit("slot.available", slot_id=s.id)
            return a

    def reschedule(self, appointment_id: str, new_slot_id: str, source: str = "portal") -> Appointment:
        with self.lock:
            a = self.appointments.get(appointment_id)
            new = self.slots.get(new_slot_id)
            if not a or a.estado != "booked" or not new:
                raise LookupError("Cita o cupo inexistente")
            new.refresh_hold()
            if new.estado != "free":
                raise ValueError("El cupo no está disponible")
            old = self.slots[a.slot_id]
            old.estado = "free"
            new.estado = "booked"
            a.slot_id = new.id
            p = self.patients[a.username]
            self.send_email(p.correo, "Cancelación de su cita - EPS Sanitas",
                            f"Hola {p.nombre}, su cita anterior fue cancelada por reprogramación.\n\n{self._detalle_cita(old)}")
            self.send_email(p.correo, "Confirmación de su cita - EPS Sanitas",
                            f"Hola {p.nombre}, su cita fue reprogramada.\n\n{self._detalle_cita(new)}")
            self.emit("appointment.updated", appointment_id=a.id, patient_sub=p.sub, source=source,
                      start=new.inicio.isoformat())
            self.emit("slot.available", slot_id=old.id)
            return a

    def hold(self, slot_id: str, minutes: int) -> Slot:
        with self.lock:
            s = self.slots.get(slot_id)
            if not s:
                raise LookupError("Cupo inexistente")
            s.refresh_hold()
            if s.estado != "free":
                raise ValueError("El cupo no está disponible")
            s.estado, s.hold_id = "held", f"hold-{secrets.token_hex(8)}"
            s.hold_expira = now() + timedelta(minutes=minutes)
            return s

    def release(self, slot_id: str, hold_id: str) -> None:
        with self.lock:
            s = self.slots.get(slot_id)
            if s and s.estado == "held" and s.hold_id == hold_id:
                s.estado, s.hold_id, s.hold_expira = "free", None, None

    def open_slot(self, especialidad: str, profesional: str, inicio: datetime) -> Slot:
        with self.lock:
            s = self.add_slot(especialidad, profesional, inicio)
            self.emit("slot.available", slot_id=s.id)
            return s

    def take_outbox(self) -> list[dict]:
        with self.lock:
            items, self.outbox = self.outbox, []
            return items


def seed(state: SanitasState, rnd_seed: int = 7) -> None:
    """Datos de demostración: la agenda de las próximas 3 semanas está llena; los pacientes de demo tienen citas lejanas."""
    rnd = random.Random(rnd_seed)
    demo = [("paciente1", "Ana Gómez", "ana.gomez@example.com"), ("paciente2", "Luis Pérez", "luis.perez@example.com"),
            ("paciente3", "María Rodríguez", "maria.rodriguez@example.com")]
    for u, n, c in demo:
        state.add_patient(u, n, c)
    for i in range(4, 41):
        state.add_patient(f"paciente{i}", f"Paciente {i}", f"paciente{i}@example.com")
    today = datetime.now(BOGOTA).replace(hour=0, minute=0, second=0, microsecond=0)
    for day in range(1, 46):
        d = today + timedelta(days=day)
        if d.weekday() >= 5:
            continue
        for esp, profs in PROFESIONALES.items():
            for prof in profs:
                for h in (7, 9, 14, 16):
                    state.add_slot(esp, prof, d.replace(hour=h))
    others = [p for p in state.patients if p not in ("paciente1", "paciente2", "paciente3")]
    for s in list(state.slots.values()):
        if (s.inicio - datetime.now(UTC)).days < 21:
            s.estado = "booked"
            a = Appointment(id=f"cita-{uuid.uuid4().hex[:10]}", slot_id=s.id, username=rnd.choice(others))
            state.appointments[a.id] = a
    # citas lejanas de los pacientes de demostración (días 28 a 40)
    for u, esp in (("paciente1", "Medicina general"), ("paciente2", "Odontología"), ("paciente3", "Pediatría")):
        free = sorted((s for s in state.slots.values() if s.especialidad == esp and s.estado == "free"
                       and 28 <= (s.inicio - datetime.now(UTC)).days <= 40), key=lambda s: s.inicio)
        if free:
            s = free[len(free) // 2]
            s.estado = "booked"
            a = Appointment(id=f"cita-{uuid.uuid4().hex[:10]}", slot_id=s.id, username=u)
            state.appointments[a.id] = a
