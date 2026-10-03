import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet

from .store import DATA

keyfile = DATA / "encryption.key"
if not keyfile.exists():
    fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(Fernet.generate_key())
fernet = Fernet(keyfile.read_bytes())


def encrypt(s):
    return fernet.encrypt(s.encode()).decode() if s else ""


def decrypt(s):
    return fernet.decrypt(s.encode()).decode() if s else ""


def digest(s):
    return hashlib.sha256(s.encode()).hexdigest()


def password_hash(s):
    salt = secrets.token_hex(16)
    return salt + ":" + hashlib.pbkdf2_hmac("sha256", s.encode(), salt.encode(), 300000).hex()


def password_ok(s, h):
    salt, value = h.split(":")
    return hmac.compare_digest(
        value, hashlib.pbkdf2_hmac("sha256", s.encode(), salt.encode(), 300000).hex()
    )


def public(value):
    if isinstance(value, list):
        return [public(x) for x in value]
    if isinstance(value, dict):
        return {
            k: public(v)
            for k, v in value.items()
            if k not in ("secret", "api_key", "token", "path", "image", "headers_secret")
        }
    return value
