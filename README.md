# SaniBot · MVP

SaniBot es un servicio complementario, autorizado por EPS Sanitas, que busca en segundo plano citas más próximas para los
pacientes del centro médico Teusaquillo. Cuando se libera un cupo por una cancelación o por la apertura de una agenda, se
lo ofrece al paciente por **WhatsApp**, su único canal de comunicación. El paciente acepta o rechaza con un botón, y
SaniBot reprograma la cita sin que tenga que entrar al portal de Sanitas.

Este repositorio contiene el **producto mínimo viable (Sprints 0 a 4)**. Todo corre en un entorno simulado: no se conecta
a sistemas reales de Sanitas.

## Qué incluye

| Componente | Descripción |
|---|---|
| `sanibot/` | Backend FastAPI y aplicación web de SaniBot. |
| `mock_sanitas/` | Sanitas simulada: proveedor OAuth 2.0 con PKCE, API de agendas tipo FHIR (`Slot`, `Appointment`), portal y **correos de confirmación y cancelación**, que Sanitas envía por su cuenta, por fuera de SaniBot. |
| `simulator/` | Simulador de agenda integrado en la Sanitas simulada: cancelaciones de otros pacientes, nuevas agendas y reservas que compiten con SaniBot (llegadas tipo Poisson, parámetros calibrables). |
| `iniciar.py` | Arranca todo con un solo comando y abre la **consola de demostración** (`/demo`). |
| `sanibot/plantillas.py` | Registra y consulta en Meta las plantillas de WhatsApp de SaniBot. |
| `docs/GUIA_WHATSAPP_REAL.md` | Paso a paso para probar con WhatsApp real. |
| `tests/` | 63 pruebas automatizadas: flujo extremo a extremo, controles de la matriz de riesgos, plantillas de WhatsApp y consola de demostración. |
| `docker-compose.yml` | SaniBot + Sanitas simulada + PostgreSQL + Mailpit (correo) + simulador y Keycloak opcionales. |
| `.github/` | CI (ruff, pytest con cobertura mínima de 80 %, SonarQube Cloud, pip-audit) y Dependabot. |

## Cómo ejecutarlo

### Opción rápida: un solo comando (recomendada para la demostración)

Requiere Python 3.11 o superior. La primera vez:

```bash
python -m venv .venv
source .venv/Scripts/activate        # Windows (Git Bash). En macOS y Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

Desde entonces, cada vez:

```bash
source .venv/Scripts/activate
python iniciar.py                    # página del paciente; --consola abre la consola de demostración
```

`iniciar.py` crea el `.env` si no existe (y genera la llave de cifrado), arranca la Sanitas simulada y SaniBot con
cancelaciones simuladas en segundo plano (120 por hora; `--simulador 0` las apaga) y abre **http://localhost:8000**,
la página del paciente: autorización, inicio de sesión en Sanitas, verificación de WhatsApp y activación de la búsqueda.
Todo lo demás ocurre por WhatsApp. Con `--consola` se abre **/demo**, que muestra en una sola pantalla la app, el chat de
WhatsApp y el lado de Sanitas (citas, correos y simulador). `Ctrl+C` detiene todo.

> Use siempre `localhost`, no `127.0.0.1`: la vinculación con Sanitas regresa a `localhost:8000`.

### Opción A: Docker Compose (recomendada)

Requiere Docker Desktop abierto. Los comandos sirven en Git Bash, macOS y Linux:

```bash
cp .env.example .env
# Genera la llave de cifrado (solo usa la librería estándar de Python) y la escribe en .env
KEY=$(python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
sed -i "s|^SANIBOT_TOKEN_ENCRYPTION_KEYS=.*|SANIBOT_TOKEN_ENCRYPTION_KEYS=$KEY|" .env
# Contraseña de la base de datos: escriba una propia en la línea POSTGRES_PASSWORD= del .env
docker compose up --build
```

| Servicio | URL |
|---|---|
| SaniBot | http://localhost:8000 |
| Consola de demostración | http://localhost:8000/demo |
| Simulador de WhatsApp (vista sencilla) | http://localhost:8000/dev/whatsapp |
| Portal de Sanitas (simulado) | http://localhost:8100/portal |
| Correos de Sanitas (Mailpit) | http://localhost:8025 |
| Documentación de la API | http://localhost:8000/docs y http://localhost:8100/docs |

Para encender el simulador de agenda desde el arranque: `SIMULADOR=120 docker compose up` (o use el botón de la consola).

### Opción B: sin Docker, a mano (dos terminales)

Requiere Python 3.11 o superior. En Windows (Git Bash) el entorno se activa con `source .venv/Scripts/activate`; en macOS y Linux, con `source .venv/bin/activate`.

```bash
python -m venv .venv && source .venv/Scripts/activate
pip install -e ".[dev]"
export SANIBOT_TOKEN_ENCRYPTION_KEYS=$(python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
export SANIBOT_OAUTH_TOKEN_URL=http://127.0.0.1:8100/oauth/token SANIBOT_OAUTH_USERINFO_URL=http://127.0.0.1:8100/oauth/userinfo \
       SANIBOT_OAUTH_REVOKE_URL=http://127.0.0.1:8100/oauth/revoke SANIBOT_SANITAS_API_URL=http://127.0.0.1:8100/fhir
MOCK_SANIBOT_WEBHOOK_URL=http://127.0.0.1:8000/webhooks/sanitas uvicorn mock_sanitas.app:app --port 8100 &
uvicorn sanibot.main:app_factory --factory --port 8000
```

### Pruebas

```bash
pytest                       # 63 pruebas
pytest --cov                 # con cobertura
ruff check .                 # análisis estático
```

## Guion de demostración (5 minutos)

Todo ocurre en la consola de demostración (`python iniciar.py --consola`):

1. En la columna **1 · App del paciente**: **Comenzar** → acepte el consentimiento.
2. En la pantalla de Sanitas elija `paciente1` (contraseña `sanitas123`) y **Autorizar**. SaniBot **nunca ve la contraseña**.
3. Escriba un número de WhatsApp. El código llega a la columna **2 · WhatsApp**; cópielo y verifique.
4. Elija **Buscar una fecha anterior** para la cita de Ana, marque sus horarios y active la búsqueda.
5. En la columna **3 · Sanitas**, pulse **Encender** en el simulador de agenda (o **Un evento ya**). Las cancelaciones de otros pacientes aparecen en la lista.
6. En el chat llega la oferta con los botones **Aceptar** y **Rechazar**. Pulse **Aceptar**.
7. Llega la confirmación por WhatsApp, la app del paciente muestra la nueva cita y en la columna de Sanitas aparecen los **correos de Sanitas**: la cancelación de la anterior y la confirmación de la nueva.
8. Para mostrar la baja, escriba `BAJA` en el chat: SaniBot deja de escribir y suspende las búsquedas.

> Con el simulador a 120 eventos por hora la oferta suele llegar en menos de un minuto. Si la cita de Ana es posterior a
> los cupos libres de los datos de demostración, la oferta puede llegar de inmediato.

## Arquitectura

```
Paciente ──HTTPS──▶ App web SaniBot ──▶ Backend SaniBot ──OAuth 2.0 + PKCE──▶ Proveedor OAuth de Sanitas
                                           │  ▲
           WhatsApp (bot verificado) ◀─────┤  └── webhooks firmados (HMAC) ── API de agendas Sanitas ──▶ correos de Sanitas
                                           ▼
                                      PostgreSQL (tokens cifrados, solicitudes, ofertas, auditoría)
```

- **Patrón adaptador:** toda la comunicación con Sanitas pasa por `sanibot/integrations/sanitas.py`, y con WhatsApp, por `sanibot/integrations/whatsapp.py`. Si la API cambia, solo se modifican esos archivos.
- **Eventos en lugar de sondeo:** Sanitas avisa cuando se libera un cupo o cuando una cita cambia en el portal. SaniBot no recarga ninguna página.
- **Tareas periódicas** (`services/worker.py`): expiran las ofertas sin respuesta y pasan el cupo al siguiente candidato, reintentan eventos fallidos y cancelaciones pendientes, y vencen las solicitudes que llegaron a su fecha límite.

## Trazabilidad con las historias de usuario

| Historia | Dónde está |
|---|---|
| HU01, HU02, HU18 Vinculación OAuth, consentimiento, revinculación | `services/accounts.py` (`start_link`, `complete_link`, `get_access_token`) |
| HU08 Verificar WhatsApp y opt-in; «BAJA» | `services/accounts.py` (`start_whatsapp_verification`, `confirm_whatsapp`, `opt_out`) |
| HU03–HU07 Citas, solicitudes y preferencias | `services/requests_service.py`, `api/web.py` |
| HU09, HU10, HU14 Detección, prioridad por orden de inscripción, expiración | `services/matching.py` |
| HU11, HU12, HU40 Oferta por WhatsApp con botones desde el bot verificado | `services/matching.py`, `integrations/whatsapp.py` |
| HU13 Reprogramación atómica | `services/reprogramming.py` (`reprogram`) |
| HU16 Desvincular y revocar | `services/accounts.py` (`unlink`) |
| HU17 Historial | `api/web.py` (`/historial`) |
| HU24 Auditoría inmutable | `services/audit.py`, `models.py` (`RegistroAuditoria`), `GET /admin/auditoria` |
| HU25 Interruptor de seguridad | `services/reprogramming.py`, `/admin/interruptor` |
| HU28 Tokens cifrados con rotación de llaves | `security/crypto.py` |
| HU30, HU31 Notificación rápida y no doble asignación | `matching.py` + índices únicos parciales en `models.py` |
| HU32 Privacidad de los mensajes | `services/notifier.py` (`check_privacy`) |
| HU36 Calidad del código | `.github/workflows/ci.yml`, `pyproject.toml` |
| HU41 Sincronización con el portal | `services/matching.py` (`sync_appointment`, verificación de la cita vigente antes de ofrecer) |

**Fuera del MVP** (planeadas en el cronograma): modo híbrido y deshacer (HU15), reglas de prioridad configurables (HU19), vista para el personal administrativo (HU20), inscripción por call center (HU21), conciliación completa (HU22), recordatorios (HU26), reportes (HU27, aunque `GET /admin/metricas` ya calcula los indicadores básicos) y los requisitos de infraestructura del Sprint 5 (alta disponibilidad, carga y recuperación).

## Controles de la matriz de riesgos y sus pruebas

| Riesgo | Control | Prueba |
|---|---|---|
| 3 Pérdida de eventos | Idempotencia por id de evento; reintentos del worker | `test_evento_duplicado_se_ignora`, `test_evento_que_falla_queda_pendiente_y_se_reintenta` |
| 4 Doble asignación | Índices únicos parciales + apartado (hold) del cupo en Sanitas | `test_la_base_de_datos_impide_dos_ofertas_activas_para_un_cupo`, `test_eventos_concurrentes_...` |
| 5 / 40 Pérdida de la cita original | Se reserva la nueva antes de cancelar la anterior; reintento de la cancelación | `test_si_la_reserva_falla_...`, `test_si_la_cancelacion_anterior_falla_se_reintenta` |
| 6 Tokens revocados | Suspensión, aviso por WhatsApp y reactivación al revincular | `test_token_revocado_suspende_y_la_revinculacion_reactiva` |
| 9 / 36 Robo de tokens / llave | Cifrado Fernet con rotación; nada de tokens en logs ni auditoría | `test_tokens_cifrados_en_reposo`, `test_rotacion_de_llaves` |
| 10 Suplantación en la confirmación | Solo responde el número verificado del paciente | `test_un_tercero_no_puede_responder_la_oferta` |
| 11 / 13 Phishing y datos sensibles | Sin especialidad ni enlaces en los mensajes; firma HMAC de webhooks | `test_los_mensajes_no_incluyen_...`, `test_webhook_sin_firma_valida_es_rechazado` |
| 19 / 42 WhatsApp | Si falla el envío, el cupo pasa al siguiente; «BAJA» | `test_si_whatsapp_falla_...`, `test_baja_detiene_los_mensajes` |
| 37 Reprogramación masiva | Interruptor de seguridad por umbral por minuto | `test_interruptor_detiene_reprogramaciones_masivas` |
| 43 Desincronización con el portal | Eventos de cambios + verificación de la cita vigente antes de ofrecer | `test_cancelar_en_el_portal_...`, `test_reprogramar_en_el_portal_...`, `test_si_la_cita_cambio_sin_evento_...` |

## WhatsApp real (API de WhatsApp Cloud)

La guía completa está en [`docs/GUIA_WHATSAPP_REAL.md`](docs/GUIA_WHATSAPP_REAL.md). En resumen:

1. `python -m sanibot.plantillas registrar` y luego `python -m sanibot.plantillas estado` hasta que las cinco estén aprobadas.
2. `python iniciar.py --tunel` para exponer `POST /webhooks/whatsapp` con HTTPS y registrar esa URL en Meta con el `VERIFY_TOKEN`. SaniBot verifica la firma `X-Hub-Signature-256` con el `APP_SECRET`.
3. `SANIBOT_WHATSAPP_MODE=cloud` en el `.env`. La consola de demostración muestra entonces el estado de entrega de cada mensaje (enviado, entregado, leído) y los botones se pulsan desde el celular.

En producción, el número sería la cuenta verificada que provee Sanitas con el nombre SaniBot.

## Keycloak (opcional)

`docker compose --profile keycloak up` levanta Keycloak con el realm `infra/keycloak/realm-sanitas.json`. Ese realm incluye el cliente `sanibot` con PKCE obligatorio, los permisos `citas.leer` y `citas.reprogramar`, y los usuarios `paciente1` a `paciente3`.

Para usarlo, apunte las variables `SANIBOT_OAUTH_*` a `http://localhost:8080/realms/sanitas/protocol/openid-connect/{auth,token,userinfo,revoke}`. En la Sanitas simulada configure `MOCK_AUTH_MODE=keycloak` y `MOCK_KEYCLOAK_JWKS_URL=http://keycloak:8080/realms/sanitas/protocol/openid-connect/certs`.

## Seguridad en producción

- `Settings.check_production()` impide arrancar fuera de localhost con los secretos de desarrollo o sin cookies seguras.
- Las llaves de cifrado deben vivir en un gestor de llaves (KMS). `TokenCipher` es el punto de integración y ya soporta la rotación.
- Los endpoints `/admin/*` exigen `X-Admin-Key`. En producción deben quedar detrás de la red interna o de un API Gateway con WAF.
- Las rutas `/dev/*` solo existen en el modo consola de WhatsApp.

## Limitaciones conocidas de esta versión

- La API de agendas de Sanitas es una simulación propia con forma de FHIR, no una implementación completa del estándar.
- En el entorno donde se escribió este MVP, el código, las pruebas y un recorrido completo en navegador se verificaron con SQLite. Lo siguiente está configurado, pero queda por validar en su primera ejecución: `docker compose`, PostgreSQL, el realm de Keycloak y la API real de WhatsApp Cloud.
- Las tareas periódicas corren dentro del proceso web. Para varias réplicas, conviene moverlas a un worker dedicado con bloqueo distribuido.
