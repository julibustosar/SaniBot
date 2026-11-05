# Guía: probar SaniBot con WhatsApp real

Esta guía parte de lo que ya está hecho en Meta for Developers: la app **SaniBot** con el caso de uso de WhatsApp, el
número de prueba y el `.env` con estos valores (nunca los pegue en un chat ni los suba al repositorio):

```
SANIBOT_WHATSAPP_MODE=console
SANIBOT_WHATSAPP_PHONE_NUMBER_ID=...
SANIBOT_WHATSAPP_BUSINESS_ACCOUNT_ID=...
SANIBOT_WHATSAPP_ACCESS_TOKEN=...        (token permanente del usuario del sistema)
SANIBOT_WHATSAPP_APP_SECRET=...
SANIBOT_WHATSAPP_VERIFY_TOKEN=sanibot-verify-token
SANIBOT_WHATSAPP_API_VERSION=v25.0
```

Todos los comandos se escriben en Git Bash, dentro de la carpeta del proyecto y con el entorno activo
(`source .venv/Scripts/activate`).

## Paso 1. Registrar las plantillas

WhatsApp solo deja que una empresa inicie la conversación con **plantillas aprobadas**. SaniBot usa cinco:

| Plantilla | Categoría | Para qué |
|---|---|---|
| `sanibot_verificacion` | Autenticación | Código para verificar el número (con botón «Copiar código»). |
| `sanibot_bienvenida` | Utilidad | Presenta a SaniBot como bot complementario y autorizado, y explica cómo darse de baja. |
| `sanibot_oferta_cupo` | Utilidad | Ofrece el cupo (fecha, hora, sede y minutos para responder) con los botones **Aceptar** y **Rechazar**. |
| `sanibot_confirmacion` | Utilidad | Confirma la nueva cita y recuerda que Sanitas envía los correos, como siempre. |
| `sanibot_aviso` | Utilidad | Avisos breves: oferta vencida, cupo ya tomado, búsqueda cerrada, etc. |

Ninguna incluye la especialidad, datos clínicos ni enlaces (riesgos 11 y 13).

```bash
python -m sanibot.plantillas ver          # opcional: muestra lo que se enviará a Meta
python -m sanibot.plantillas registrar
```

Meta las revisa, normalmente en minutos (a veces hasta 24 horas). Consulte el avance con:

```bash
python -m sanibot.plantillas estado
```

Cuando diga **«5 de 5 aprobadas»**, siga al paso 2. Si alguna sale **rechazada**, el comando muestra el motivo; ajuste el
texto en `PLANTILLAS` (archivo `sanibot/integrations/whatsapp.py`), cambie el nombre (por ejemplo `sanibot_aviso_v2`, porque
Meta no deja reutilizar el nombre de inmediato) y vuelva a registrar. Si Meta la **reclasifica como marketing**, igual
funciona con el número de prueba.

## Paso 2. Registrar los números que recibirán mensajes

El número de prueba de Meta solo escribe a números registrados (hasta 5).

1. En el panel de la app: **WhatsApp → Configuración de la API** (en algunos paneles, *Casos de uso → Personalizar → Configuración de la API*).
2. En **«Para»**, elija **Administrar lista de números de teléfono** y agregue su celular (y los de su equipo) con el indicativo +57.
3. Cada persona recibe un código por WhatsApp para confirmar.

Pruebe que todo está bien conectado (cuando la plantilla de bienvenida ya esté aprobada):

```bash
python -m sanibot.plantillas probar 3001234567
```

Si responde que el número no está en la lista de destinatarios permitidos, revise este paso.

## Paso 3. Instalar el túnel

Meta necesita una dirección pública con HTTPS para avisarle a SaniBot cuando el paciente pulsa **Aceptar**, **Rechazar**
o escribe **BAJA** (el *webhook*). Como SaniBot corre en su computador, se usa un túnel. Elija uno:

**Opción A: cloudflared** (sin cuenta, la más rápida). En PowerShell o en el símbolo del sistema:

```
winget install --id Cloudflare.cloudflared
```

Cierre y vuelva a abrir Git Bash. La dirección cambia cada vez que arranca, así que debe actualizarla en Meta (paso 5) en
cada sesión.

**Opción B: ngrok con dominio fijo** (requiere cuenta gratuita, pero la dirección no cambia).

1. Cree una cuenta en ngrok.com e instale ngrok (`winget install ngrok.ngrok`).
2. Copie su *authtoken* del panel de ngrok y ejecute `ngrok config add-authtoken SU_TOKEN`.
3. En el panel de ngrok, en **Domains**, reclame el dominio estático gratuito y escríbalo en el `.env`:
   `NGROK_DOMAIN=su-dominio.ngrok-free.app`

## Paso 4. Cambiar a modo cloud y arrancar

En el `.env`:

```
SANIBOT_WHATSAPP_MODE=cloud
```

Luego:

```bash
python iniciar.py --tunel
```

Al arrancar se muestra algo así:

```
  Túnel: https://xxxx.trycloudflare.com
  URL del webhook para Meta: https://xxxx.trycloudflare.com/webhooks/whatsapp
```

## Paso 5. Configurar el webhook en Meta

1. En el panel de la app: **WhatsApp → Configuración** (o *Casos de uso → Personalizar → Configuración*).
2. En **Webhook**, pulse **Editar**:
   - **URL de devolución de llamada**: la «URL del webhook para Meta» del paso 4.
   - **Token de verificación**: el valor de `SANIBOT_WHATSAPP_VERIFY_TOKEN` (por defecto `sanibot-verify-token`).
3. Pulse **Verificar y guardar**. SaniBot responde a la verificación automáticamente.
4. En **Campos del webhook**, busque **messages** y pulse **Suscribirse**.

Si el webhook quedó verificado pero no llegan las respuestas, ejecute una vez:

```bash
python -m sanibot.plantillas suscribir
```

## Paso 6. Probar el flujo completo

En la página del paciente (se abre sola en http://localhost:8000):

1. **Comenzar** → acepte la autorización → en Sanitas elija `paciente1` y **Autorizar a SaniBot**.
2. Escriba **su número real** (uno registrado en el paso 2). El código llega a su celular.
3. Pulse **Adelantar esta cita** y **Activar búsqueda**. Ya puede cerrar la página.
4. Las cancelaciones simuladas corren solas en segundo plano. En uno o dos minutos llega a su WhatsApp la oferta con los
   botones **Aceptar** y **Rechazar**. Responda desde el celular y llegará la confirmación.

Si quiere ver el estado de entrega de cada mensaje (enviado, entregado, leído), arranque con
`python iniciar.py --tunel --consola`.

Para volver a la demostración sin celular, cambie a `SANIBOT_WHATSAPP_MODE=console` y reinicie.

## Problemas frecuentes

| Mensaje | Qué hacer |
|---|---|
| «Ese número no está en la lista de destinatarios permitidos» | Paso 2. |
| «La plantilla todavía no existe o no está aprobada» | `python -m sanibot.plantillas estado` y espere la aprobación. |
| «El token de acceso de Meta venció o no es válido» | Genere de nuevo el token del usuario del sistema con los permisos `whatsapp_business_messaging` y `whatsapp_business_management`, y actualice el `.env`. |
| Meta no verifica el webhook | Revise que `python iniciar.py --tunel` siga abierto, que la URL termine en `/webhooks/whatsapp` y que el token coincida con el `.env`. |
| Pulsé Aceptar en el celular y no pasa nada | El túnel cambió de dirección (cloudflared): actualice la URL en el paso 5. También puede ejecutar `python -m sanibot.plantillas suscribir`. |
| «Firma inválida» en la terminal | El `SANIBOT_WHATSAPP_APP_SECRET` no corresponde a la app. Cópielo de nuevo desde **Configuración de la app → Básica**. |

## Seguridad

- El token de acceso y el App Secret van **solo** en el `.env`, que está excluido del repositorio.
- Si alguno aparece en una captura de pantalla o se comparte por error, genérelo de nuevo en Meta.
- En producción, el número sería la cuenta verificada que provee Sanitas con el nombre SaniBot, y los pacientes la
  conocerían antes por el aviso de la EPS.
