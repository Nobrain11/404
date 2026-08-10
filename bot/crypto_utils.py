"""Wallet creation, import from seed/key, Fernet encryption. Keys NEVER logged."""
from __future__ import annotations

import secrets
import string

from cryptography.fernet import Fernet, InvalidToken
from eth_account import Account
from eth_account.hdaccount import generate_mnemonic


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


def create_wallet() -> tuple[str, str, str]:
    """
    Returns (address, private_key, mnemonic).
    All are sensitive — private_key and mnemonic are NEVER logged.
    """
    Account.enable_unaudited_hdwallet_features()
    mnemonic = generate_mnemonic(num_words=12, lang="english")
    account = Account.from_mnemonic(mnemonic)
    return account.address, account.key.hex(), mnemonic


def import_from_private_key(private_key: str) -> tuple[str, str]:
    """Import wallet from hex private key. Returns (address, private_key)."""
    pk = private_key.strip()
    if not pk.startswith("0x"):
        pk = "0x" + pk
    account = Account.from_key(pk)
    return account.address, pk


def import_from_mnemonic(mnemonic: str) -> tuple[str, str]:
    """Import wallet from 12/24-word seed phrase. Returns (address, private_key)."""
    Account.enable_unaudited_hdwallet_features()
    account = Account.from_mnemonic(mnemonic.strip())
    return account.address, account.key.hex()


def make_referral_code(telegram_id: int) -> str:
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(5))
    return f"ERR{str(telegram_id)[-3:]}{suffix}"


def short_addr(address: str) -> str:
    if not address or len(address) < 12:
        return address or "unknown"
    return f"{address[:6]}...{address[-4:]}"
