"""Proteção local de segredos usando o perfil do usuário do Windows."""

from __future__ import annotations

import base64
import hashlib
from itertools import cycle


PREFIX = "dpapi:"


def protect_secret(value: str) -> str:
    if not value:
        return ""
    if value.startswith(PREFIX):
        return value
    try:
        import win32crypt
    except ImportError as exc:
        raise RuntimeError("O pacote pywin32 é necessário para proteger credenciais") from exc
    result = win32crypt.CryptProtectData(
        value.encode("utf-8"), "Gerenciador Firebird", None, None, None, 0
    )
    encrypted = result[1] if isinstance(result, tuple) else result
    return PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect_secret(value: str, legacy_key: str = "firebird_manager_key") -> str:
    if not value:
        return ""
    if value.startswith(PREFIX):
        try:
            import win32crypt
        except ImportError as exc:
            raise RuntimeError("O pacote pywin32 é necessário para ler credenciais") from exc
        encrypted = base64.b64decode(value[len(PREFIX):], validate=True)
        result = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)
        decrypted = result[1] if isinstance(result, tuple) else result
        return decrypted.decode("utf-8")
    return _decrypt_legacy(value, legacy_key)


def _decrypt_legacy(value: str, key: str) -> str:
    """Lê valores das versões antigas para permitir migração transparente."""
    try:
        from cryptography.fernet import Fernet

        fernet_key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
        return Fernet(fernet_key).decrypt(value.encode()).decode()
    except Exception:
        decoded = base64.b64decode(value.encode()).decode()
        xored = "".join(chr(ord(char) ^ ord(secret)) for char, secret in zip(decoded, cycle(key)))
        return base64.b64decode(xored.encode()).decode()
