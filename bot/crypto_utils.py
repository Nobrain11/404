"""Wallet creation, Fernet encryption. Keys NEVER logged."""
from __future__ import annotations

import secrets
import string

from cryptography.fernet import Fernet, InvalidToken
from eth_account import Account


def generate_encryption_key() -> str:
    return Fernet.generate_key().decode()


def _fernet(key: str) -> Fernet:
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plaintext: str, key: str) -> str:
    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str, key: str) -> str:
    try:
        return _fernet(key).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("ENCRYPTION_KEY mismatch") from exc


def create_wallet() -> tuple[str, str]:
    account = Account.create(secrets.token_hex(32))
    return account.address, account.key.hex()


def make_referral_code(telegram_id: int) -> str:
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(5))
    return f"ERR{str(telegram_id)[-3:]}{suffix}"


def short_addr(address: str) -> str:
    if not address or len(address) < 12:
        return address or "unknown"
    return f"{address[:6]}...{address[-4:]}"
