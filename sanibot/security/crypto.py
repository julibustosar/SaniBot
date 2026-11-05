"""Cifrado de tokens en reposo (RNF01 / riesgos 9 y 36).

En producción la llave debe vivir en un gestor de llaves (KMS). Esta clase es el punto de integración:
cifra con la primera llave y descifra con cualquiera de la lista, lo que permite rotar llaves sin perder datos.
"""

import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class TokenCipher:
    def __init__(self, keys_csv: str):
        keys = [k.strip() for k in keys_csv.split(",") if k.strip()]
        if not keys:
            raise ValueError(
                "Falta SANIBOT_TOKEN_ENCRYPTION_KEYS. Genere una llave con: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:  # llave perdida o dato alterado
            raise ValueError("No fue posible descifrar el token") from exc

    def rotate(self, value: str) -> str:
        return self._fernet.rotate(value.encode()).decode()


def hash_code(code: str, secret: str) -> str:
    return hmac.new(secret.encode(), code.encode(), hashlib.sha256).hexdigest()


def generate_code(digits: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(digits))
