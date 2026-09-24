import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import discord
import httpx
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN:
    print("DISCORD_TOKEN is empty; Discord bot is disabled.")
    raise SystemExit(0)

API_URL = os.getenv("API_INTERNAL_URL", "http://api:8000").rstrip("/")
DISCORD_API_TOKEN = os.getenv("DISCORD_API_TOKEN", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
API_TOKEN = DISCORD_API_TOKEN or ADMIN_TOKEN
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
ADMIN_ROLE_IDS = {int(x.strip()) for x in os.getenv("DISCORD_ADMIN_ROLE_IDS", "").split(",") if x.strip().isdigit()}
CLEAN_SLASH_ON_START = os.getenv("DISCORD_CLEAN_SLASH_ON_START", "true").strip().lower() in {"1", "true", "yes", "on"}
STATUS_NAME = os.getenv("DISCORD_STATUS", "online").strip().lower()
ACTIVITY_TYPE = os.getenv("DISCORD_ACTIVITY_TYPE", "watching").strip().lower()
ACTIVITY_TEXT = os.getenv("DISCORD_ACTIVITY_TEXT", "SparkingCraft Licenses").strip() or "SparkingCraft Licenses"
BRAND = os.getenv("DISCORD_BRAND_NAME", "SparkingCraft License Manager").strip() or "SparkingCraft License Manager"

if not API_TOKEN:
    print("Neither DISCORD_API_TOKEN nor ADMIN_TOKEN is configured.")
    raise SystemExit(1)

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
started_at = datetime.now(timezone.utc)
commands_synced_once = False

# Discord embed colors
C_INFO = 0x5865F2
C_OK = 0x57F287
C_WARN = 0xFEE75C
C_ERROR = 0xED4245
C_DARK = 0x2B2D31


class ApiError(Exception):
    def __init__(self, status: int, detail: Any):
        self.status = status
        self.detail = detail
        super().__init__(str(detail))


def allowed(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    return any(role.id in ADMIN_ROLE_IDS for role in interaction.user.roles)


async def guard(interaction: discord.Interaction) -> bool:
    if allowed(interaction):
        return True
    await send_embed(interaction, error_embed("Sin permisos", "No tienes permisos para administrar el sistema de licencias."), ephemeral=True)
    return False


def base_embed(title: str, description: str | None = None, color: int = C_INFO) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=BRAND)
    return e


def success_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(f"✅ {title}", description, C_OK)


def error_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(f"❌ {title}", description, C_ERROR)


def warning_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(f"⚠️ {title}", description, C_WARN)


def info_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(f"🔐 {title}", description, C_INFO)


def status_emoji(status: str) -> str:
    return "🟢" if status == "ACTIVE" else "🔴" if status == "DISABLED" else "🟡"


def safe_text(value: Any, fallback: str = "—") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def fmt_dt(value: str | None) -> str:
    if not value:
        return "Nunca"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return f"<t:{int(dt.timestamp())}:f>"
    except Exception:
        return value


def masked_id(value: str) -> str:
    return value[:8] + "…" if len(value) > 8 else value


async def api(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {API_TOKEN}"
    async with httpx.AsyncClient(timeout=15.0) as http:
        response = await http.request(method, API_URL + path, headers=headers, **kwargs)
    if response.status_code >= 400:
        try:
            payload = response.json()
            detail = payload.get("detail", payload)
        except Exception:
            detail = response.text or f"HTTP {response.status_code}"
        raise ApiError(response.status_code, detail)
    if not response.content:
        return None
    try:
        return response.json()
    except Exception:
        return response.text


def friendly_api_error(exc: Exception) -> discord.Embed:
    if not isinstance(exc, ApiError):
        return error_embed("Error inesperado", "Ocurrió un error interno. Revisa los logs del bot.")
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("code")
        if code == "RESET_COOLDOWN":
            retry_at = detail.get("retry_at")
            return warning_embed("Clear en cooldown", f"Podrás volver a liberar servidores el {fmt_dt(retry_at)}.")
        if code == "PRODUCT_IN_USE":
            count = len(detail.get("licenses", []))
            return warning_embed(
                "Producto en uso",
                f"Este producto está asociado a **{count} licencia(s)**.\nPrimero migra o quita el producto de esas licencias.",
            )
        message = detail.get("message") or detail.get("detail")
        if message:
            return error_embed("Operación rechazada", str(message))
    text = str(detail)
    # Never expose raw Pydantic / stack-like structures to Discord users.
    if "uuid_parsing" in text or "valid UUID" in text:
        return error_embed("Licencia inválida", "No pude identificar esa licencia. Selecciónala desde el menú del bot.")
    translations = {
        "Invalid bearer token": "El bot no está autenticado correctamente con la API.",
        "Missing bearer token": "Falta el token de autenticación del bot.",
        "License not found": "No encontré esa licencia.",
        "Product not found": "No encontré ese producto.",
        "Customer not found": "No encontré ese usuario en el sistema.",
        "Product slug already exists": "Ya existe un producto con ese slug.",
    }
    return error_embed("No se pudo completar", translations.get(text, text[:500]))


async def send_embed(interaction: discord.Interaction, embed: discord.Embed, *, view: discord.ui.View | None = None, ephemeral: bool = True):
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=ephemeral)


async def edit_or_send(interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View | None = None):
    if interaction.response.is_done():
        try:
            await interaction.edit_original_response(embed=embed, view=view)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.edit_message(embed=embed, view=view)


def license_embed(lic: dict, title: str = "Detalle de licencia") -> discord.Embed:
    e = info_embed(title)
    e.add_field(name="Licencia", value=f"`{lic['masked_key']}`", inline=False)
    e.add_field(name="Estado", value=f"{status_emoji(lic['status'])} **{lic['status']}**", inline=True)
    e.add_field(name="Servidores", value=f"`{lic['active_activations']} / {lic['max_activations']}`", inline=True)
    e.add_field(name="Tipo", value=f"`{lic['license_type']}`", inline=True)
    e.add_field(name="Productos", value="\n".join(f"• `{x}`" for x in lic.get("products", [])) or "—", inline=False)
    owner = lic.get("customer_username") or "Sin asignar"
    discord_id = lic.get("customer_discord_id")
    if discord_id:
        owner += f"\n<@{discord_id}>"
    e.add_field(name="Usuario", value=owner, inline=True)
    e.add_field(name="Expira", value=fmt_dt(lic.get("expires_at")), inline=True)
    e.add_field(name="ID", value=f"`{lic['id']}`", inline=False)
    return e


def licenses_page_embed(rows: list[dict], page: int, per_page: int = 5, title: str = "Licencias") -> discord.Embed:
    total_pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    current = rows[start:start + per_page]
    e = info_embed(title, f"**{len(rows)}** licencia(s) registradas")
    if not current:
        e.description = "No hay licencias para mostrar."
    for lic in current:
        products = ", ".join(lic.get("products", [])) or "Sin productos"
        owner = lic.get("customer_username") or (f"<@{lic['customer_discord_id']}>" if lic.get("customer_discord_id") else "Sin asignar")
        e.add_field(
            name=f"{status_emoji(lic['status'])} {lic['masked_key']}",
            value=(
                f"**Productos:** {products}\n"
                f"**Servidores:** `{lic['active_activations']}/{lic['max_activations']}`\n"
                f"**Usuario:** {owner}\n"
                f"**ID:** `{masked_id(lic['id'])}`"
            ),
            inline=False,
        )
    e.set_footer(text=f"{BRAND} • Página {page + 1}/{total_pages}")
    return e


class PaginatorView(discord.ui.View):
    def __init__(self, rows: list[dict], title: str = "Licencias", per_page: int = 5):
        super().__init__(timeout=180)
        self.rows = rows
        self.title = title
        self.per_page = per_page
        self.page = 0

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        total = max(1, (len(self.rows) + self.per_page - 1) // self.per_page)
        self.page = (self.page - 1) % total
        await interaction.response.edit_message(embed=licenses_page_embed(self.rows, self.page, self.per_page, self.title), view=self)

    @discord.ui.button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        total = max(1, (len(self.rows) + self.per_page - 1) // self.per_page)
        self.page = (self.page + 1) % total
        await interaction.response.edit_message(embed=licenses_page_embed(self.rows, self.page, self.per_page, self.title), view=self)


class ConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, on_confirm: Callable[[discord.Interaction], Awaitable[None]], confirm_label: str = "Confirmar", danger: bool = False):
        super().__init__(timeout=90)
        self.owner_id = owner_id
        self.on_confirm = on_confirm
        self.confirm.label = confirm_label
        self.confirm.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await send_embed(interaction, error_embed("Acción privada", "Solo quien abrió este menú puede utilizarlo."))
            return False
        return True

    @discord.ui.button(label="Confirmar", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await self.on_confirm(interaction)
        self.stop()

    @discord.ui.button(label="Cancelar", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=warning_embed("Operación cancelada", "No se realizó ningún cambio."), view=self)
        self.stop()


class LicenseSelectView(discord.ui.View):
    def __init__(self, owner_id: int, rows: list[dict], callback: Callable[[discord.Interaction, dict], Awaitable[None]], placeholder: str = "Selecciona una licencia"):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.rows = rows[:25]
        options = []
        for lic in self.rows:
            products = ", ".join(lic.get("products", []))[:50] or "Sin productos"
            options.append(
                discord.SelectOption(
                    label=lic["masked_key"][:100],
                    value=lic["id"],
                    description=f"{lic['status']} • {lic['active_activations']}/{lic['max_activations']} • {products}"[:100],
                    emoji="🟢" if lic["status"] == "ACTIVE" else "🔴",
                )
            )
        select = discord.ui.Select(placeholder=placeholder, min_values=1, max_values=1, options=options)

        async def selected(interaction: discord.Interaction):
            lic_id = select.values[0]
            lic = next(x for x in self.rows if x["id"] == lic_id)
            await callback(interaction, lic)

        select.callback = selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await send_embed(interaction, error_embed("Acción privada", "Solo quien abrió este menú puede utilizarlo."))
            return False
        return True


class ProductSelectCreateView(discord.ui.View):
    def __init__(self, owner_id: int, products: list[dict], customer: discord.User | discord.Member | None, max_servers: int, expires_days: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.products = products[:25]
        self.customer = customer
        self.max_servers = max_servers
        self.expires_days = expires_days
        self.selected: list[str] = []
        options = [
            discord.SelectOption(label=p["name"][:100], value=p["slug"], description=p["slug"][:100], emoji="📦")
            for p in self.products if p.get("active")
        ]
        self.select = discord.ui.Select(
            placeholder="Selecciona uno o varios productos",
            min_values=1,
            max_values=max(1, len(options)),
            options=options,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await send_embed(interaction, error_embed("Acción privada", "Solo quien abrió este menú puede utilizarlo."))
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        self.selected = list(self.select.values)
        e = info_embed("Crear licencia", "Productos seleccionados. Confirma para generar la licencia.")
        e.add_field(name="Productos", value="\n".join(f"• `{x}`" for x in self.selected), inline=False)
        e.add_field(name="Máx. servidores", value=str(self.max_servers), inline=True)
        e.add_field(name="Expiración", value="Permanente" if self.expires_days == 0 else f"{self.expires_days} días", inline=True)
        e.add_field(name="Usuario", value=self.customer.mention if self.customer else "Sin asignar", inline=True)
        await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="Crear licencia", emoji="🔑", style=discord.ButtonStyle.success, row=2)
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected:
            await send_embed(interaction, warning_embed("Selecciona productos", "Debes seleccionar al menos un producto."))
            return
        await interaction.response.defer()
        try:
            customer_id = None
            if self.customer:
                c = await api(
                    "POST",
                    "/admin/customers/upsert-discord",
                    json={"discord_id": str(self.customer.id), "username": str(self.customer)},
                )
                customer_id = c["id"]
            expires_at = None
            license_type = "LIFETIME"
            if self.expires_days > 0:
                expires_at = (datetime.now(timezone.utc) + timedelta(days=self.expires_days)).isoformat()
                license_type = "SUBSCRIPTION"
            payload = {
                "product_slugs": self.selected,
                "customer_id": customer_id,
                "license_type": license_type,
                "max_activations": self.max_servers,
                "expires_at": expires_at,
            }
            data = await api("POST", "/admin/licenses", json=payload)
            e = success_embed("Licencia creada", "Guarda esta clave ahora. Por seguridad, la API no puede mostrarla otra vez.")
            e.add_field(name="🔑 License Key", value=f"```{data['license_key']}```", inline=False)
            e.add_field(name="Productos", value="\n".join(f"• `{x}`" for x in data["products"]), inline=False)
            e.add_field(name="Servidores", value=f"`0 / {data['max_activations']}`", inline=True)
            e.add_field(name="Usuario", value=self.customer.mention if self.customer else "Sin asignar", inline=True)
            e.add_field(name="ID", value=f"`{data['id']}`", inline=False)
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(embed=e, view=self)
        except Exception as exc:
            await interaction.edit_original_response(embed=friendly_api_error(exc), view=self)


class ProductSetView(discord.ui.View):
    def __init__(self, owner_id: int, license_data: dict, products: list[dict]):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.license = license_data
        current = set(license_data.get("products", []))
        options = [
            discord.SelectOption(
                label=p["name"][:100],
                value=p["slug"],
                description=p["slug"][:100],
                default=p["slug"] in current,
                emoji="📦",
            )
            for p in products[:25] if p.get("active")
        ]
        select = discord.ui.Select(placeholder="Productos permitidos", min_values=1, max_values=max(1, len(options)), options=options)

        async def selected(interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                updated = await api("PATCH", f"/admin/licenses/{self.license['id']}", json={"product_slugs": select.values})
                await interaction.edit_original_response(embed=success_embed("Productos actualizados", "\n".join(f"• `{x}`" for x in updated["products"])), view=None)
            except Exception as exc:
                await interaction.edit_original_response(embed=friendly_api_error(exc), view=self)

        select.callback = selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id


async def get_licenses(query: str | None = None) -> list[dict]:
    params = {"q": query} if query else None
    return await api("GET", "/admin/licenses", params=params)


async def choose_license(interaction: discord.Interaction, query: str | None, callback: Callable[[discord.Interaction, dict], Awaitable[None]], admin_only: bool = True):
    if admin_only and not allowed(interaction):
        await send_embed(interaction, error_embed("Sin permisos", "No tienes permisos para administrar licencias."))
        return
    try:
        rows = await get_licenses(query)
    except Exception as exc:
        await send_embed(interaction, friendly_api_error(exc))
        return
    if not rows:
        await send_embed(interaction, warning_embed("Sin resultados", "No encontré licencias con ese criterio."))
        return
    if len(rows) == 1:
        await callback(interaction, rows[0])
        return
    view = LicenseSelectView(interaction.user.id, rows, callback)
    await send_embed(interaction, info_embed("Selecciona una licencia", f"Encontré **{len(rows)}** coincidencias."), view=view)


# ------------------------- Groups -------------------------
product_group = app_commands.Group(name="product", description="Administrar productos licenciables")
license_group = app_commands.Group(name="license", description="Administrar licencias")
my_group = app_commands.Group(name="my", description="Gestionar tus licencias")
system_group = app_commands.Group(name="system", description="Administrar el sistema")


# ------------------------- Product commands -------------------------
@product_group.command(name="list", description="Lista todos los productos")
async def product_list(interaction: discord.Interaction):
    if not await guard(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        rows = await api("GET", "/admin/products")
        e = base_embed("📦 Productos", f"**{len(rows)}** producto(s) registrados", C_INFO)
        if not rows:
            e.description = "No hay productos creados."
        for p in rows:
            e.add_field(name=f"{'🟢' if p['active'] else '🔴'} {p['name']}", value=f"Slug: `{p['slug']}`\nID: `{masked_id(p['id'])}`", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(embed=friendly_api_error(exc), ephemeral=True)


@product_group.command(name="create", description="Crea un nuevo producto")
@app_commands.describe(slug="Slug interno, ejemplo sparkcore", name="Nombre visible")
async def product_create(interaction: discord.Interaction, slug: str, name: str):
    if not await guard(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        p = await api("POST", "/admin/products", json={"slug": slug.strip().lower(), "name": name.strip()})
        e = success_embed("Producto creado")
        e.add_field(name="Nombre", value=p["name"], inline=True)
        e.add_field(name="Slug", value=f"`{p['slug']}`", inline=True)
        e.add_field(name="ID", value=f"`{p['id']}`", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(embed=friendly_api_error(exc), ephemeral=True)


async def resolve_product(slug: str) -> dict | None:
    rows = await api("GET", "/admin/products")
    q = slug.strip().lower()
    return next((p for p in rows if p["slug"] == q), None)


@product_group.command(name="enable", description="Activa un producto")
async def product_enable(interaction: discord.Interaction, slug: str):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        p = await resolve_product(slug)
        if not p: raise ApiError(404, "Product not found")
        p = await api("PATCH", f"/admin/products/{p['id']}", json={"active": True})
        await interaction.followup.send(embed=success_embed("Producto activado", f"`{p['slug']}` ya puede utilizarse en licencias."), ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(embed=friendly_api_error(exc), ephemeral=True)


@product_group.command(name="disable", description="Desactiva un producto")
async def product_disable(interaction: discord.Interaction, slug: str):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        p = await resolve_product(slug)
        if not p: raise ApiError(404, "Product not found")
        p = await api("PATCH", f"/admin/products/{p['id']}", json={"active": False})
        await interaction.followup.send(embed=warning_embed("Producto desactivado", f"`{p['slug']}` dejará de validar mientras esté desactivado."), ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(embed=friendly_api_error(exc), ephemeral=True)


@product_group.command(name="delete", description="Elimina un producto que no esté en uso")
async def product_delete(interaction: discord.Interaction, slug: str):
    if not await guard(interaction): return
    try:
        p = await resolve_product(slug)
        if not p:
            await send_embed(interaction, error_embed("Producto no encontrado", f"No existe `{slug}`."))
            return
    except Exception as exc:
        await send_embed(interaction, friendly_api_error(exc)); return

    async def do_delete(i: discord.Interaction):
        await i.response.defer()
        try:
            await api("DELETE", f"/admin/products/{p['id']}")
            await i.edit_original_response(embed=success_embed("Producto eliminado", f"`{p['slug']}` fue eliminado."), view=None)
        except Exception as exc:
            await i.edit_original_response(embed=friendly_api_error(exc), view=None)

    e = warning_embed("Eliminar producto", f"Vas a eliminar **{p['name']}** (`{p['slug']}`).\nSi está asociado a alguna licencia, la API bloqueará la eliminación.")
    await send_embed(interaction, e, view=ConfirmView(interaction.user.id, do_delete, "Eliminar", danger=True))


# ------------------------- License commands -------------------------
@license_group.command(name="create", description="Crea una licencia y selecciona sus productos")
@app_commands.describe(user="Usuario de Discord propietario", max_servers="Máximo de servidores", expires_days="0 = permanente")
async def license_create(
    interaction: discord.Interaction,
    user: discord.User | None = None,
    max_servers: app_commands.Range[int, 1, 1000] = 1,
    expires_days: app_commands.Range[int, 0, 3650] = 0,
):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        products = await api("GET", "/admin/products")
        active = [p for p in products if p.get("active")]
        if not active:
            await interaction.followup.send(embed=warning_embed("No hay productos", "Crea al menos un producto antes de generar licencias."), ephemeral=True)
            return
        if len(active) > 25:
            await interaction.followup.send(embed=warning_embed("Demasiados productos", "El selector admite hasta 25 productos activos. Desactiva productos antiguos o divide el catálogo."), ephemeral=True)
            return
        e = info_embed("Crear licencia", "Selecciona todos los productos que esta licencia debe permitir.")
        e.add_field(name="Usuario", value=user.mention if user else "Sin asignar", inline=True)
        e.add_field(name="Máx. servidores", value=str(max_servers), inline=True)
        e.add_field(name="Expiración", value="Permanente" if expires_days == 0 else f"{expires_days} días", inline=True)
        view = ProductSelectCreateView(interaction.user.id, active, user, max_servers, expires_days)
        await interaction.followup.send(embed=e, view=view, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(embed=friendly_api_error(exc), ephemeral=True)


@license_group.command(name="list", description="Lista las licencias")
async def license_list(interaction: discord.Interaction):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        rows = await get_licenses()
        view = PaginatorView(rows)
        await interaction.followup.send(embed=licenses_page_embed(rows, 0), view=view, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(embed=friendly_api_error(exc), ephemeral=True)


@license_group.command(name="info", description="Muestra una licencia; si hay varias, abre un selector")
async def license_info(interaction: discord.Interaction, query: str | None = None):
    async def action(i: discord.Interaction, lic: dict):
        await send_embed(i, license_embed(lic))
    await choose_license(interaction, query, action)


@license_group.command(name="enable", description="Activa una licencia")
async def license_enable(interaction: discord.Interaction, query: str | None = None):
    async def action(i: discord.Interaction, lic: dict):
        if not i.response.is_done(): await i.response.defer()
        try:
            out = await api("PATCH", f"/admin/licenses/{lic['id']}", json={"status": "ACTIVE"})
            await i.edit_original_response(embed=success_embed("Licencia activada", f"`{out['masked_key']}` está ACTIVE."), view=None)
        except Exception as exc:
            await i.edit_original_response(embed=friendly_api_error(exc), view=None)
    await choose_license(interaction, query, action)


@license_group.command(name="disable", description="Desactiva una licencia")
async def license_disable(interaction: discord.Interaction, query: str | None = None):
    async def action(i: discord.Interaction, lic: dict):
        if not i.response.is_done(): await i.response.defer()
        try:
            out = await api("PATCH", f"/admin/licenses/{lic['id']}", json={"status": "DISABLED"})
            await i.edit_original_response(embed=warning_embed("Licencia desactivada", f"`{out['masked_key']}` está DISABLED. Los plugins lo detectarán en la siguiente validación/heartbeat."), view=None)
        except Exception as exc:
            await i.edit_original_response(embed=friendly_api_error(exc), view=None)
    await choose_license(interaction, query, action)


@license_group.command(name="reset", description="Libera todos los servidores de una licencia")
async def license_reset(interaction: discord.Interaction, query: str | None = None):
    async def action(i: discord.Interaction, lic: dict):
        async def confirm(ci: discord.Interaction):
            await ci.response.defer()
            try:
                out = await api("POST", f"/admin/licenses/{lic['id']}/reset-activations")
                await ci.edit_original_response(embed=success_embed("Activaciones liberadas", f"Ahora: `{out['active_activations']}/{out['max_activations']}` servidores activos."), view=None)
            except Exception as exc:
                await ci.edit_original_response(embed=friendly_api_error(exc), view=None)
        await send_embed(i, warning_embed("Liberar servidores", f"Se liberarán todas las activaciones de `{lic['masked_key']}`."), view=ConfirmView(i.user.id, confirm, "Liberar"))
    await choose_license(interaction, query, action)


@license_group.command(name="delete", description="Elimina definitivamente una licencia")
async def license_delete(interaction: discord.Interaction, query: str | None = None):
    async def action(i: discord.Interaction, lic: dict):
        async def confirm(ci: discord.Interaction):
            await ci.response.defer()
            try:
                await api("DELETE", f"/admin/licenses/{lic['id']}")
                await ci.edit_original_response(embed=success_embed("Licencia eliminada", f"`{lic['masked_key']}` ya no existe."), view=None)
            except Exception as exc:
                await ci.edit_original_response(embed=friendly_api_error(exc), view=None)
        e = warning_embed("Eliminar licencia", f"**{lic['masked_key']}**\nProductos: {', '.join(lic['products'])}\nActivaciones: {lic['active_activations']}/{lic['max_activations']}\n\nEsta acción es permanente.")
        await send_embed(i, e, view=ConfirmView(i.user.id, confirm, "Eliminar", danger=True))
    await choose_license(interaction, query, action)


@license_group.command(name="rotate", description="Genera una nueva key y revoca la anterior")
async def license_rotate(interaction: discord.Interaction, query: str | None = None):
    async def action(i: discord.Interaction, lic: dict):
        async def confirm(ci: discord.Interaction):
            await ci.response.defer()
            try:
                out = await api("POST", f"/admin/licenses/{lic['id']}/rotate")
                e = success_embed("Licencia rotada", "La key anterior dejó de ser válida. Guarda la nueva ahora.")
                e.add_field(name="Nueva License Key", value=f"```{out['license_key']}```", inline=False)
                await ci.edit_original_response(embed=e, view=None)
            except Exception as exc:
                await ci.edit_original_response(embed=friendly_api_error(exc), view=None)
        await send_embed(i, warning_embed("Rotar licencia", f"La clave actual de `{lic['masked_key']}` dejará de funcionar inmediatamente."), view=ConfirmView(i.user.id, confirm, "Rotar", danger=True))
    await choose_license(interaction, query, action)


@license_group.command(name="assign", description="Asigna una licencia a un usuario de Discord")
async def license_assign(interaction: discord.Interaction, user: discord.User, query: str | None = None):
    async def action(i: discord.Interaction, lic: dict):
        if not i.response.is_done(): await i.response.defer()
        try:
            customer = await api("POST", "/admin/customers/upsert-discord", json={"discord_id": str(user.id), "username": str(user)})
            out = await api("PATCH", f"/admin/licenses/{lic['id']}", json={"customer_id": customer["id"]})
            await i.edit_original_response(embed=success_embed("Licencia asignada", f"`{out['masked_key']}` ahora pertenece a {user.mention}."), view=None)
        except Exception as exc:
            await i.edit_original_response(embed=friendly_api_error(exc), view=None)
    await choose_license(interaction, query, action)


@license_group.command(name="products", description="Cambia los productos incluidos en una licencia")
async def license_products(interaction: discord.Interaction, query: str | None = None):
    async def action(i: discord.Interaction, lic: dict):
        try:
            products = await api("GET", "/admin/products")
            active = [p for p in products if p.get("active")]
            if len(active) > 25:
                await send_embed(i, warning_embed("Demasiados productos", "El selector admite hasta 25 productos activos.")); return
            await send_embed(i, info_embed("Productos de la licencia", f"Editando `{lic['masked_key']}`."), view=ProductSetView(i.user.id, lic, active))
        except Exception as exc:
            await send_embed(i, friendly_api_error(exc))
    await choose_license(interaction, query, action)


@license_group.command(name="servers", description="Muestra las activaciones de una licencia")
async def license_servers(interaction: discord.Interaction, query: str | None = None):
    async def action(i: discord.Interaction, lic: dict):
        if not i.response.is_done(): await i.response.defer()
        try:
            rows = await api("GET", f"/admin/licenses/{lic['id']}/activations")
            e = base_embed("🖥️ Servidores / Activaciones", f"{lic['masked_key']} • **{len(rows)}** registro(s)", C_INFO)
            if not rows: e.description += "\n\nNo hay activaciones registradas."
            for a in rows[:15]:
                e.add_field(
                    name=f"{'🟢' if a['status'] == 'ACTIVE' else '⚪'} {safe_text(a.get('last_product_slug'), 'Servidor')}",
                    value=(f"**Estado:** {a['status']}\n**IP:** `{safe_text(a.get('last_ip'))}`\n**Server ID:** `{masked_id(a['server_id'])}`\n"
                           f"**Minecraft:** `{safe_text(a.get('minecraft_version'))}`\n**Última vez:** {fmt_dt(a.get('last_seen'))}"),
                    inline=False,
                )
            await i.edit_original_response(embed=e, view=None)
        except Exception as exc:
            await i.edit_original_response(embed=friendly_api_error(exc), view=None)
    await choose_license(interaction, query, action)


# ------------------------- My commands -------------------------
async def my_licenses(user_id: int) -> list[dict]:
    return await api("GET", f"/bot/users/{user_id}/licenses")


async def choose_my_license(interaction: discord.Interaction, callback: Callable[[discord.Interaction, dict], Awaitable[None]]):
    try:
        rows = await my_licenses(interaction.user.id)
    except Exception as exc:
        await send_embed(interaction, friendly_api_error(exc)); return
    if not rows:
        await send_embed(interaction, warning_embed("Sin licencias", "No tienes licencias asociadas a tu cuenta de Discord.")); return
    if len(rows) == 1:
        await callback(interaction, rows[0]); return
    await send_embed(interaction, info_embed("Selecciona tu licencia", f"Tienes **{len(rows)}** licencias."), view=LicenseSelectView(interaction.user.id, rows, callback))


@my_group.command(name="licenses", description="Muestra tus licencias")
async def my_licenses_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        rows = await my_licenses(interaction.user.id)
        view = PaginatorView(rows, "Mis licencias")
        await interaction.followup.send(embed=licenses_page_embed(rows, 0, title="Mis licencias"), view=view, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(embed=friendly_api_error(exc), ephemeral=True)


@my_group.command(name="license", description="Muestra el detalle de una de tus licencias")
async def my_license_cmd(interaction: discord.Interaction):
    async def action(i: discord.Interaction, lic: dict):
        await send_embed(i, license_embed(lic, "Mi licencia"))
    await choose_my_license(interaction, action)


@my_group.command(name="servers", description="Muestra los servidores asociados a tu licencia")
async def my_servers_cmd(interaction: discord.Interaction):
    async def action(i: discord.Interaction, lic: dict):
        if not i.response.is_done(): await i.response.defer()
        try:
            rows = await api("GET", f"/bot/users/{i.user.id}/licenses/{lic['id']}/activations")
            e = base_embed("🖥️ Mis servidores", f"{lic['masked_key']} • `{lic['active_activations']}/{lic['max_activations']}` activos", C_INFO)
            if not rows:
                e.description += "\n\nNo hay servidores registrados."
            for a in rows[:15]:
                e.add_field(
                    name=f"{'🟢' if a['status'] == 'ACTIVE' else '⚪'} {safe_text(a.get('last_product_slug'), 'Servidor')}",
                    value=f"IP: `{safe_text(a.get('last_ip'))}`\nÚltima conexión: {fmt_dt(a.get('last_seen'))}\nMinecraft: `{safe_text(a.get('minecraft_version'))}`",
                    inline=False,
                )
            await i.edit_original_response(embed=e, view=None)
        except Exception as exc:
            await i.edit_original_response(embed=friendly_api_error(exc), view=None)
    await choose_my_license(interaction, action)


@my_group.command(name="clear", description="Libera los servidores de una de tus licencias")
async def my_clear_cmd(interaction: discord.Interaction):
    async def action(i: discord.Interaction, lic: dict):
        async def confirm(ci: discord.Interaction):
            await ci.response.defer()
            try:
                out = await api("POST", f"/bot/users/{ci.user.id}/licenses/{lic['id']}/reset-activations")
                await ci.edit_original_response(embed=success_embed("Servidores liberados", f"Ahora tienes `{out['active_activations']}/{out['max_activations']}` activaciones en uso."), view=None)
            except Exception as exc:
                await ci.edit_original_response(embed=friendly_api_error(exc), view=None)
        e = warning_embed("Liberar servidores", f"Licencia: `{lic['masked_key']}`\nActualmente: `{lic['active_activations']}/{lic['max_activations']}`\n\nEsta acción tiene cooldown para evitar abuso.")
        await send_embed(i, e, view=ConfirmView(i.user.id, confirm, "Liberar"))
    await choose_my_license(interaction, action)


# ------------------------- System commands -------------------------
@system_group.command(name="status", description="Estado de API, BD y bot")
async def system_status(interaction: discord.Interaction):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    started = time.perf_counter()
    try:
        data = await api("GET", "/admin/system/status")
        api_ms = round((time.perf_counter() - started) * 1000)
        discord_ms = round(client.latency * 1000)
        uptime = datetime.now(timezone.utc) - started_at
        seconds = int(uptime.total_seconds())
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        e = base_embed("⚙️ SparkingCraft License System", "Estado general del sistema", C_OK)
        e.add_field(name="API", value=f"🟢 Online\n`{api_ms} ms`", inline=True)
        e.add_field(name="Database", value="🟢 Connected", inline=True)
        e.add_field(name="Discord Bot", value=f"🟢 Online\n`{discord_ms} ms`", inline=True)
        e.add_field(name="Productos", value=f"`{data['active_products']}/{data['products']}` activos", inline=True)
        e.add_field(name="Licencias", value=f"`{data['active_licenses']}/{data['licenses']}` activas", inline=True)
        e.add_field(name="Activaciones", value=f"`{data['active_activations']}` activas", inline=True)
        e.add_field(name="Usuarios", value=f"`{data['customers']}`", inline=True)
        e.add_field(name="Deshabilitadas", value=f"`{data['disabled_licenses']}`", inline=True)
        e.add_field(name="Uptime", value=f"`{days}d {hours}h {minutes}m`", inline=True)
        await interaction.followup.send(embed=e, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(embed=friendly_api_error(exc), ephemeral=True)


async def sync_guild_commands(clean_first: bool):
    if not GUILD_ID.isdigit():
        await tree.sync()
        return "global"
    guild = discord.Object(id=int(GUILD_ID))
    if clean_first:
        tree.clear_commands(guild=guild)
        await tree.sync(guild=guild)  # removes old guild slash commands remotely
    tree.copy_global_to(guild=guild)
    synced = await tree.sync(guild=guild)
    return f"guild ({len(synced)} root commands)"


@system_group.command(name="sync", description="Limpia y resincroniza los slash commands")
async def system_sync(interaction: discord.Interaction):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        target = await sync_guild_commands(True)
        await interaction.followup.send(embed=success_embed("Comandos sincronizados", f"Se limpiaron los comandos antiguos y se registró el esquema actual en **{target}**."), ephemeral=True)
    except Exception:
        await interaction.followup.send(embed=error_embed("No se pudo sincronizar", "Revisa los logs del bot y los permisos de la aplicación."), ephemeral=True)


@system_group.command(name="migrate-products", description="Migra slugs antiguos conocidos a los slugs actuales")
async def system_migrate_products(interaction: discord.Interaction):
    if not await guard(interaction): return
    aliases = [("spark-core", "sparkcore"), ("combat", "combatplus")]

    async def confirm(i: discord.Interaction):
        await i.response.defer()
        results = []
        for source, target in aliases:
            try:
                out = await api("POST", "/admin/products/migrate", json={"source_slug": source, "target_slug": target})
                results.append(f"✅ `{source}` → `{target}` • {out['licenses_updated']} licencia(s)")
            except ApiError as exc:
                if exc.status == 404:
                    results.append(f"➖ `{source}` → `{target}` • no aplica")
                else:
                    results.append(f"❌ `{source}` → `{target}` • error")
            except Exception:
                results.append(f"❌ `{source}` → `{target}` • error")
        await i.edit_original_response(embed=success_embed("Migración finalizada", "\n".join(results)), view=None)

    e = warning_embed(
        "Migrar productos antiguos",
        "Se revisarán estas equivalencias:\n\n• `spark-core` → `sparkcore`\n• `combat` → `combatplus`\n\nLas licencias se moverán al producto correcto antes de borrar el slug antiguo.",
    )
    await send_embed(interaction, e, view=ConfirmView(interaction.user.id, confirm, "Migrar"))


# Register grouped commands
tree.add_command(product_group)
tree.add_command(license_group)
tree.add_command(my_group)
tree.add_command(system_group)


def build_presence():
    status_map = {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "invisible": discord.Status.invisible,
    }
    status = status_map.get(STATUS_NAME, discord.Status.online)
    if ACTIVITY_TYPE == "playing":
        activity = discord.Game(name=ACTIVITY_TEXT)
    elif ACTIVITY_TYPE == "listening":
        activity = discord.Activity(type=discord.ActivityType.listening, name=ACTIVITY_TEXT)
    elif ACTIVITY_TYPE == "competing":
        activity = discord.Activity(type=discord.ActivityType.competing, name=ACTIVITY_TEXT)
    else:
        activity = discord.Activity(type=discord.ActivityType.watching, name=ACTIVITY_TEXT)
    return status, activity


@client.event
async def on_ready():
    global commands_synced_once
    status, activity = build_presence()
    await client.change_presence(status=status, activity=activity)
    if not commands_synced_once:
        try:
            target = await sync_guild_commands(CLEAN_SLASH_ON_START)
            print(f"[BOT] Slash commands synced to {target}")
            commands_synced_once = True
        except Exception as exc:
            print(f"[BOT] Slash sync failed: {exc!r}")
    auth_mode = "DISCORD_API_TOKEN" if DISCORD_API_TOKEN else "ADMIN_TOKEN fallback"
    print(f"[AUTH] Discord API authentication: {auth_mode}")
    print(f"[API] Internal URL: {API_URL}")
    print(f"[BOT] Presence: {ACTIVITY_TYPE} {ACTIVITY_TEXT}")
    print(f"[BOT] Ready as {client.user}")


client.run(TOKEN)
