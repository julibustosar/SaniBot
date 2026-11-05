"""Motor del simulador de agenda: genera cancelaciones, aperturas de agenda y reservas de otros pacientes.

Corre dentro de la Sanitas simulada (un hilo en segundo plano que se enciende y apaga desde el portal o la
consola de demostración). Los parámetros deben calibrarse con las entrevistas al personal administrativo
(riesgo 26): eventos por hora, proporción de aperturas y distribución por especialidad.
"""

import logging
import random
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from mock_sanitas.state import BOGOTA, PROFESIONALES, SanitasState

ESPECIALIDADES = {"Medicina general": 0.45, "Odontología": 0.2, "Pediatría": 0.15, "Ginecología": 0.1,
                  "Dermatología": 0.1}
DEMO = {"paciente1", "paciente2", "paciente3"}
log = logging.getLogger("simulador")


@dataclass
class Parametros:
    eventos_por_hora: float = 120
    proporcion_aperturas: float = 0.2
    proporcion_reservas: float = 0.1  # otro paciente toma un cupo libre por teléfono o en el portal


@dataclass
class Simulador:
    state: SanitasState
    params: Parametros = field(default_factory=Parametros)
    semilla: int | None = None

    def __post_init__(self):
        self.rnd = random.Random(self.semilla)
        self.historial: deque[dict] = deque(maxlen=30)
        self.eventos = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ control
    @property
    def activo(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def iniciar(self, eventos_por_hora: float | None = None) -> None:
        with self._lock:
            if eventos_por_hora is not None:
                self.params.eventos_por_hora = max(1.0, min(float(eventos_por_hora), 3600.0))
            if self.activo:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="simulador", daemon=True)
            self._thread.start()

    def detener(self) -> None:
        self._stop.set()

    def estado(self) -> dict:
        return {"activo": self.activo, "eventos_por_hora": self.params.eventos_por_hora,
                "proporcion_aperturas": self.params.proporcion_aperturas, "eventos": self.eventos,
                "historial": list(self.historial)}

    def _loop(self) -> None:
        while not self._stop.is_set():
            media = 3600 / self.params.eventos_por_hora
            if self._stop.wait(self.rnd.expovariate(1 / media)):  # llegadas tipo Poisson
                break
            try:
                self.paso()
            except Exception:  # el simulador nunca debe tumbar la Sanitas simulada
                log.exception("Error en el simulador")

    # ------------------------------------------------------------ eventos
    def paso(self) -> dict | None:
        esp = self.rnd.choices(list(ESPECIALIDADES), weights=list(ESPECIALIDADES.values()))[0]
        r = self.rnd.random()
        if r < self.params.proporcion_aperturas:
            ev = self.abrir_cupo(esp)
        elif r < self.params.proporcion_aperturas + self.params.proporcion_reservas:
            ev = self.reservar_otro(esp) or self.cancelar_otro(esp)
        else:
            ev = self.cancelar_otro(esp)
        if ev:
            self.eventos += 1
            ev["hora"] = datetime.now(BOGOTA).strftime("%H:%M:%S")
            self.historial.appendleft(ev)
            log.info("Simulador: %s", ev["texto"])
        return ev

    def _cuando(self, inicio: datetime) -> str:
        return inicio.astimezone(BOGOTA).strftime("%d/%m %H:%M")

    def cancelar_otro(self, especialidad: str) -> dict | None:
        """Otro paciente cancela una cita (al menos 24 h en el futuro, para que sirva a alguien)."""
        limite = datetime.now(UTC) + timedelta(hours=24)
        with self.state.lock:
            citas = [a for a in self.state.appointments.values() if a.estado == "booked" and a.username not in DEMO
                     and self.state.slots[a.slot_id].especialidad == especialidad
                     and self.state.slots[a.slot_id].inicio > limite]
            if not citas:
                return None
            a = self.rnd.choice(citas)
            self.state.cancel(a.id, source="portal")
            s = self.state.slots[a.slot_id]
        return {"tipo": "cancelacion", "texto": f"Otro paciente canceló {especialidad} del {self._cuando(s.inicio)}"}

    def abrir_cupo(self, especialidad: str) -> dict:
        dia = datetime.now(BOGOTA) + timedelta(days=self.rnd.randint(2, 15))
        while dia.weekday() >= 5:
            dia += timedelta(days=1)
        inicio = dia.replace(hour=self.rnd.choice([7, 8, 10, 11, 15, 17]), minute=self.rnd.choice([0, 30]),
                             second=0, microsecond=0)
        s = self.state.open_slot(especialidad, self.rnd.choice(PROFESIONALES[especialidad]), inicio)
        return {"tipo": "apertura", "texto": f"Nueva agenda: {especialidad} el {self._cuando(s.inicio)}"}

    def reservar_otro(self, especialidad: str) -> dict | None:
        """Otro paciente toma un cupo libre por los canales de Sanitas (compite con SaniBot)."""
        with self.state.lock:
            libres = [s for s in self.state.slots.values() if s.especialidad == especialidad
                      and s.to_fhir()["status"] == "free" and s.inicio > datetime.now(UTC) + timedelta(hours=24)]
            otros = [u for u in self.state.patients if u not in DEMO]
            if not libres or not otros:
                return None
            s = min(libres, key=lambda x: x.inicio)
            self.state.book(self.rnd.choice(otros), s.id, source="portal")
        return {"tipo": "reserva", "texto": f"Otro paciente reservó {especialidad} del {self._cuando(s.inicio)}"}
