# Guía: ejecutar SaniBot con WhatsApp

SaniBot funciona con la API de WhatsApp Cloud de Meta. El paciente usa una página web solo para autorizar sus datos,
iniciar sesión en Sanitas, verificar su WhatsApp y activar la búsqueda; todo lo demás ocurre en su WhatsApp.

## Qué se necesita (una sola vez)

1. **El archivo `.env`** con los datos de Meta (nunca los pegue en un chat ni los suba al repositorio):

   ```
   SANIBOT_WHATSAPP_PHONE_NUMBER_ID=...      «Identificador del número de teléfono» (unos 15-16 dígitos)
   SANIBOT_WHATSAPP_NUMERO_VISIBLE=...       el número de prueba de Meta, campo «De», solo dígitos (p. ej. 15551682297)
   SANIBOT_WHATSAPP_BUSINESS_ACCOUNT_ID=...  «Identificador de la cuenta de WhatsApp Business»
   SANIBOT_WHATSAPP_ACCESS_TOKEN=...         token permanente del usuario del sistema
   SANIBOT_WHATSAPP_APP_SECRET=...           Configuración de la app > Básica > Clave secreta
   SANIBOT_WHATSAPP_VERIFY_TOKEN=sanibot-verify-token
   SANIBOT_WHATSAPP_API_VERSION=v25.0
   ```

   Los tres primeros están en Meta for Developers, en **WhatsApp → Configuración de la API**.

2. **Su celular autorizado.** El número de prueba de Meta solo escribe a números registrados (hasta 5). En
   **WhatsApp → Configuración de la API**, en el campo **«Para»**, elija **Administrar lista de números de teléfono**,
   agregue su celular con +57 y confírmelo con el código que le llega.

3. **El túnel.** Meta necesita una dirección pública para entregarle a SaniBot los mensajes del paciente. En PowerShell:

   ```
   winget install --id Cloudflare.cloudflared
   ```

   Cierre y vuelva a abrir Git Bash.

4. **Las plantillas enviadas a revisión** (opcional para la demostración, ver «Plantillas» más abajo):

   ```bash
   python -m sanibot.plantillas registrar
   ```

## Cada vez que se usa

Todo en Git Bash, dentro de la carpeta del proyecto:

```bash
cd ~/Documents/sanibot-mvp
source .venv/Scripts/activate
python iniciar.py
```

El programa revisa el `.env`, arranca SaniBot y la Sanitas simulada, abre el túnel y muestra algo así:

```
  >> Pega esta URL en Meta (WhatsApp > Configuración > Webhook > Editar):
     https://xxxx.trycloudflare.com/webhooks/whatsapp
     Token de verificación: sanibot-verify-token
```

**Conectar el webhook** (cada vez, porque la dirección del túnel cambia en cada arranque):

1. Meta for Developers → su app → **WhatsApp → Configuración**.
2. En **Webhook**, **Editar**: pegue la URL y el token de verificación, y pulse **Verificar y guardar**.
3. La primera vez, en **Campos del webhook**, busque **messages** y pulse **Suscribirse**.

Para detener SaniBot: en otra ventana de Git Bash, `python iniciar.py --detener`.

## El flujo del paciente

1. En la página que se abrió (http://localhost:8000) pulse **Activar SaniBot**, acepte la autorización y entre a Sanitas
   con `paciente1` (contraseña `sanitas123`).
2. Escriba **su número de celular** (el que autorizó en Meta) y pulse **Continuar**.
3. La página muestra un mensaje como **`SANIBOT 482913`**. **Envíelo desde su celular al WhatsApp de SaniBot**:
   - en el celular: pulse **Abrir WhatsApp con el mensaje listo** y luego enviar;
   - desde el computador: en el WhatsApp de su celular, inicie un chat con el número de SaniBot (el de
     `SANIBOT_WHATSAPP_NUMERO_VISIBLE`, por ejemplo +1 555 168 2297) y escriba el mensaje tal cual.
   La página avanza sola cuando SaniBot lo recibe, y le llega la bienvenida por WhatsApp.
4. Pulse **Adelantar esta cita** y **Activar búsqueda**. Ya puede cerrar la página.
5. Las cancelaciones simuladas corren solas. En uno o dos minutos le llega por WhatsApp la oferta con los botones
   **Aceptar** y **Rechazar**. Al aceptar, le llega la confirmación.
6. Para darse de baja, escriba **BAJA** en el chat.

## Plantillas

WhatsApp exige plantillas aprobadas solo cuando la empresa escribe primero. Como el paciente le escribe primero a
SaniBot al verificar su número, durante las 24 horas siguientes SaniBot puede responderle con mensajes normales (también
con botones). Si una plantilla todavía no está aprobada, SaniBot envía el mismo texto de esa forma, así que **la
demostración funciona aunque Meta no haya terminado de revisar**. Para que la ventana esté abierta, haga la verificación
del número el mismo día de la demostración (cualquier mensaje suyo al bot la renueva).

Consulte la revisión con `python -m sanibot.plantillas estado`. Que Meta reclasifique una plantilla como MARKETING no
impide usarla.

## Problemas frecuentes

| Qué pasa | Qué hacer |
|---|---|
| «Falta…» o «parece un número de teléfono» al arrancar | Corrija esa línea del `.env` como indica el mensaje. |
| La página se queda «Esperando tu mensaje» | Revise que el webhook en Meta tenga la URL del túnel **de este arranque** y esté suscrito a **messages**; que envió el mensaje desde el mismo número que escribió en la página; y que no pasaron más de 10 minutos. |
| «Ese número no está en la lista de destinatarios permitidos» | Autorice su celular en Meta (punto 2 de la primera sección). |
| «El token de acceso de Meta venció o no es válido» | Genere de nuevo el token del usuario del sistema y actualice el `.env`. |
| «Firma inválida» en la terminal | El App Secret del `.env` no corresponde a la app. Cópielo de nuevo. |
| «El puerto 8000 ya está en uso» | `python iniciar.py --detener` y vuelva a arrancar. |

## Seguridad

- El token de acceso y el App Secret van **solo** en el `.env`, que está excluido del repositorio.
- Si alguno aparece en una captura de pantalla o se comparte por error, genérelo de nuevo en Meta.
- En producción, el número sería la cuenta verificada que provee Sanitas con el nombre SaniBot, y los pacientes la
  conocerían antes por el aviso de la EPS.
