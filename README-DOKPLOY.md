# Sparking License API — despliegue en Dokploy

Esta variante está preparada para **Dokploy**. No incluye Caddy porque Dokploy ya enruta los dominios mediante Traefik.

## 1. DNS

Mantén el registro A:

- Host: `api`
- Dominio: `api.sparkingcraft.com`
- IPv4: `199.127.60.243`

## 2. Crear el servicio en Dokploy

1. Crea un Project.
2. Añade un servicio **Compose / Docker Compose** apuntando al repositorio donde subas esta carpeta.
3. Usa el `docker-compose.yml` incluido.
4. Activa **Isolated Deployments** si tu instalación de Dokploy ofrece esa opción.

## 3. Variables de entorno

Genera un `.env` localmente con:

```bash
python3 scripts/generate_env.py
```

Luego copia el contenido de `.env` a la pestaña **Environment** del servicio Compose en Dokploy.

Completa además, si usarás Discord:

```env
DISCORD_TOKEN=TOKEN_DEL_BOT
DISCORD_GUILD_ID=ID_DEL_SERVIDOR
DISCORD_ADMIN_ROLE_IDS=ID_ROL_ADMIN
DISCORD_API_TOKEN=
```

`DISCORD_API_TOKEN` puede quedar vacío en el primer despliegue. En ese caso el bot usa `ADMIN_TOKEN`. Después conviene crear una API key dedicada y colocarla ahí.

## 4. Dominio en Dokploy

Después del primer deploy:

1. Abre el Compose.
2. Ve a **Domains**.
3. Añade un dominio al servicio `api`.
4. Host: `api.sparkingcraft.com`
5. Path: `/`
6. Container Port: `8000`
7. HTTPS: `ON`
8. Certificate: `Let's Encrypt`
9. Guarda y redeploy.

No publiques PostgreSQL y no necesitas mapear `8000:8000` al host.

## 5. Comprobación

```bash
curl https://api.sparkingcraft.com/health
```

Debe responder:

```json
{"status":"ok","service":"sparking-license-api"}
```

La clave pública de firma se obtiene una sola vez con:

```bash
curl https://api.sparkingcraft.com/v1/public-key
```

Esa clave pública debe quedar **embebida/pinneada en el plugin Minecraft**. No la descargues y confíes en ella dinámicamente en cada inicio.

## 6. Crear producto

Usa el `ADMIN_TOKEN` del entorno:

```bash
curl -X POST https://api.sparkingcraft.com/admin/products \
  -H "Authorization: Bearer TU_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"slug":"mi-plugin","name":"Mi Plugin"}'
```

## 7. Crear API key exclusiva para Discord

```bash
curl -X POST https://api.sparkingcraft.com/admin/api-keys \
  -H "Authorization: Bearer TU_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"discord-bot"}'
```

La respuesta contiene un token `spark_admin_...`. Guárdalo en Dokploy como:

```env
DISCORD_API_TOKEN=spark_admin_...
```

Redeploy y desde ese momento el bot deja de utilizar el bootstrap `ADMIN_TOKEN`.

## 8. Persistencia

Los datos importantes están en dos named volumes:

- `pgdata`: PostgreSQL.
- `signing_keys`: clave privada/pública RSA de la API.

**Haz backup de ambos.** Si pierdes `signing_keys`, los tokens/cache firmados previamente dejarán de validar contra la nueva clave.

## 9. Servicios

- `db`: PostgreSQL interno.
- `api`: FastAPI, puerto interno `8000`.
- `discord_bot`: bot opcional. Si `DISCORD_TOKEN` está vacío termina limpiamente y no afecta a la API.

## 10. Integración Minecraft

Usa `examples/java8/SignedLicenseVerifier.java` como base de verificación RSA. El proyecto está planteado para clientes Java 8, por lo que sirve para plugins legacy de Bukkit/Spigot 1.8.8 además de versiones modernas.
