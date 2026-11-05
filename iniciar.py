"""Arranca SaniBot con un solo comando (Windows, macOS y Linux).

    python iniciar.py                    # SaniBot + Sanitas simulada + túnel para el webhook de WhatsApp
    python iniciar.py --detener          # detiene todo si quedó corriendo
    python iniciar.py --simulador 0      # sin cancelaciones automáticas (por defecto: 120 por hora, en segundo plano)

Lee el archivo .env (si no existe, lo crea a partir de .env.example y genera la llave de cifrado).
Ctrl+C detiene todo.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
ENV = RAIZ / ".env"
PUERTO_SANIBOT, PUERTO_SANITAS = 8000, 8100


def leer_env(path: Path) -> dict[str, str]:
    datos: dict[str, str] = {}
    if not path.exists():
        return datos
    for linea in path.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        k, v = linea.split("=", 1)
        datos[k.strip()] = v.strip().strip('"').strip("'")
    return datos


def asegurar_env() -> dict[str, str]:
    if not ENV.exists():
        ejemplo = RAIZ / ".env.example"
        if ejemplo.exists():
            shutil.copyfile(ejemplo, ENV)
        else:
            ENV.touch()
        print("· Creé el archivo .env a partir de .env.example")
    datos = leer_env(ENV)
    if not datos.get("SANIBOT_TOKEN_ENCRYPTION_KEYS"):
        # Solo se agrega una línea al final (la última aparición de una variable es la que vale);
        # el contenido del archivo nunca se vuelve a escribir.
        llave = base64.urlsafe_b64encode(os.urandom(32)).decode()
        with open(ENV, "a", encoding="utf-8") as f:
            f.write(f"\nSANIBOT_TOKEN_ENCRYPTION_KEYS={llave}\n")
        datos["SANIBOT_TOKEN_ENCRYPTION_KEYS"] = llave
        print("· Generé la llave de cifrado y la guardé en .env (consérvala: cifra los tokens guardados)")
    return datos


def entorno(datos: dict[str, str], simulador: float) -> dict[str, str]:
    env = {**os.environ, **datos, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    sanitas = f"http://127.0.0.1:{PUERTO_SANITAS}"
    defaults = {
        "SANIBOT_BASE_URL": f"http://localhost:{PUERTO_SANIBOT}",
        "SANIBOT_OAUTH_AUTHORIZE_URL": f"http://localhost:{PUERTO_SANITAS}/oauth/authorize",
        "SANIBOT_OAUTH_TOKEN_URL": f"{sanitas}/oauth/token",
        "SANIBOT_OAUTH_USERINFO_URL": f"{sanitas}/oauth/userinfo",
        "SANIBOT_OAUTH_REVOKE_URL": f"{sanitas}/oauth/revoke",
        "SANIBOT_SANITAS_API_URL": f"{sanitas}/fhir",
    }
    for k, v in defaults.items():
        env.setdefault(k, v)
    # La Sanitas simulada debe compartir los secretos con SaniBot.
    env["MOCK_CLIENT_SECRET"] = env.get("SANIBOT_OAUTH_CLIENT_SECRET", "sanibot-secret")
    env["MOCK_SERVICE_KEY"] = env.get("SANIBOT_SANITAS_SERVICE_KEY", "clave-servicio-sanibot")
    env["MOCK_WEBHOOK_SECRET"] = env.get("SANIBOT_SANITAS_WEBHOOK_SECRET", "secreto-webhook-sanitas")
    env["MOCK_SANIBOT_WEBHOOK_URL"] = f"http://127.0.0.1:{PUERTO_SANIBOT}/webhooks/sanitas"
    env["MOCK_REDIRECT_URIS"] = f"{env['SANIBOT_BASE_URL']}/auth/callback"
    env["MOCK_SIMULADOR_EVENTOS_POR_HORA"] = str(simulador)
    return env


def puerto_ocupado(puerto: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", puerto)) == 0


def esperar(url: str, segundos: int = 40) -> bool:
    fin = time.time() + segundos
    while time.time() < fin:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 - URL local fija
                if r.status == 200:
                    return True
        except OSError:
            time.sleep(0.5)
    return False


def abrir_tunel(procesos: list) -> str | None:
    """Abre un túnel HTTPS hacia SaniBot con cloudflared o ngrok (el que esté instalado)."""
    if shutil.which("cloudflared"):
        p = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://localhost:{PUERTO_SANIBOT}"],  # noqa: S603,S607
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        procesos.append(p)
        fin = time.time() + 30
        while time.time() < fin and p.stdout:
            linea = p.stdout.readline()
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", linea)
            if m:
                # seguir leyendo la salida en segundo plano para que cloudflared no se bloquee
                threading.Thread(target=lambda: [None for _ in p.stdout], daemon=True).start()
                return m.group(0)
        return None
    if shutil.which("ngrok"):
        dominio = os.environ.get("NGROK_DOMAIN") or leer_env(ENV).get("NGROK_DOMAIN")
        cmd = ["ngrok", "http", str(PUERTO_SANIBOT), "--log", "stdout"] + ([f"--url={dominio}"] if dominio else [])
        procesos.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))  # noqa: S603
        fin = time.time() + 20
        while time.time() < fin:
            try:
                with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:  # noqa: S310
                    tuneles = json.load(r).get("tunnels", [])
                    https = [t["public_url"] for t in tuneles if t["public_url"].startswith("https")]
                    if https:
                        return https[0]
            except OSError:
                pass
            time.sleep(0.7)
        return None
    print("! No encontré cloudflared ni ngrok instalados. Revisa docs/GUIA_WHATSAPP_REAL.md, paso 4.")
    return None


def pids_en_puerto(puerto: int) -> set[int]:
    """Procesos que escuchan en un puerto local (Windows: netstat; macOS y Linux: lsof)."""
    try:
        if os.name == "nt":
            salida = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,  # noqa: S603,S607
                                    check=False).stdout
            return {int(c[-1]) for c in (linea.split() for linea in salida.splitlines())
                    if len(c) >= 5 and c[1].endswith(f":{puerto}") and c[2] in ("0.0.0.0:0", "[::]:0")
                    and c[-1].isdigit()}  # dirección remota vacía = en escucha (el estado varía según el idioma)
        salida = subprocess.run(["lsof", "-t", f"-iTCP:{puerto}", "-sTCP:LISTEN"], capture_output=True,  # noqa: S603,S607
                                text=True, check=False).stdout
        return {int(x) for x in salida.split() if x.isdigit()}
    except OSError:
        return set()


def detener() -> int:
    encontrados = {pid for puerto in (PUERTO_SANIBOT, PUERTO_SANITAS) for pid in pids_en_puerto(puerto)}
    encontrados.discard(os.getpid())
    if not encontrados:
        print("· No había nada corriendo en los puertos 8000 y 8100.")
        return 0
    for pid in encontrados:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)  # noqa: S603,S607
        else:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    time.sleep(1)
    quedan = [p for p in (PUERTO_SANIBOT, PUERTO_SANITAS) if puerto_ocupado(p)]
    print("· SaniBot detenido." if not quedan else f"! Siguen ocupados los puertos {quedan}.")
    return 0 if not quedan else 1


def revisar_config(env: dict[str, str]) -> bool:
    """Detecta errores frecuentes del .env antes de arrancar."""
    problemas = []
    pnid = env.get("SANIBOT_WHATSAPP_PHONE_NUMBER_ID", "")
    if not pnid:
        problemas.append("Falta SANIBOT_WHATSAPP_PHONE_NUMBER_ID (Meta: WhatsApp > Configuración de la API).")
    elif pnid.startswith("1555") or len(pnid) < 14:
        problemas.append("SANIBOT_WHATSAPP_PHONE_NUMBER_ID parece un número de teléfono. Va el «Identificador del número"
                         " de teléfono» (unos 15-16 dígitos); el teléfono va en SANIBOT_WHATSAPP_NUMERO_VISIBLE.")
    if not env.get("SANIBOT_WHATSAPP_ACCESS_TOKEN"):
        problemas.append("Falta SANIBOT_WHATSAPP_ACCESS_TOKEN.")
    if not env.get("SANIBOT_WHATSAPP_APP_SECRET"):
        problemas.append("Falta SANIBOT_WHATSAPP_APP_SECRET.")
    if not env.get("SANIBOT_WHATSAPP_NUMERO_VISIBLE"):
        problemas.append("Falta SANIBOT_WHATSAPP_NUMERO_VISIBLE: el número de prueba de Meta (campo «De»), solo dígitos,"
                         " por ejemplo 15551682297.")
    for p in problemas:
        print(f"! {p}")
    return not problemas


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simulador", type=float, default=120, metavar="EVENTOS_POR_HORA",
                    help="cancelaciones y aperturas simuladas por hora, en segundo plano (0 = apagado)")
    ap.add_argument("--sin-tunel", action="store_true", help="no abrir el túnel (WhatsApp no podrá avisar a SaniBot)")
    ap.add_argument("--tunel", action="store_true", help=argparse.SUPPRESS)  # el túnel ya es lo normal
    ap.add_argument("--sin-navegador", action="store_true", help="no abrir el navegador")
    ap.add_argument("--detener", action="store_true", help="detiene SaniBot y la Sanitas simulada si quedaron corriendo")
    args = ap.parse_args()
    if args.detener:
        return detener()
    os.chdir(RAIZ)

    for puerto in (PUERTO_SANIBOT, PUERTO_SANITAS):
        if puerto_ocupado(puerto):
            print(f"! El puerto {puerto} ya está en uso: SaniBot sigue corriendo de una vez anterior.\n"
                  "  Ejecuta «python iniciar.py --detener» y vuelve a intentarlo.")
            return 1

    datos = asegurar_env()
    env = entorno(datos, args.simulador)
    if not revisar_config(env):
        return 1
    py = sys.executable
    procesos: list[subprocess.Popen] = []
    try:
        procesos.append(subprocess.Popen([py, "-m", "uvicorn", "mock_sanitas.app:app", "--host", "127.0.0.1",  # noqa: S603
                                          "--port", str(PUERTO_SANITAS), "--no-access-log"], env=env))
        procesos.append(subprocess.Popen([py, "-m", "uvicorn", "sanibot.main:app_factory", "--factory",  # noqa: S603
                                          "--host", "127.0.0.1", "--port", str(PUERTO_SANIBOT), "--no-access-log"],
                                         env=env))
        ok = esperar(f"http://127.0.0.1:{PUERTO_SANITAS}/health") and esperar(f"http://127.0.0.1:{PUERTO_SANIBOT}/health")
        if not ok or any(p.poll() is not None for p in procesos):
            print("! No arrancaron los servidores. Revisa los mensajes de error de arriba.")
            return 1
        tunel = None if args.sin_tunel else abrir_tunel(procesos)
        demo = f"http://localhost:{PUERTO_SANIBOT}/"
        print("\n" + "=" * 68)
        print(f"  SaniBot listo.  Abre: {demo}")
        print(f"  WhatsApp de SaniBot: +{env.get('SANIBOT_WHATSAPP_NUMERO_VISIBLE', '')}"
              f"   ·   Cancelaciones simuladas: {f'{args.simulador:g} por hora' if args.simulador else 'apagadas'}")
        if tunel:
            print("\n  >> Pega esta URL en Meta (WhatsApp > Configuración > Webhook > Editar):")
            print(f"     {tunel}/webhooks/whatsapp")
            print(f"     Token de verificación: {env.get('SANIBOT_WHATSAPP_VERIFY_TOKEN', 'sanibot-verify-token')}\n")
        else:
            print("  ! Sin túnel: WhatsApp no podrá enviarle a SaniBot tus mensajes ni tus respuestas.")
        print("  Usuarios de prueba: paciente1, paciente2, paciente3 · contraseña sanitas123")
        print("  Para detener: Ctrl+C aquí, o en otra terminal «python iniciar.py --detener».")
        print("=" * 68 + "\n")
        if not args.sin_navegador:
            webbrowser.open(demo)
        while all(p.poll() is None for p in procesos[:2]):
            time.sleep(1)
        print("! Uno de los servidores se detuvo.")
        return 1
    except KeyboardInterrupt:
        print("\nDeteniendo…")
        return 0
    finally:
        for p in procesos:
            if p.poll() is None:
                p.terminate()
        for p in procesos:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
