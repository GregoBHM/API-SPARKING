import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .config import get_settings

settings = get_settings()
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalize_license_key(value: str) -> str:
    return value.strip().upper().replace(" ", "")


def hash_license_key(value: str) -> str:
    normalized = normalize_license_key(value)
    return hmac.new(
        settings.license_hash_secret.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def hash_fingerprint(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def hash_admin_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_license_key() -> str:
    groups = ["".join(secrets.choice(ALPHABET) for _ in range(5)) for _ in range(4)]
    return "SPARK-" + "-".join(groups)


def generate_admin_api_key() -> str:
    return "spark_admin_" + secrets.token_urlsafe(40)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class ResponseSigner:
    def __init__(self, key_dir: str):
        self.key_dir = Path(key_dir)
        self.private_path = self.key_dir / "private.pem"
        self.public_path = self.key_dir / "public.pem"
        self.key_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_keys()
        self.private_key = serialization.load_pem_private_key(self.private_path.read_bytes(), password=None)
        self.public_pem = self.public_path.read_text("utf-8")

    def _ensure_keys(self) -> None:
        if self.private_path.exists() and self.public_path.exists():
            return

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        self.private_path.write_bytes(private_bytes)
        self.public_path.write_bytes(public_bytes)
        try:
            os.chmod(self.private_path, 0o600)
            os.chmod(self.public_path, 0o644)
        except OSError:
            pass

    def sign_payload(self, payload: dict) -> dict:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        token = _b64url(canonical)
        signature = self.private_key.sign(
            token.encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return {"token": token, "signature": _b64url(signature), "algorithm": "RS256"}


signer = ResponseSigner(settings.signing_key_dir)
