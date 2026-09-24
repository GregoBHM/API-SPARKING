from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    admin_token: str
    license_hash_secret: str
    signing_key_dir: str = "/app/keys"
    offline_grace_hours: int = 72
    activation_bind_fingerprint: bool = True
    enable_docs: bool = False
    user_activation_reset_cooldown_hours: int = 24

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
