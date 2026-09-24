import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import AdminPrincipal, require_admin
from .config import get_settings
from .db import Base, engine, get_db
from .models import Activation, AdminApiKey, AuditLog, Customer, License, Product, utcnow
from .schemas import (
    ActivationOut,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    CustomerCreate,
    CustomerDiscordUpsert,
    CustomerOut,
    LicenseCreate,
    LicenseCreated,
    LicenseOut,
    LicenseRequest,
    LicenseUpdate,
    ProductCreate,
    ProductMigrate,
    ProductOut,
    ProductUpdate,
    SignedResponse,
)
from .security import generate_admin_api_key, generate_license_key, hash_admin_token, hash_license_key, signer
from .service import activate, deactivate, log_event, signed_payload, verify

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Sparking License API",
    version="2.0.0",
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
    lifespan=lifespan,
)


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else None


def to_license_out(db: Session, lic: License) -> LicenseOut:
    active_count = int(
        db.scalar(
            select(func.count(Activation.id)).where(
                Activation.license_id == lic.id,
                Activation.status == "ACTIVE",
            )
        )
        or 0
    )
    return LicenseOut(
        id=lic.id,
        masked_key=f"SPARK-****-****-****-{lic.key_last4}",
        status=lic.status,
        license_type=lic.license_type,
        max_activations=lic.max_activations,
        active_activations=active_count,
        offline_grace_hours=lic.offline_grace_hours,
        expires_at=lic.expires_at,
        customer_id=lic.customer_id,
        customer_username=lic.customer.username if lic.customer else None,
        customer_discord_id=lic.customer.discord_id if lic.customer else None,
        products=sorted([p.slug for p in lic.products]),
        created_at=lic.created_at,
        updated_at=lic.updated_at,
    )


def _resolve_products(db: Session, slugs: list[str]) -> list[Product]:
    normalized = sorted({s.strip().lower() for s in slugs if s and s.strip()})
    if not normalized:
        raise HTTPException(400, "License must include at least one product")
    products = list(db.scalars(select(Product).where(Product.slug.in_(normalized), Product.active.is_(True))).all())
    found = {p.slug for p in products}
    missing = [s for s in normalized if s not in found]
    if missing:
        raise HTTPException(400, f"Unknown/inactive product(s): {', '.join(missing)}")
    return products


def _find_customer_by_discord(db: Session, discord_id: str) -> Customer | None:
    return db.scalar(select(Customer).where(Customer.discord_id == discord_id).order_by(Customer.created_at.asc()))


def _get_owned_license(db: Session, discord_id: str, license_id: uuid.UUID) -> tuple[Customer, License]:
    customer = _find_customer_by_discord(db, discord_id)
    if not customer:
        raise HTTPException(404, "No customer account is linked to this Discord user")
    lic = db.get(License, license_id)
    if not lic or lic.customer_id != customer.id:
        raise HTTPException(404, "License not found for this Discord user")
    return customer, lic


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "sparking-license-api", "version": "2.0.0", "database": "ok"}


@app.get("/v1/public-key", response_class=PlainTextResponse)
def public_key():
    return signer.public_pem


@app.post("/v1/licenses/activate", response_model=SignedResponse)
def license_activate(req: LicenseRequest, request: Request, db: Session = Depends(get_db)):
    return signer.sign_payload(signed_payload(req, activate(db, req, client_ip(request))))


@app.post("/v1/licenses/verify", response_model=SignedResponse)
def license_verify(req: LicenseRequest, request: Request, db: Session = Depends(get_db)):
    return signer.sign_payload(signed_payload(req, verify(db, req, client_ip(request), "LICENSE_VERIFIED")))


@app.post("/v1/licenses/heartbeat", response_model=SignedResponse)
def license_heartbeat(req: LicenseRequest, request: Request, db: Session = Depends(get_db)):
    return signer.sign_payload(signed_payload(req, verify(db, req, client_ip(request), "HEARTBEAT")))


@app.post("/v1/licenses/deactivate", response_model=SignedResponse)
def license_deactivate(req: LicenseRequest, request: Request, db: Session = Depends(get_db)):
    return signer.sign_payload(signed_payload(req, deactivate(db, req, client_ip(request))))


# ------------------------- Products -------------------------
@app.get("/admin/products", response_model=list[ProductOut])
def admin_products(db: Session = Depends(get_db), _: AdminPrincipal = Depends(require_admin)):
    return list(db.scalars(select(Product).order_by(Product.created_at.asc())).all())


@app.post("/admin/products", response_model=ProductOut)
def admin_create_product(body: ProductCreate, db: Session = Depends(get_db), _: AdminPrincipal = Depends(require_admin)):
    product = Product(name=body.name.strip(), slug=body.slug.strip().lower())
    db.add(product)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Product slug already exists")
    db.refresh(product)
    return product


@app.patch("/admin/products/{product_id}", response_model=ProductOut)
def admin_update_product(
    product_id: uuid.UUID,
    body: ProductUpdate,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(product, key, value.strip() if isinstance(value, str) else value)
    db.commit()
    db.refresh(product)
    return product


@app.delete("/admin/products/{product_id}")
def admin_delete_product(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    used = list(db.scalars(select(License).join(License.products).where(Product.id == product.id)).unique().all())
    if used:
        raise HTTPException(
            409,
            {
                "code": "PRODUCT_IN_USE",
                "message": f"Product is used by {len(used)} license(s). Remove or migrate it first.",
                "licenses": [str(x.id) for x in used],
            },
        )
    db.delete(product)
    db.commit()
    return {"ok": True}


@app.post("/admin/products/migrate")
def admin_migrate_product(
    body: ProductMigrate,
    request: Request,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = Depends(require_admin),
):
    source_slug = body.source_slug.strip().lower()
    target_slug = body.target_slug.strip().lower()
    if source_slug == target_slug:
        raise HTTPException(400, "Source and target products are the same")
    source = db.scalar(select(Product).where(Product.slug == source_slug))
    target = db.scalar(select(Product).where(Product.slug == target_slug))
    if not source:
        raise HTTPException(404, f"Source product '{source_slug}' not found")
    if not target:
        raise HTTPException(404, f"Target product '{target_slug}' not found")

    affected = list(db.scalars(select(License).join(License.products).where(Product.id == source.id)).unique().all())
    for lic in affected:
        new_products = [p for p in lic.products if p.id != source.id]
        if all(p.id != target.id for p in new_products):
            new_products.append(target)
        lic.products = new_products
        lic.updated_at = utcnow()
        log_event(
            db,
            "ADMIN_PRODUCT_MIGRATED",
            client_ip(request),
            lic.id,
            details={"by": principal.name, "from": source_slug, "to": target_slug},
        )
    db.flush()
    db.delete(source)
    db.commit()
    return {"ok": True, "from": source_slug, "to": target_slug, "licenses_updated": len(affected)}


# ------------------------- Customers -------------------------
@app.get("/admin/customers", response_model=list[CustomerOut])
def admin_customers(
    q: str | None = Query(default=None, max_length=255),
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    stmt = select(Customer).order_by(Customer.created_at.desc()).limit(200)
    if q:
        like = f"%{q}%"
        stmt = (
            select(Customer)
            .where(or_(Customer.username.ilike(like), Customer.email.ilike(like), Customer.discord_id.ilike(like)))
            .order_by(Customer.created_at.desc())
            .limit(200)
        )
    return list(db.scalars(stmt).all())


@app.post("/admin/customers", response_model=CustomerOut)
def admin_create_customer(body: CustomerCreate, db: Session = Depends(get_db), _: AdminPrincipal = Depends(require_admin)):
    if body.discord_id:
        existing = _find_customer_by_discord(db, body.discord_id)
        if existing:
            return existing
    customer = Customer(**body.model_dump())
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@app.post("/admin/customers/upsert-discord", response_model=CustomerOut)
def admin_upsert_discord_customer(
    body: CustomerDiscordUpsert,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    customer = _find_customer_by_discord(db, body.discord_id)
    if customer:
        if body.username:
            customer.username = body.username
            db.commit()
            db.refresh(customer)
        return customer
    customer = Customer(discord_id=body.discord_id, username=body.username)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@app.get("/admin/customers/by-discord/{discord_id}", response_model=CustomerOut)
def admin_customer_by_discord(
    discord_id: str,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    customer = _find_customer_by_discord(db, discord_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    return customer


@app.get("/admin/customers/{customer_id}/licenses", response_model=list[LicenseOut])
def admin_customer_licenses(
    customer_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    if not db.get(Customer, customer_id):
        raise HTTPException(404, "Customer not found")
    licenses = list(db.scalars(select(License).where(License.customer_id == customer_id).order_by(License.created_at.desc())).unique().all())
    return [to_license_out(db, lic) for lic in licenses]


# ------------------------- Licenses -------------------------
@app.get("/admin/licenses", response_model=list[LicenseOut])
def admin_licenses(
    q: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    licenses = list(db.scalars(select(License).order_by(License.created_at.desc()).limit(500)).unique().all())
    if q:
        query = q.strip().lower()
        filtered = []
        for lic in licenses:
            if query in str(lic.id).lower() or query in lic.key_last4.lower():
                filtered.append(lic)
                continue
            if any(query in p.slug.lower() or query in p.name.lower() for p in lic.products):
                filtered.append(lic)
                continue
            if lic.customer and (
                (lic.customer.username and query in lic.customer.username.lower())
                or (lic.customer.email and query in lic.customer.email.lower())
                or (lic.customer.discord_id and query in lic.customer.discord_id.lower())
            ):
                filtered.append(lic)
        licenses = filtered
    return [to_license_out(db, lic) for lic in licenses]


@app.get("/admin/licenses/{license_id}", response_model=LicenseOut)
def admin_license_get(license_id: uuid.UUID, db: Session = Depends(get_db), _: AdminPrincipal = Depends(require_admin)):
    lic = db.get(License, license_id)
    if not lic:
        raise HTTPException(404, "License not found")
    return to_license_out(db, lic)


@app.post("/admin/licenses", response_model=LicenseCreated)
def admin_create_license(
    body: LicenseCreate,
    request: Request,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = Depends(require_admin),
):
    products = _resolve_products(db, body.product_slugs)
    if body.customer_id and not db.get(Customer, body.customer_id):
        raise HTTPException(400, "Customer not found")
    raw_key = generate_license_key()
    lic = License(
        key_hash=hash_license_key(raw_key),
        key_last4=raw_key[-4:],
        status="ACTIVE",
        license_type=body.license_type,
        max_activations=body.max_activations,
        offline_grace_hours=body.offline_grace_hours or settings.offline_grace_hours,
        expires_at=body.expires_at,
        customer_id=body.customer_id,
        products=products,
    )
    db.add(lic)
    db.flush()
    log_event(db, "ADMIN_LICENSE_CREATED", client_ip(request), lic.id, details={"by": principal.name, "products": body.product_slugs})
    db.commit()
    db.refresh(lic)
    return LicenseCreated(**to_license_out(db, lic).model_dump(), license_key=raw_key)


@app.patch("/admin/licenses/{license_id}", response_model=LicenseOut)
def admin_update_license(
    license_id: uuid.UUID,
    body: LicenseUpdate,
    request: Request,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = Depends(require_admin),
):
    lic = db.get(License, license_id)
    if not lic:
        raise HTTPException(404, "License not found")
    data = body.model_dump(exclude_unset=True)
    if "product_slugs" in data:
        lic.products = _resolve_products(db, data.pop("product_slugs") or [])
    if "customer_id" in data and data["customer_id"] is not None and not db.get(Customer, data["customer_id"]):
        raise HTTPException(400, "Customer not found")
    for key, value in data.items():
        setattr(lic, key, value)
    lic.updated_at = utcnow()
    log_event(db, "ADMIN_LICENSE_UPDATED", client_ip(request), lic.id, details={"by": principal.name, "fields": list(body.model_fields_set)})
    db.commit()
    db.refresh(lic)
    return to_license_out(db, lic)


@app.delete("/admin/licenses/{license_id}")
def admin_delete_license(
    license_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = Depends(require_admin),
):
    lic = db.get(License, license_id)
    if not lic:
        raise HTTPException(404, "License not found")
    log_event(db, "ADMIN_LICENSE_DELETED", client_ip(request), lic.id, details={"by": principal.name, "last4": lic.key_last4})
    db.delete(lic)
    db.commit()
    return {"ok": True}


@app.post("/admin/licenses/{license_id}/rotate", response_model=LicenseCreated)
def admin_rotate_license(
    license_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = Depends(require_admin),
):
    lic = db.get(License, license_id)
    if not lic:
        raise HTTPException(404, "License not found")
    raw_key = generate_license_key()
    lic.key_hash = hash_license_key(raw_key)
    lic.key_last4 = raw_key[-4:]
    lic.updated_at = utcnow()
    log_event(db, "ADMIN_LICENSE_ROTATED", client_ip(request), lic.id, details={"by": principal.name})
    db.commit()
    db.refresh(lic)
    return LicenseCreated(**to_license_out(db, lic).model_dump(), license_key=raw_key)


@app.post("/admin/licenses/{license_id}/reset-activations", response_model=LicenseOut)
def admin_reset_activations(
    license_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = Depends(require_admin),
):
    lic = db.get(License, license_id)
    if not lic:
        raise HTTPException(404, "License not found")
    changed = 0
    for activation in lic.activations:
        if activation.status == "ACTIVE":
            activation.status = "INACTIVE"
            activation.last_seen = utcnow()
            changed += 1
    log_event(db, "ADMIN_ACTIVATIONS_RESET", client_ip(request), lic.id, details={"by": principal.name, "count": changed})
    db.commit()
    db.refresh(lic)
    return to_license_out(db, lic)


@app.get("/admin/licenses/{license_id}/activations", response_model=list[ActivationOut])
def admin_license_activations(
    license_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    if not db.get(License, license_id):
        raise HTTPException(404, "License not found")
    return list(db.scalars(select(Activation).where(Activation.license_id == license_id).order_by(Activation.last_seen.desc())).all())


@app.delete("/admin/licenses/{license_id}/activations/{activation_id}")
def admin_delete_activation(
    license_id: uuid.UUID,
    activation_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = Depends(require_admin),
):
    activation = db.get(Activation, activation_id)
    if not activation or activation.license_id != license_id:
        raise HTTPException(404, "Activation not found")
    log_event(db, "ADMIN_ACTIVATION_RELEASED", client_ip(request), license_id, activation_id, {"by": principal.name})
    db.delete(activation)
    db.commit()
    return {"ok": True}


# ------------------------- Bot user-safe routes -------------------------
@app.get("/bot/users/{discord_id}/licenses", response_model=list[LicenseOut])
def bot_user_licenses(
    discord_id: str,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    customer = _find_customer_by_discord(db, discord_id)
    if not customer:
        return []
    licenses = list(db.scalars(select(License).where(License.customer_id == customer.id).order_by(License.created_at.desc())).unique().all())
    return [to_license_out(db, lic) for lic in licenses]


@app.get("/bot/users/{discord_id}/licenses/{license_id}/activations", response_model=list[ActivationOut])
def bot_user_license_activations(
    discord_id: str,
    license_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    _get_owned_license(db, discord_id, license_id)
    return list(db.scalars(select(Activation).where(Activation.license_id == license_id).order_by(Activation.last_seen.desc())).all())


@app.post("/bot/users/{discord_id}/licenses/{license_id}/reset-activations", response_model=LicenseOut)
def bot_user_reset_activations(
    discord_id: str,
    license_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    _, lic = _get_owned_license(db, discord_id, license_id)
    cooldown = max(0, settings.user_activation_reset_cooldown_hours)
    if cooldown:
        since = utcnow() - timedelta(hours=cooldown)
        recent = db.scalar(
            select(AuditLog)
            .where(
                AuditLog.license_id == lic.id,
                AuditLog.action == "USER_ACTIVATIONS_RESET",
                AuditLog.created_at >= since,
            )
            .order_by(AuditLog.created_at.desc())
        )
        if recent:
            retry_at = recent.created_at + timedelta(hours=cooldown)
            raise HTTPException(429, {"code": "RESET_COOLDOWN", "retry_at": retry_at.isoformat(), "hours": cooldown})
    changed = 0
    for activation in lic.activations:
        if activation.status == "ACTIVE":
            activation.status = "INACTIVE"
            activation.last_seen = utcnow()
            changed += 1
    log_event(db, "USER_ACTIVATIONS_RESET", client_ip(request), lic.id, details={"discord_id": discord_id, "count": changed})
    db.commit()
    db.refresh(lic)
    return to_license_out(db, lic)


# ------------------------- System / audit / keys -------------------------
@app.get("/admin/system/status")
def admin_system_status(db: Session = Depends(get_db), _: AdminPrincipal = Depends(require_admin)):
    return {
        "status": "ok",
        "database": "ok",
        "products": int(db.scalar(select(func.count(Product.id))) or 0),
        "active_products": int(db.scalar(select(func.count(Product.id)).where(Product.active.is_(True))) or 0),
        "licenses": int(db.scalar(select(func.count(License.id))) or 0),
        "active_licenses": int(db.scalar(select(func.count(License.id)).where(License.status == "ACTIVE")) or 0),
        "disabled_licenses": int(db.scalar(select(func.count(License.id)).where(License.status == "DISABLED")) or 0),
        "active_activations": int(db.scalar(select(func.count(Activation.id)).where(Activation.status == "ACTIVE")) or 0),
        "customers": int(db.scalar(select(func.count(Customer.id))) or 0),
        "time": utcnow(),
    }


@app.get("/admin/audit")
def admin_audit(
    license_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(require_admin),
):
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if license_id:
        stmt = select(AuditLog).where(AuditLog.license_id == license_id).order_by(AuditLog.created_at.desc()).limit(limit)
    logs = list(db.scalars(stmt).all())
    return [
        {
            "id": str(x.id),
            "action": x.action,
            "license_id": str(x.license_id) if x.license_id else None,
            "activation_id": str(x.activation_id) if x.activation_id else None,
            "ip": x.ip,
            "details": x.details,
            "created_at": x.created_at,
        }
        for x in logs
    ]


@app.get("/admin/api-keys", response_model=list[ApiKeyOut])
def admin_api_keys(db: Session = Depends(get_db), _: AdminPrincipal = Depends(require_admin)):
    return list(db.scalars(select(AdminApiKey).order_by(AdminApiKey.created_at.desc())).all())


@app.post("/admin/api-keys", response_model=ApiKeyCreated)
def admin_create_api_key(body: ApiKeyCreate, db: Session = Depends(get_db), _: AdminPrincipal = Depends(require_admin)):
    raw = generate_admin_api_key()
    row = AdminApiKey(name=body.name.strip(), token_hash=hash_admin_token(raw), token_last4=raw[-4:])
    db.add(row)
    db.commit()
    db.refresh(row)
    return ApiKeyCreated(id=row.id, name=row.name, token=raw, token_last4=row.token_last4, created_at=row.created_at)


@app.delete("/admin/api-keys/{api_key_id}")
def admin_revoke_api_key(api_key_id: uuid.UUID, db: Session = Depends(get_db), _: AdminPrincipal = Depends(require_admin)):
    row = db.get(AdminApiKey, api_key_id)
    if not row:
        raise HTTPException(404, "API key not found")
    row.active = False
    db.commit()
    return {"ok": True}
