import os
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import discord
import httpx
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN:
    print("DISCORD_TOKEN is empty; Discord bot is disabled.")
    raise SystemExit(0)

API_URL = os.getenv("API_INTERNAL_URL", "http://api:8000").rstrip("/")
API_TOKEN = (os.getenv("DISCORD_API_TOKEN") or os.environ["ADMIN_TOKEN"]).strip()
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
ADMIN_ROLE_IDS = {
    int(x.strip()) for x in os.getenv("DISCORD_ADMIN_ROLE_IDS", "").split(",") if x.strip().isdigit()
}
CLEAN_SLASH_ON_START = os.getenv("DISCORD_CLEAN_SLASH_ON_START", "true").lower() in {"1", "true", "yes", "on"}
DM_LICENSE_ON_CREATE = os.getenv("DISCORD_LICENSE_DM_ON_CREATE", "true").lower() in {"1", "true", "yes", "on"}

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
_synced_once = False


class ApiError(RuntimeError):
    def __init__(self, status_code: int, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(self._message())

    def _message(self) -> str:
        if isinstance(self.detail, dict):
            if "message" in self.detail:
                return str(self.detail["message"])
            if "detail" in self.detail:
                return str(self.detail["detail"])
        return str(self.detail)


async def api(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {API_TOKEN}"
    async with httpx.AsyncClient(timeout=15.0) as http:
        response = await http.request(method, API_URL + path, headers=headers, **kwargs)

    if response.status_code >= 400:
        try:
            body = response.json()
            detail = body.get("detail", body)
        except Exception:
            detail = response.text
        raise ApiError(response.status_code, detail)

    if not response.content:
        return None
    try:
        return response.json()
    except Exception:
        return response.text


def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    return any(role.id in ADMIN_ROLE_IDS for role in interaction.user.roles)


async def guard_admin(interaction: discord.Interaction) -> bool:
    if is_admin(interaction):
        return True
    if interaction.response.is_done():
        await interaction.followup.send("No tienes permisos para administrar licencias.", ephemeral=True)
    else:
        await interaction.response.send_message("No tienes permisos para administrar licencias.", ephemeral=True)
    return False


def fmt_date(value: Optional[str]) -> str:
    if not value:
        return "Nunca"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y %H:%M UTC")
    except Exception:
        return str(value)


def short_server_id(value: str) -> str:
    if len(value) <= 12:
        return value
    return f"{value[:6]}…{value[-6:]}"


def product_lines(products: list[dict]) -> str:
    if not products:
        return "No hay productos creados."
    lines = []
    for p in products[:50]:
        icon = "✅" if p.get("active") else "⛔"
        lines.append(f"{icon} **{p['name']}** — `{p['slug']}`")
    if len(products) > 50:
        lines.append(f"… y {len(products) - 50} más")
    return "\n".join(lines)


async def ensure_customer(member: discord.Member) -> dict:
    return await api(
        "POST",
        "/admin/customers/ensure-discord",
        json={"discord_id": str(member.id), "username": str(member)},
    )


async def customer_for_user(user_id: int) -> Optional[dict]:
    try:
        return await api("GET", f"/admin/customers/by-discord/{user_id}")
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        raise


async def licenses_for_user(user_id: int) -> list[dict]:
    customer = await customer_for_user(user_id)
    if not customer:
        return []
    return await api("GET", f"/admin/customers/{customer['id']}/licenses")


async def get_product_by_slug(slug: str) -> Optional[dict]:
    products = await api("GET", "/admin/products")
    normalized = slug.strip().lower()
    return next((p for p in products if p["slug"] == normalized), None)


class OwnerView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Este menú no te pertenece.", ephemeral=True)
            return False
        return True


class ProductSelectionView(OwnerView):
    def __init__(
        self,
        owner_id: int,
        products: list[dict],
        title: str,
        on_confirm: Callable[[list[str]], Awaitable[str]],
        preselected: Optional[list[str]] = None,
    ):
        super().__init__(owner_id)
        self.on_confirm = on_confirm
        self.selected = set(preselected or [])
        active_products = [p for p in products if p.get("active")]
        if len(active_products) > 25:
            raise ValueError("Discord permite como máximo 25 productos en este selector.")
        if not active_products:
            raise ValueError("No hay productos activos.")

        options = [
            discord.SelectOption(
                label=p["name"][:100],
                value=p["slug"],
                description=p["slug"][:100],
                default=p["slug"] in self.selected,
            )
            for p in active_products
        ]
        select = discord.ui.Select(
            placeholder=title,
            min_values=1,
            max_values=len(options),
            options=options,
        )
        select.callback = self._select_callback
        self.select = select
        self.add_item(select)

    async def _select_callback(self, interaction: discord.Interaction):
        self.selected = set(self.select.values)
        await interaction.response.defer()

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        selected = sorted(self.selected or set(self.select.values))
        if not selected:
            await interaction.response.send_message("Selecciona al menos un producto.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.on_confirm(selected)
        except Exception as exc:
            message = f"❌ {exc}"
        await interaction.edit_original_response(content=message, view=None)
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Operación cancelada.", view=None)
        self.stop()


class ConfirmView(OwnerView):
    def __init__(self, owner_id: int, on_confirm: Callable[[], Awaitable[str]], confirm_label: str = "Eliminar"):
        super().__init__(owner_id)
        self.on_confirm = on_confirm
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "confirm_action":
                child.label = confirm_label

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger, custom_id="confirm_action")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.on_confirm()
        except Exception as exc:
            message = f"❌ {exc}"
        await interaction.edit_original_response(content=message, view=None)
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Operación cancelada.", view=None)
        self.stop()


class MyLicenseSelectView(OwnerView):
    def __init__(
        self,
        owner_id: int,
        licenses: list[dict],
        placeholder: str,
        on_select: Callable[[dict], Awaitable[str]],
        danger: bool = False,
    ):
        super().__init__(owner_id)
        self.licenses = {x["id"]: x for x in licenses[:25]}
        self.on_select = on_select
        options = [
            discord.SelectOption(
                label=f"{x['masked_key']} • {x['status']}"[:100],
                value=x["id"],
                description=(", ".join(x.get("products", [])) or "Sin productos")[:100],
            )
            for x in licenses[:25]
        ]
        select = discord.ui.Select(placeholder=placeholder, min_values=1, max_values=1, options=options)
        select.callback = self._select_callback
        self.add_item(select)
        self.danger = danger

    async def _select_callback(self, interaction: discord.Interaction):
        lic = self.licenses[self.children[0].values[0]]
        if not self.danger:
            await interaction.response.defer(ephemeral=True)
            try:
                message = await self.on_select(lic)
            except Exception as exc:
                message = f"❌ {exc}"
            await interaction.edit_original_response(content=message, view=None)
            self.stop()
            return

        async def do_confirm() -> str:
            return await self.on_select(lic)

        confirm_view = ConfirmView(interaction.user.id, do_confirm, confirm_label="Liberar servidores")
        await interaction.response.edit_message(
            content=(
                f"⚠️ Vas a liberar **todas las activaciones** de `{lic['masked_key']}`.\n"
                f"Actualmente usa `{lic['active_activations']}/{lic['max_activations']}` servidores.\n"
                "Esta acción tiene cooldown para usuarios."
            ),
            view=confirm_view,
        )
        self.stop()


product_group = app_commands.Group(name="product", description="Administración de productos")
license_group = app_commands.Group(name="license", description="Administración de licencias")
my_group = app_commands.Group(name="my", description="Tus licencias")


# -------------------- /product --------------------

@product_group.command(name="list", description="Lista todos los productos")
async def product_list(interaction: discord.Interaction):
    if not await guard_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        products = await api("GET", "/admin/products")
        await interaction.followup.send("**Productos SparkingCraft**\n" + product_lines(products), ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@product_group.command(name="create", description="Crea un producto/plugin licenciable")
@app_commands.describe(slug="ID interno: rpgitems, sparkcore...", name="Nombre visible")
async def product_create(interaction: discord.Interaction, slug: str, name: str):
    if not await guard_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        data = await api("POST", "/admin/products", json={"slug": slug.lower().strip(), "name": name.strip()})
        await interaction.followup.send(f"✅ Producto creado: **{data['name']}** (`{data['slug']}`)", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@product_group.command(name="info", description="Muestra un producto")
async def product_info(interaction: discord.Interaction, slug: str):
    if not await guard_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        p = await get_product_by_slug(slug)
        if not p:
            await interaction.followup.send("Producto no encontrado.", ephemeral=True)
            return
        await interaction.followup.send(
            f"**{p['name']}**\nSlug: `{p['slug']}`\nEstado: {'✅ Activo' if p['active'] else '⛔ Desactivado'}\nID: `{p['id']}`",
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


async def set_product_active(interaction: discord.Interaction, slug: str, active: bool):
    if not await guard_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        p = await get_product_by_slug(slug)
        if not p:
            await interaction.followup.send("Producto no encontrado.", ephemeral=True)
            return
        data = await api("PATCH", f"/admin/products/{p['id']}", json={"active": active})
        await interaction.followup.send(
            f"{'✅' if active else '⛔'} `{data['slug']}` {'activado' if active else 'desactivado'}.",
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@product_group.command(name="enable", description="Activa un producto")
async def product_enable(interaction: discord.Interaction, slug: str):
    await set_product_active(interaction, slug, True)


@product_group.command(name="disable", description="Desactiva un producto")
async def product_disable(interaction: discord.Interaction, slug: str):
    await set_product_active(interaction, slug, False)


@product_group.command(name="delete", description="Elimina un producto sin licencias asociadas")
async def product_delete(interaction: discord.Interaction, slug: str):
    if not await guard_admin(interaction):
        return
    try:
        p = await get_product_by_slug(slug)
        if not p:
            await interaction.response.send_message("Producto no encontrado.", ephemeral=True)
            return

        async def do_delete() -> str:
            await api("DELETE", f"/admin/products/{p['id']}")
            return f"✅ Producto `{p['slug']}` eliminado."

        await interaction.response.send_message(
            f"⚠️ ¿Eliminar **{p['name']}** (`{p['slug']}`)?\nSi alguna licencia lo usa, la API bloqueará el borrado.",
            view=ConfirmView(interaction.user.id, do_delete),
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


# -------------------- /license --------------------

@license_group.command(name="create", description="Crea una licencia para un usuario y permite seleccionar productos")
@app_commands.describe(user="Cliente de Discord", max_activations="Máximo de servidores", expires_days="0 = permanente")
async def license_create(
    interaction: discord.Interaction,
    user: discord.Member,
    max_activations: app_commands.Range[int, 1, 1000] = 1,
    expires_days: app_commands.Range[int, 0, 3650] = 0,
):
    if not await guard_admin(interaction):
        return
    try:
        products = await api("GET", "/admin/products")
        active_products = [p for p in products if p.get("active")]
        if not active_products:
            await interaction.response.send_message("No hay productos activos. Crea uno con `/product create`.", ephemeral=True)
            return

        async def do_create(selected: list[str]) -> str:
            customer = await ensure_customer(user)
            expires_at = None
            license_type = "LIFETIME"
            if expires_days > 0:
                expires_at = (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat()
                license_type = "SUBSCRIPTION"
            payload = {
                "product_slugs": selected,
                "customer_id": customer["id"],
                "license_type": license_type,
                "max_activations": max_activations,
                "expires_at": expires_at,
            }
            data = await api("POST", "/admin/licenses", json=payload)
            dm_status = ""
            if DM_LICENSE_ON_CREATE:
                try:
                    await user.send(
                        "**Tu licencia SparkingCraft**\n"
                        f"`{data['license_key']}`\n"
                        f"Productos: `{', '.join(data['products'])}`\n"
                        f"Servidores: `{data['max_activations']}`\n"
                        f"Expira: `{fmt_date(data.get('expires_at'))}`\n\n"
                        "Guárdala. La clave completa solo se muestra al crearla o rotarla."
                    )
                    dm_status = "\n📩 Se envió la licencia al usuario por DM."
                except Exception:
                    dm_status = "\n⚠️ No pude enviar DM; entrégale la clave manualmente."
            return (
                "✅ **Licencia creada**\n"
                f"Usuario: {user.mention}\n"
                f"Clave: `{data['license_key']}`\n"
                f"ID: `{data['id']}`\n"
                f"Productos: `{', '.join(data['products'])}`\n"
                f"Servidores: `{data['active_activations']}/{data['max_activations']}`\n"
                f"Expira: `{fmt_date(data.get('expires_at'))}`"
                f"{dm_status}"
            )

        view = ProductSelectionView(
            interaction.user.id,
            products,
            "Selecciona todos los productos de la licencia",
            do_create,
        )
        await interaction.response.send_message(
            f"Crear licencia para {user.mention}\nMáximo de servidores: `{max_activations}`\nExpiración: `{'Permanente' if expires_days == 0 else str(expires_days) + ' días'}`\n\n"
            f"**Productos registrados:**\n{product_lines(products)}\n\nSelecciona los productos activos que incluirá la licencia:",
            view=view,
            ephemeral=True,
        )
    except Exception as exc:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


@license_group.command(name="list", description="Lista licencias; opcionalmente filtra por usuario")
async def license_list(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    if not await guard_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        if user:
            rows = await licenses_for_user(user.id)
            title = f"Licencias de {user.mention}"
        else:
            rows = await api("GET", "/admin/licenses")
            title = "Licencias"
        if not rows:
            await interaction.followup.send("No hay licencias.", ephemeral=True)
            return
        lines = [f"**{title}**"]
        for x in rows[:20]:
            lines.append(
                f"`{x['masked_key']}` • **{x['status']}** • `{x['active_activations']}/{x['max_activations']}` • "
                f"{', '.join(x['products'])}\n`{x['id']}`"
            )
        if len(rows) > 20:
            lines.append(f"… y {len(rows) - 20} más")
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@license_group.command(name="info", description="Muestra los detalles de una licencia")
async def license_info(interaction: discord.Interaction, license_id: str):
    if not await guard_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        lic = await api("GET", f"/admin/licenses/{license_id}")
        activations = await api("GET", f"/admin/licenses/{license_id}/activations")
        active = [a for a in activations if a["status"] == "ACTIVE"]
        servers = "\n".join(
            f"• `{a.get('last_ip') or 'IP desconocida'}` • `{short_server_id(a['server_id'])}` • {a.get('last_product_slug') or '-'}"
            for a in active[:10]
        ) or "Sin servidores activos"
        await interaction.followup.send(
            f"**{lic['masked_key']}**\n"
            f"ID: `{lic['id']}`\n"
            f"Estado: **{lic['status']}**\n"
            f"Productos: `{', '.join(lic['products'])}`\n"
            f"Servidores: `{lic['active_activations']}/{lic['max_activations']}`\n"
            f"Expira: `{fmt_date(lic.get('expires_at'))}`\n"
            f"Cliente ID: `{lic.get('customer_id') or 'Sin asignar'}`\n\n"
            f"**Activaciones activas**\n{servers}",
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@license_group.command(name="products", description="Modifica los productos incluidos en una licencia")
async def license_products(interaction: discord.Interaction, license_id: str):
    if not await guard_admin(interaction):
        return
    try:
        lic = await api("GET", f"/admin/licenses/{license_id}")
        products = await api("GET", "/admin/products")

        async def do_update(selected: list[str]) -> str:
            data = await api("PATCH", f"/admin/licenses/{license_id}", json={"product_slugs": selected})
            return f"✅ Productos actualizados: `{', '.join(data['products'])}`"

        view = ProductSelectionView(
            interaction.user.id,
            products,
            "Selecciona los productos permitidos",
            do_update,
            preselected=lic["products"],
        )
        await interaction.response.send_message(
            f"**{lic['masked_key']}**\n\n**Productos registrados:**\n{product_lines(products)}\n\nMarca los productos activos que debe incluir:",
            view=view,
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


@license_group.command(name="assign", description="Asigna una licencia existente a un usuario")
async def license_assign(interaction: discord.Interaction, license_id: str, user: discord.Member):
    if not await guard_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        customer = await ensure_customer(user)
        data = await api("PATCH", f"/admin/licenses/{license_id}", json={"customer_id": customer["id"]})
        await interaction.followup.send(f"✅ `{data['masked_key']}` asignada a {user.mention}.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@license_group.command(name="edit", description="Edita límite de servidores y/o expiración")
async def license_edit(
    interaction: discord.Interaction,
    license_id: str,
    max_activations: Optional[app_commands.Range[int, 1, 1000]] = None,
    expires_days: Optional[app_commands.Range[int, 0, 3650]] = None,
):
    if not await guard_admin(interaction):
        return
    if max_activations is None and expires_days is None:
        await interaction.response.send_message("Indica `max_activations` y/o `expires_days`.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        payload = {}
        if max_activations is not None:
            payload["max_activations"] = max_activations
        if expires_days is not None:
            if expires_days == 0:
                payload["expires_at"] = None
                payload["license_type"] = "LIFETIME"
            else:
                payload["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat()
                payload["license_type"] = "SUBSCRIPTION"
        data = await api("PATCH", f"/admin/licenses/{license_id}", json=payload)
        await interaction.followup.send(
            f"✅ Actualizada `{data['masked_key']}`\nServidores: `{data['max_activations']}`\nExpira: `{fmt_date(data.get('expires_at'))}`",
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


async def set_license_status(interaction: discord.Interaction, license_id: str, status: str):
    if not await guard_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        data = await api("PATCH", f"/admin/licenses/{license_id}", json={"status": status})
        await interaction.followup.send(f"✅ `{data['masked_key']}` → **{status}**", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@license_group.command(name="enable", description="Activa una licencia")
async def license_enable(interaction: discord.Interaction, license_id: str):
    await set_license_status(interaction, license_id, "ACTIVE")


@license_group.command(name="disable", description="Desactiva una licencia")
async def license_disable(interaction: discord.Interaction, license_id: str):
    await set_license_status(interaction, license_id, "DISABLED")


@license_group.command(name="reset", description="Admin: libera todas las activaciones sin cooldown")
async def license_reset(interaction: discord.Interaction, license_id: str):
    if not await guard_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        data = await api("POST", f"/admin/licenses/{license_id}/reset-activations")
        await interaction.followup.send(
            f"✅ Activaciones liberadas: `{data['active_activations']}/{data['max_activations']}`",
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@license_group.command(name="activation_remove", description="Elimina una activación concreta")
async def license_activation_remove(interaction: discord.Interaction, license_id: str, activation_id: str):
    if not await guard_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await api("DELETE", f"/admin/licenses/{license_id}/activations/{activation_id}")
        await interaction.followup.send("✅ Activación eliminada.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@license_group.command(name="rotate", description="Invalida la clave actual y genera una nueva")
async def license_rotate(interaction: discord.Interaction, license_id: str):
    if not await guard_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        data = await api("POST", f"/admin/licenses/{license_id}/rotate")
        await interaction.followup.send(
            f"✅ Clave rotada. La anterior ya no sirve.\nNueva clave: `{data['license_key']}`\nID: `{data['id']}`",
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@license_group.command(name="delete", description="Elimina definitivamente una licencia")
async def license_delete(interaction: discord.Interaction, license_id: str):
    if not await guard_admin(interaction):
        return
    try:
        lic = await api("GET", f"/admin/licenses/{license_id}")

        async def do_delete() -> str:
            await api("DELETE", f"/admin/licenses/{license_id}")
            return f"✅ Licencia `{lic['masked_key']}` eliminada definitivamente."

        await interaction.response.send_message(
            f"⚠️ ¿Eliminar definitivamente `{lic['masked_key']}`?\nProductos: `{', '.join(lic['products'])}`\nActivaciones: `{lic['active_activations']}`",
            view=ConfirmView(interaction.user.id, do_delete),
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


# -------------------- /my --------------------

@my_group.command(name="licenses", description="Muestra todas tus licencias")
async def my_licenses(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        rows = await licenses_for_user(interaction.user.id)
        if not rows:
            await interaction.followup.send("No tienes licencias asociadas a tu Discord.", ephemeral=True)
            return
        lines = ["**Tus licencias SparkingCraft**"]
        for x in rows[:20]:
            lines.append(
                f"`{x['masked_key']}` • {'✅' if x['status'] == 'ACTIVE' else '⛔'} **{x['status']}**\n"
                f"Productos: `{', '.join(x['products'])}`\n"
                f"Servidores: `{x['active_activations']}/{x['max_activations']}` • Expira: `{fmt_date(x.get('expires_at'))}`\n"
                f"ID: `{x['id']}`"
            )
        await interaction.followup.send("\n\n".join(lines)[:1900], ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@my_group.command(name="license", description="Selecciona una de tus licencias y ve sus detalles")
async def my_license(interaction: discord.Interaction):
    try:
        rows = await licenses_for_user(interaction.user.id)
        if not rows:
            await interaction.response.send_message("No tienes licencias asociadas.", ephemeral=True)
            return

        async def show(lic: dict) -> str:
            return (
                f"**{lic['masked_key']}**\n"
                f"Estado: **{lic['status']}**\n"
                f"Productos: `{', '.join(lic['products'])}`\n"
                f"Servidores: `{lic['active_activations']}/{lic['max_activations']}`\n"
                f"Expira: `{fmt_date(lic.get('expires_at'))}`\n"
                f"ID: `{lic['id']}`"
            )

        await interaction.response.send_message(
            "Selecciona la licencia:",
            view=MyLicenseSelectView(interaction.user.id, rows, "Selecciona una licencia", show),
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


@my_group.command(name="servers", description="Muestra los servidores/activaciones de tus licencias")
async def my_servers(interaction: discord.Interaction):
    try:
        rows = await licenses_for_user(interaction.user.id)
        if not rows:
            await interaction.response.send_message("No tienes licencias asociadas.", ephemeral=True)
            return

        async def show_servers(lic: dict) -> str:
            activations = await api("GET", f"/admin/licenses/{lic['id']}/activations")
            active = [a for a in activations if a["status"] == "ACTIVE"]
            if not active:
                return f"**{lic['masked_key']}**\nNo tiene servidores activos."
            lines = [f"**Servidores de {lic['masked_key']}**"]
            for idx, a in enumerate(active[:20], 1):
                lines.append(
                    f"**#{idx}** IP: `{a.get('last_ip') or 'desconocida'}`\n"
                    f"Server ID: `{short_server_id(a['server_id'])}` • Producto: `{a.get('last_product_slug') or '-'}`\n"
                    f"Minecraft: `{a.get('minecraft_version') or '-'}` • Último uso: `{fmt_date(a.get('last_seen'))}`"
                )
            return "\n\n".join(lines)[:1900]

        await interaction.response.send_message(
            "Selecciona una licencia para ver sus servidores:",
            view=MyLicenseSelectView(interaction.user.id, rows, "Selecciona una licencia", show_servers),
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


@my_group.command(name="clear", description="Libera tus servidores activos respetando el cooldown")
async def my_clear(interaction: discord.Interaction):
    try:
        rows = await licenses_for_user(interaction.user.id)
        if not rows:
            await interaction.response.send_message("No tienes licencias asociadas.", ephemeral=True)
            return

        async def clear_servers(lic: dict) -> str:
            try:
                data = await api(
                    "POST",
                    f"/admin/licenses/{lic['id']}/user-reset-activations",
                    json={"discord_id": str(interaction.user.id)},
                )
                return (
                    f"✅ Servidores liberados para `{lic['masked_key']}`.\n"
                    f"Se liberaron `{data['reset_count']}` activaciones.\n"
                    f"Cooldown: `{data['cooldown_hours']} horas`."
                )
            except ApiError as exc:
                if exc.status_code == 429 and isinstance(exc.detail, dict):
                    next_allowed = exc.detail.get("next_allowed_at")
                    return f"⏳ Ya usaste el clear recientemente. Podrás volver a usarlo: `{fmt_date(next_allowed)}`."
                raise

        await interaction.response.send_message(
            "Selecciona la licencia cuyas activaciones deseas liberar:",
            view=MyLicenseSelectView(
                interaction.user.id,
                rows,
                "Selecciona una licencia",
                clear_servers,
                danger=True,
            ),
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


# Register only the new command tree.
tree.add_command(product_group)
tree.add_command(license_group)
tree.add_command(my_group)


@client.event
async def on_ready():
    global _synced_once
    if _synced_once:
        return

    # Clean remote global slash commands first, while preserving the new local
    # command objects so they can immediately be re-registered below. This also
    # removes stale commands from deployments that previously synced globally.
    if CLEAN_SLASH_ON_START:
        local_global_commands = list(tree.get_commands())
        tree.clear_commands(guild=None)
        await tree.sync()
        for command in local_global_commands:
            tree.add_command(command)

    if GUILD_ID.isdigit():
        guild = discord.Object(id=int(GUILD_ID))

        if CLEAN_SLASH_ON_START:
            # Publish an empty guild set before the new tree. This removes old
            # /product_create, /license_create, /license_find, etc. commands.
            tree.clear_commands(guild=guild)
            await tree.sync(guild=guild)

        # Copy the new grouped commands (/product, /license, /my) to this guild.
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        print(f"Synced {len(synced)} guild command groups to {GUILD_ID}")
    else:
        # Without a guild ID the bot works globally. Syncing may take longer to
        # appear in Discord, so a guild ID is recommended for this project.
        synced = await tree.sync()
        print(f"Synced {len(synced)} global command groups")

    _synced_once = True
    print(f"Discord bot ready as {client.user}")


client.run(TOKEN)
