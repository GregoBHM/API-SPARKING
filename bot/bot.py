import os
from datetime import datetime, timedelta, timezone

import discord
import httpx
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN:
    print("DISCORD_TOKEN is empty; Discord bot is disabled.")
    raise SystemExit(0)
API_URL = os.getenv("API_INTERNAL_URL", "http://api:8000").rstrip("/")
API_TOKEN = os.getenv("DISCORD_API_TOKEN") or os.environ["ADMIN_TOKEN"]
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
ADMIN_ROLE_IDS = {
    int(x.strip()) for x in os.getenv("DISCORD_ADMIN_ROLE_IDS", "").split(",") if x.strip().isdigit()
}

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def allowed(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    return any(role.id in ADMIN_ROLE_IDS for role in interaction.user.roles)


async def guard(interaction: discord.Interaction) -> bool:
    if allowed(interaction):
        return True
    await interaction.response.send_message("No tienes permisos para administrar licencias.", ephemeral=True)
    return False


async def api(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {API_TOKEN}"
    async with httpx.AsyncClient(timeout=15.0) as http:
        response = await http.request(method, API_URL + path, headers=headers, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"API {response.status_code}: {response.text}")
    return response.json()


@tree.command(name="product_create", description="Crea un producto/plugin licenciable")
@app_commands.describe(slug="ID corto, ejemplo sparking-duels", name="Nombre visible")
async def product_create(interaction: discord.Interaction, slug: str, name: str):
    if not await guard(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        data = await api("POST", "/admin/products", json={"slug": slug.lower(), "name": name})
        await interaction.followup.send(f"Producto creado: `{data['slug']}` ({data['id']})", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(str(exc), ephemeral=True)


@tree.command(name="license_create", description="Crea una licencia")
@app_commands.describe(
    products="Slugs separados por coma",
    max_activations="Cantidad máxima de servidores",
    expires_days="0 = permanente",
)
async def license_create(
    interaction: discord.Interaction,
    products: str,
    max_activations: app_commands.Range[int, 1, 1000] = 1,
    expires_days: app_commands.Range[int, 0, 3650] = 0,
):
    if not await guard(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    expires_at = None
    license_type = "LIFETIME"
    if expires_days > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat()
        license_type = "SUBSCRIPTION"
    payload = {
        "product_slugs": [x.strip().lower() for x in products.split(",") if x.strip()],
        "license_type": license_type,
        "max_activations": max_activations,
        "expires_at": expires_at,
    }
    try:
        data = await api("POST", "/admin/licenses", json=payload)
        msg = (
            f"Licencia creada\n"
            f"`{data['license_key']}`\n"
            f"ID: `{data['id']}`\n"
            f"Productos: `{', '.join(data['products'])}`\n"
            f"Activaciones: `{data['active_activations']}/{data['max_activations']}`"
        )
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(str(exc), ephemeral=True)


@tree.command(name="license_find", description="Busca licencias por ID, últimos 4, producto o cliente")
async def license_find(interaction: discord.Interaction, query: str):
    if not await guard(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        rows = await api("GET", "/admin/licenses", params={"q": query})
        if not rows:
            await interaction.followup.send("No encontré licencias.", ephemeral=True)
            return
        lines = []
        for x in rows[:10]:
            lines.append(
                f"`{x['id']}` • `{x['masked_key']}` • {x['status']} • "
                f"{x['active_activations']}/{x['max_activations']} • {', '.join(x['products'])}"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(str(exc), ephemeral=True)


@tree.command(name="license_disable", description="Desactiva una licencia por UUID")
async def license_disable(interaction: discord.Interaction, license_id: str):
    if not await guard(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        data = await api("PATCH", f"/admin/licenses/{license_id}", json={"status": "DISABLED"})
        await interaction.followup.send(f"Licencia `{data['id']}` desactivada.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(str(exc), ephemeral=True)


@tree.command(name="license_enable", description="Activa una licencia por UUID")
async def license_enable(interaction: discord.Interaction, license_id: str):
    if not await guard(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        data = await api("PATCH", f"/admin/licenses/{license_id}", json={"status": "ACTIVE"})
        await interaction.followup.send(f"Licencia `{data['id']}` activada.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(str(exc), ephemeral=True)


@tree.command(name="license_reset", description="Libera todas las activaciones de una licencia")
async def license_reset(interaction: discord.Interaction, license_id: str):
    if not await guard(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        data = await api("POST", f"/admin/licenses/{license_id}/reset-activations")
        await interaction.followup.send(
            f"Activaciones liberadas. Ahora: `{data['active_activations']}/{data['max_activations']}`",
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(str(exc), ephemeral=True)


@client.event
async def on_ready():
    if GUILD_ID.isdigit():
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    print(f"Discord bot ready as {client.user}")


client.run(TOKEN)
