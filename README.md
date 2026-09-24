# Sparking License API

Sistema de licencias para plugins de Minecraft preparado para desplegarse con Docker en **api.sparkingcraft.com**.

Incluye:

- FastAPI como backend.
- PostgreSQL persistente.
- HTTPS automático con Caddy + Let's Encrypt.
- Licencias por producto, expiración y límite de servidores.
- Activación, verificación, heartbeat y desactivación.
- Respuestas firmadas con RSA-3072 / SHA256withRSA (RS256), compatibles con Java 8.
- Caché offline posible mediante `valid_until` dentro del token firmado.
- Fingerprint opcionalmente vinculante.
- Auditoría.
- API administrativa con token bootstrap y API keys revocables.
- Bot de Discord opcional para crear/buscar/desactivar/resetear licencias.
- Scripts de backup/restore de PostgreSQL.

## 1. Requisitos del servidor

- Ubuntu/Debian recomendado.
- Docker Engine + Docker Compose plugin.
- Puertos TCP 80 y 443 abiertos hacia Internet.
- El DNS `api.sparkingcraft.com` debe apuntar a `199.127.60.243`.
- No necesitas exponer PostgreSQL: el compose no publica el puerto 5432.

## 2. Primera instalación

```bash
cd sparking-license-api
python3 scripts/generate_env.py
nano .env
```

Revisa especialmente:

```env
DOMAIN=api.sparkingcraft.com
```

Luego:

```bash
docker compose up -d --build
```

Comprueba:

```bash
curl https://api.sparkingcraft.com/health
```

Debe devolver algo similar a:

```json
{"status":"ok","service":"sparking-license-api"}
```

## 3. Clave pública para tus plugins

La primera vez que arranca la API se genera un par RSA dentro del volumen Docker `signing_keys`.

Obtén la clave pública:

```bash
curl https://api.sparkingcraft.com/v1/public-key
```

O directamente desde el contenedor:

```bash
docker compose exec api cat /app/keys/public.pem
```

**Pínala dentro de tus plugins.** El plugin debe verificar la firma con esa clave. Nunca incluyas `private.pem` dentro de un JAR.

No borres el volumen `signing_keys` ni reemplaces la private key sin planificar una rotación, porque tus plugins dejarían de reconocer las nuevas respuestas.

## 4. Autenticación administrativa

El `.env` contiene:

```env
ADMIN_TOKEN=spark_bootstrap_...
```

Úsalo como:

```bash
-H "Authorization: Bearer TU_ADMIN_TOKEN"
```

Ese token es el bootstrap. Para bots/integraciones es mejor crear una API key separada usando `/admin/api-keys` y luego colocar esa key en `DISCORD_API_TOKEN`.

## 5. Crear un producto

```bash
curl -X POST https://api.sparkingcraft.com/admin/products \
  -H "Authorization: Bearer TU_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name":"Sparking Duels",
    "slug":"sparking-duels"
  }'
```

## 6. Crear una licencia

```bash
curl -X POST https://api.sparkingcraft.com/admin/licenses \
  -H "Authorization: Bearer TU_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "product_slugs":["sparking-duels"],
    "license_type":"LIFETIME",
    "max_activations":1,
    "offline_grace_hours":72,
    "expires_at":null
  }'
```

La respuesta mostrará `license_key` **solo en ese momento**. En la base de datos se almacena un HMAC SHA-256, no la clave en texto plano.

Ejemplo de key:

```text
SPARK-ABCDE-FG234-HJKLM-NP789
```

## 7. Flujo desde un plugin Minecraft

### Activación inicial

`POST /v1/licenses/activate`

```json
{
  "license_key":"SPARK-ABCDE-FG234-HJKLM-NP789",
  "product":"sparking-duels",
  "server_id":"uuid-persistente-del-servidor",
  "fingerprint":"fingerprint-estable",
  "plugin_version":"1.0.0",
  "minecraft_version":"1.8.8",
  "nonce":"valor-aleatorio-unico"
}
```

### Verificación al iniciar

`POST /v1/licenses/verify`

### Heartbeat

`POST /v1/licenses/heartbeat`

Recomendación: cada 15-30 minutos, nunca cada tick ni cada segundo.

### Liberar activación

`POST /v1/licenses/deactivate`

## 8. Respuesta firmada

La API no devuelve un simple `valid: true`.

Devuelve:

```json
{
  "token":"BASE64URL_DEL_PAYLOAD",
  "signature":"FIRMA_BASE64URL",
  "algorithm":"RS256"
}
```

El plugin debe:

1. Verificar `signature` sobre los bytes ASCII exactos de `token` usando la public key fijada dentro del JAR.
2. Solo si la firma es válida, decodificar `token` como Base64 URL-safe.
3. Parsear el JSON interno.
4. Comprobar `nonce`, `server_id`, `product`, `status`, `issued_at` y `valid_until`.

Payload válido aproximado:

```json
{
  "ok":true,
  "status":"VALID",
  "license_id":"...",
  "activation_id":"...",
  "product":"sparking-duels",
  "server_id":"...",
  "nonce":"...",
  "issued_at":1790000000,
  "valid_until":1790259200,
  "expires_at":null,
  "message":"License is valid"
}
```

Hay un ejemplo Java 8 en:

```text
examples/java8/SignedLicenseVerifier.java
```

## 9. Estados principales

- `VALID`
- `INVALID_LICENSE`
- `LICENSE_DISABLED`
- `LICENSE_EXPIRED`
- `INVALID_PRODUCT`
- `ACTIVATION_REQUIRED`
- `ACTIVATION_LIMIT`
- `FINGERPRINT_MISMATCH`

Los rechazos también se firman, de forma que el cliente use el mismo formato.

## 10. Fingerprint

Por defecto:

```env
ACTIVATION_BIND_FINGERPRINT=true
```

Si un mismo `server_id` reaparece con otro fingerprint, se rechaza. Si tus clientes usan hostings que recrean contenedores con frecuencia, puedes ponerlo en `false` y usar principalmente el `server_id` persistente.

Recomendación para Minecraft: crear una vez un UUID y guardarlo en un archivo como:

```text
plugins/TuPlugin/server-id.dat
```

No uses únicamente IP como identidad del servidor.

## 11. Discord bot

Crea una aplicación/bot en Discord y completa:

```env
DISCORD_TOKEN=...
DISCORD_GUILD_ID=...
DISCORD_ADMIN_ROLE_IDS=123456789012345678,987654321098765432
DISCORD_API_TOKEN=...
```

`DISCORD_ADMIN_ROLE_IDS` es una lista separada por comas. Los usuarios con permiso Administrator también son aceptados.

Para lanzar API + bot:

```bash
docker compose --profile discord up -d --build
```

Comandos incluidos:

- `/product_create`
- `/license_create`
- `/license_find`
- `/license_disable`
- `/license_enable`
- `/license_reset`

Las respuestas sensibles del bot son `ephemeral`.

## 12. Crear una API key dedicada para Discord

```bash
curl -X POST https://api.sparkingcraft.com/admin/api-keys \
  -H "Authorization: Bearer TU_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"discord-bot"}'
```

Copia el `token` devuelto a:

```env
DISCORD_API_TOKEN=spark_admin_...
```

Reinicia solo el bot:

```bash
docker compose --profile discord up -d --build discord_bot
```

## 13. Backups

Crear backup:

```bash
./scripts/backup.sh
```

Restaurar:

```bash
./scripts/restore.sh backups/sparking_licenses-YYYYMMDD-HHMMSS.sql.gz
```

Además de PostgreSQL, respalda de forma segura tu `.env` y la private key del volumen `signing_keys`.

Para copiar las keys al host si necesitas una copia de seguridad controlada:

```bash
docker compose exec -T api cat /app/keys/private.pem > private.pem.backup
docker compose exec -T api cat /app/keys/public.pem > public.pem.backup
chmod 600 private.pem.backup
```

No subas esas copias a GitHub.

## 14. Actualización habitual

```bash
docker compose build --pull
docker compose up -d
```

## 15. Endpoints

Plugin/API pública:

```text
GET  /health
GET  /v1/public-key
POST /v1/licenses/activate
POST /v1/licenses/verify
POST /v1/licenses/heartbeat
POST /v1/licenses/deactivate
```

Administración:

```text
GET    /admin/products
POST   /admin/products
GET    /admin/customers
POST   /admin/customers
GET    /admin/licenses
GET    /admin/licenses/{uuid}
POST   /admin/licenses
PATCH  /admin/licenses/{uuid}
POST   /admin/licenses/{uuid}/rotate
POST   /admin/licenses/{uuid}/reset-activations
GET    /admin/licenses/{uuid}/activations
GET    /admin/audit
GET    /admin/api-keys
POST   /admin/api-keys
DELETE /admin/api-keys/{uuid}
```

## 16. Seguridad importante

- Mantén `ADMIN_TOKEN`, `LICENSE_HASH_SECRET` y `private.pem` fuera de Git.
- `LICENSE_HASH_SECRET` no debe cambiar después de emitir licencias; cambiarlo hace que las claves existentes dejen de encontrarse.
- La private key de firma tampoco debe cambiar sin actualizar la public key incrustada en los plugins.
- Caddy es el único servicio publicado; PostgreSQL y FastAPI permanecen en la red interna Docker.
- Una licencia en un JAR Java nunca puede ser 100% imposible de crackear. La firma, la validación remota, los heartbeats y la ofuscación elevan considerablemente el coste de bypass.
- No hagas llamadas HTTP en el main thread de Bukkit/Spigot; usa una tarea asíncrona y aplica el resultado de forma segura al hilo principal cuando sea necesario.

## 17. Próximos pasos recomendados

La base está preparada para añadir después un panel web, webhooks de Discord, métricas, planes/ventas, renovaciones automáticas, scopes reales por API key, Redis para rate limiting distribuido y un SDK Java completo compartido entre todos tus plugins.
