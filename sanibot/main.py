"""Punto de entrada de SaniBot: construye la aplicación FastAPI y sus dependencias."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sanibot.api import admin, demo, web, webhooks
from sanibot.config import Settings, get_settings
from sanibot.db import Database
from sanibot.integrations.sanitas import SanitasClient
from sanibot.integrations.whatsapp import CloudWhatsApp, ConsoleWhatsApp, WhatsAppClient
from sanibot.security.crypto import TokenCipher
from sanibot.security.sessions import SessionManager
from sanibot.services import worker
from sanibot.services.context import AppContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # sin una línea por cada llamada HTTP
log = logging.getLogger("sanibot")
BASE = Path(__file__).parent


def build_context(settings: Settings, sanitas_http: httpx.Client | None = None,
                  whatsapp: WhatsAppClient | None = None) -> AppContext:
    if whatsapp is None:
        whatsapp = CloudWhatsApp(settings) if settings.whatsapp_mode == "cloud" else ConsoleWhatsApp()
    return AppContext(settings=settings, db=Database(settings.database_url),
                      sanitas=SanitasClient(settings, sanitas_http), whatsapp=whatsapp,
                      cipher=TokenCipher(settings.token_encryption_keys))


def create_app(settings: Settings | None = None, ctx: AppContext | None = None) -> FastAPI:
    settings = settings or (ctx.settings if ctx else get_settings())
    settings.check_production()
    ctx = ctx or build_context(settings)
    ctx.db.create_all()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.enable_workers:
            async def loop():
                while True:
                    await asyncio.sleep(settings.worker_interval_seconds)
                    await asyncio.to_thread(worker.tick, ctx)
            task = asyncio.create_task(loop())
        log.info("SaniBot iniciado (WhatsApp: %s, sede: %s)", settings.whatsapp_mode, settings.sede)
        if settings.demo_active:
            log.info("Consola de demostración: %s/demo", settings.base_url)
        yield
        if task:
            task.cancel()

    app = FastAPI(title="SaniBot", version="0.1.0", lifespan=lifespan,
                  description="Servicio complementario de búsqueda y reasignación automática de citas (MVP).")
    app.state.ctx = ctx
    app.state.sessions = SessionManager(settings.secret_key, settings.secure_cookies)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    app.include_router(web.router)
    app.include_router(webhooks.router)
    app.include_router(admin.router)
    app.include_router(demo.router)
    return app


def app_factory() -> FastAPI:  # uvicorn sanibot.main:app_factory --factory
    return create_app()
