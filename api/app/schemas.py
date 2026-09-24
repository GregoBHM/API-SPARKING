from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LicenseRequest(BaseModel):
    license_key: str = Field(min_length=12, max_length=128)
    product: str = Field(min_length=1, max_length=80)
    server_id: str = Field(min_length=8, max_length=128)
    fingerprint: str = Field(min_length=8, max_length=512)
    plugin_version: str | None = Field(default=None, max_length=64)
    minecraft_version: str | None = Field(default=None, max_length=64)
    nonce: str = Field(min_length=8, max_length=128)

    @field_validator("product")
    @classmethod
    def normalize_product(cls, value: str) -> str:
        return value.strip().lower()


class SignedResponse(BaseModel):
    token: str
    signature: str
    algorithm: str = "RS256"


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$")


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    active: bool
    created_at: datetime


class CustomerCreate(BaseModel):
    username: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=255)
    discord_id: str | None = Field(default=None, max_length=32)
    notes: str | None = None


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str | None
    email: str | None
    discord_id: str | None
    notes: str | None
    created_at: datetime


class LicenseCreate(BaseModel):
    product_slugs: list[str] = Field(min_length=1)
    customer_id: UUID | None = None
    license_type: str = Field(default="LIFETIME", pattern=r"^(LIFETIME|SUBSCRIPTION|TRIAL)$")
    max_activations: int = Field(default=1, ge=1, le=1000)
    offline_grace_hours: int | None = Field(default=None, ge=1, le=720)
    expires_at: datetime | None = None


class LicenseUpdate(BaseModel):
    status: str | None = Field(default=None, pattern=r"^(ACTIVE|DISABLED)$")
    license_type: str | None = Field(default=None, pattern=r"^(LIFETIME|SUBSCRIPTION|TRIAL)$")
    max_activations: int | None = Field(default=None, ge=1, le=1000)
    offline_grace_hours: int | None = Field(default=None, ge=1, le=720)
    expires_at: datetime | None = None
    customer_id: UUID | None = None
    product_slugs: list[str] | None = None


class LicenseOut(BaseModel):
    id: UUID
    masked_key: str
    status: str
    license_type: str
    max_activations: int
    active_activations: int
    offline_grace_hours: int
    expires_at: datetime | None
    customer_id: UUID | None
    products: list[str]
    created_at: datetime
    updated_at: datetime


class LicenseCreated(LicenseOut):
    license_key: str


class ActivationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    license_id: UUID
    server_id: str
    status: str
    first_seen: datetime
    last_seen: datetime
    last_ip: str | None
    plugin_version: str | None
    minecraft_version: str | None
    last_product_slug: str | None


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ApiKeyCreated(BaseModel):
    id: UUID
    name: str
    token: str
    token_last4: str
    created_at: datetime


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    token_last4: str
    active: bool
    created_at: datetime
    last_used_at: datetime | None
