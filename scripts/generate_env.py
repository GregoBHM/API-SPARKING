from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
example = root / ".env.example"
target = root / ".env"

if target.exists():
    raise SystemExit(".env already exists. Delete it first if you really want to regenerate secrets.")

text = example.read_text("utf-8")
postgres_password = secrets.token_hex(24)
admin_token = "spark_bootstrap_" + secrets.token_urlsafe(48)
license_secret = secrets.token_hex(64)

text = text.replace("POSTGRES_PASSWORD=CHANGE_ME", f"POSTGRES_PASSWORD={postgres_password}")
text = text.replace(
    "DATABASE_URL=postgresql+psycopg://sparking:CHANGE_ME@db:5432/sparking_licenses",
    f"DATABASE_URL=postgresql+psycopg://sparking:{postgres_password}@db:5432/sparking_licenses",
)
text = text.replace("ADMIN_TOKEN=CHANGE_ME", f"ADMIN_TOKEN={admin_token}")
text = text.replace("LICENSE_HASH_SECRET=CHANGE_ME", f"LICENSE_HASH_SECRET={license_secret}")

target.write_text(text, "utf-8")
print(f"Created {target}")
print("Keep .env private and back it up securely.")
