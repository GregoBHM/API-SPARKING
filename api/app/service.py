from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Activation, AuditLog, License, Product, utcnow
from .security import hash_fingerprint, hash_license_key

settings = get_settings()


class ValidationResult:
    def __init__(self, status: str, message: str, license_obj: License | None = None, activation: Activation | None = None):
        self.status = status
        self.message = message
        self.license = license_obj
        self.activation = activation

    @property
    def valid(self) -> bool:
        return self.status == "VALID"


def log_event(
    db: Session,
    action: str,
    ip: str | None,
    license_id=None,
    activation_id=None,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            action=action,
            ip=ip,
            license_id=license_id,
            activation_id=activation_id,
            details=details or {},
        )
    )


def _load_license(db: Session, license_key: str) -> License | None:
    return db.scalar(select(License).where(License.key_hash == hash_license_key(license_key)))


def _check_license_and_product(db: Session, license_key: str, product_slug: str) -> ValidationResult:
    lic = _load_license(db, license_key)
    if not lic:
        return ValidationResult("INVALID_LICENSE", "License key not found")

    if lic.status != "ACTIVE":
        return ValidationResult("LICENSE_DISABLED", "License is disabled", lic)

    now = utcnow()
    if lic.expires_at and lic.expires_at <= now:
        return ValidationResult("LICENSE_EXPIRED", "License has expired", lic)

    product = db.scalar(select(Product).where(Product.slug == product_slug))
    if not product or not product.active:
        return ValidationResult("INVALID_PRODUCT", "Product is not available", lic)

    if all(p.id != product.id for p in lic.products):
        return ValidationResult("INVALID_PRODUCT", "License does not include this product", lic)

    return ValidationResult("VALID", "License and product are valid", lic)


def _find_activation(db: Session, lic: License, server_id: str) -> Activation | None:
    return db.scalar(
        select(Activation).where(Activation.license_id == lic.id, Activation.server_id == server_id)
    )


def _active_count(db: Session, lic: License) -> int:
    return int(
        db.scalar(
            select(func.count(Activation.id)).where(
                Activation.license_id == lic.id,
                Activation.status == "ACTIVE",
            )
        )
        or 0
    )


def activate(db: Session, req, ip: str | None) -> ValidationResult:
    base = _check_license_and_product(db, req.license_key, req.product)
    if not base.valid:
        log_event(db, "ACTIVATION_REJECTED", ip, getattr(base.license, "id", None), details={"status": base.status, "product": req.product})
        db.commit()
        return base

    lic = base.license
    fingerprint_hash = hash_fingerprint(req.fingerprint)
    activation = _find_activation(db, lic, req.server_id)

    if activation and activation.status == "ACTIVE":
        if settings.activation_bind_fingerprint and activation.fingerprint_hash != fingerprint_hash:
            result = ValidationResult("FINGERPRINT_MISMATCH", "Server fingerprint does not match", lic, activation)
            log_event(db, "ACTIVATION_REJECTED", ip, lic.id, activation.id, {"status": result.status, "server_id": req.server_id})
            db.commit()
            return result

        activation.last_seen = utcnow()
        activation.last_ip = ip
        activation.plugin_version = req.plugin_version
        activation.minecraft_version = req.minecraft_version
        activation.last_product_slug = req.product
        log_event(db, "ACTIVATION_REUSED", ip, lic.id, activation.id, {"product": req.product})
        db.commit()
        return ValidationResult("VALID", "Existing activation is valid", lic, activation)

    if _active_count(db, lic) >= lic.max_activations:
        result = ValidationResult("ACTIVATION_LIMIT", "Maximum number of active servers reached", lic)
        log_event(db, "ACTIVATION_REJECTED", ip, lic.id, details={"status": result.status, "server_id": req.server_id})
        db.commit()
        return result

    if activation:
        activation.status = "ACTIVE"
        activation.fingerprint_hash = fingerprint_hash
        activation.last_seen = utcnow()
        activation.last_ip = ip
        activation.plugin_version = req.plugin_version
        activation.minecraft_version = req.minecraft_version
        activation.last_product_slug = req.product
    else:
        activation = Activation(
            license_id=lic.id,
            server_id=req.server_id,
            fingerprint_hash=fingerprint_hash,
            status="ACTIVE",
            first_seen=utcnow(),
            last_seen=utcnow(),
            last_ip=ip,
            plugin_version=req.plugin_version,
            minecraft_version=req.minecraft_version,
            last_product_slug=req.product,
        )
        db.add(activation)
        db.flush()

    log_event(db, "LICENSE_ACTIVATED", ip, lic.id, activation.id, {"product": req.product, "server_id": req.server_id})
    db.commit()
    return ValidationResult("VALID", "License activated", lic, activation)


def verify(db: Session, req, ip: str | None, event_name: str = "LICENSE_VERIFIED") -> ValidationResult:
    base = _check_license_and_product(db, req.license_key, req.product)
    if not base.valid:
        log_event(db, "VERIFY_REJECTED", ip, getattr(base.license, "id", None), details={"status": base.status, "product": req.product})
        db.commit()
        return base

    lic = base.license
    activation = _find_activation(db, lic, req.server_id)
    if not activation or activation.status != "ACTIVE":
        result = ValidationResult("ACTIVATION_REQUIRED", "This server is not activated", lic)
        log_event(db, "VERIFY_REJECTED", ip, lic.id, details={"status": result.status, "server_id": req.server_id})
        db.commit()
        return result

    fingerprint_hash = hash_fingerprint(req.fingerprint)
    if settings.activation_bind_fingerprint and activation.fingerprint_hash != fingerprint_hash:
        result = ValidationResult("FINGERPRINT_MISMATCH", "Server fingerprint does not match", lic, activation)
        log_event(db, "VERIFY_REJECTED", ip, lic.id, activation.id, {"status": result.status})
        db.commit()
        return result

    activation.last_seen = utcnow()
    activation.last_ip = ip
    activation.plugin_version = req.plugin_version
    activation.minecraft_version = req.minecraft_version
    activation.last_product_slug = req.product
    log_event(db, event_name, ip, lic.id, activation.id, {"product": req.product})
    db.commit()
    return ValidationResult("VALID", "License is valid", lic, activation)


def deactivate(db: Session, req, ip: str | None) -> ValidationResult:
    base = _check_license_and_product(db, req.license_key, req.product)
    if not base.valid:
        return base

    lic = base.license
    activation = _find_activation(db, lic, req.server_id)
    if not activation or activation.status != "ACTIVE":
        return ValidationResult("ACTIVATION_REQUIRED", "No active activation found", lic)

    activation.status = "INACTIVE"
    activation.last_seen = utcnow()
    activation.last_ip = ip
    log_event(db, "LICENSE_DEACTIVATED", ip, lic.id, activation.id, {"server_id": req.server_id})
    db.commit()
    return ValidationResult("VALID", "Activation released", lic, activation)


def signed_payload(req, result: ValidationResult) -> dict:
    now = datetime.now(timezone.utc)
    issued_at = int(now.timestamp())
    valid_until = issued_at + 60
    expires_at = None

    if result.valid and result.license:
        valid_until = issued_at + result.license.offline_grace_hours * 3600
        if result.license.expires_at:
            expires_at = int(result.license.expires_at.timestamp())
            valid_until = min(valid_until, expires_at)

    return {
        "ok": result.valid,
        "status": result.status,
        "message": result.message,
        "license_id": str(result.license.id) if result.license else None,
        "activation_id": str(result.activation.id) if result.activation else None,
        "product": req.product,
        "server_id": req.server_id,
        "nonce": req.nonce,
        "issued_at": issued_at,
        "valid_until": valid_until,
        "expires_at": expires_at,
    }
