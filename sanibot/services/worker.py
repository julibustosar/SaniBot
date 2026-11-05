"""Tareas periódicas: expirar ofertas, reintentar eventos y cancelaciones pendientes, vencer solicitudes."""

import logging

from sanibot.services import matching, reprogramming, requests_service
from sanibot.services.context import AppContext

log = logging.getLogger("sanibot.worker")


def tick(ctx: AppContext) -> dict:
    result = {}
    for name, fn in (("ofertas_expiradas", matching.expire_offers), ("eventos", matching.retry_events),
                     ("cancelaciones", reprogramming.retry_pending_cancellations),
                     ("solicitudes_vencidas", requests_service.expire_requests)):
        try:
            result[name] = fn(ctx)
        except Exception:  # una tarea fallida no debe detener las demás
            log.exception("Error en la tarea periódica %s", name)
            result[name] = "error"
    return result
