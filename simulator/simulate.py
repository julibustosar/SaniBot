"""Simulador de cancelaciones y aperturas de agenda contra una Sanitas simulada que ya está corriendo.

Normalmente no hace falta: el simulador viene integrado en la Sanitas simulada y se enciende desde la consola
de demostración (/demo) o con «python iniciar.py --simulador 120». Este comando lo enciende por HTTP.

Uso:  python -m simulator.simulate --url http://localhost:8100 --cancelaciones-por-hora 120
      python -m simulator.simulate --detener
"""

import argparse
from urllib.parse import urlparse

import httpx

HOSTS_PERMITIDOS = {"localhost", "127.0.0.1", "sanitas"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8100")
    ap.add_argument("--cancelaciones-por-hora", type=float, default=120, help="eventos por hora (Poisson)")
    ap.add_argument("--detener", action="store_true")
    args = ap.parse_args()
    url = urlparse(args.url)
    if url.scheme not in ("http", "https") or url.hostname not in HOSTS_PERMITIDOS:
        ap.error(f"--url debe apuntar a la Sanitas simulada ({', '.join(sorted(HOSTS_PERMITIDOS))})")
    accion = "detener" if args.detener else "iniciar"
    r = httpx.post(f"{args.url}/portal/simulador", data={"accion": accion,
                                                         "eventos_por_hora": args.cancelaciones_por_hora}, timeout=10)
    r.raise_for_status()
    print(r.json())


if __name__ == "__main__":
    main()
