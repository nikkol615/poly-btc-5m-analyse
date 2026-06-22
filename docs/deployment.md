# Deployment

## Target

Single VPS (Vertex, Ubuntu 24.04), managed by Coolify. All services run as
Docker containers attached to Coolify's `coolify` network.

## Coolify resources

| Resource | Type | Image / Source |
|---|---|---|
| `poly_btc` (Postgres) | Coolify-managed DB | `postgres:17-alpine` |
| `poly-btc-collector` | Coolify Application from GitHub repo | Built from `Dockerfile`, CMD `poly-btc-collector` |
| `poly-btc-app` | Coolify Application from same repo | Same image, CMD `poly-btc-app`, port `8501` published with Traefik labels |

## Networking — important

All three resources are on the **`coolify`** Docker network. Container-to-
container traffic should use the internal hostname (the Postgres container's
name, e.g. `os4sscs4w0s4cwkkskcwcgc0`), **not** the host's public IP.

Why this matters: the Coolify Postgres container is not necessarily published
to the host. When it isn't, connecting via `<public_ip>:5432` returns "server
closed the connection unexpectedly" because the port isn't actually open
externally and some intermediate hop drops the connection. Container name
works because the `coolify` network has internal DNS.

`DATABASE_URL` in the Coolify env for both apps:

```
postgres://postgres:<password>@<postgres_container_name>:5432/poly_btc
```

For local development from your laptop, use an SSH tunnel:

```bash
ssh -L 5432:<postgres_container_name>:5432 root@<server_ip> -N &
# .env locally:
DATABASE_URL=postgres://postgres:<password>@localhost:5432/poly_btc
```

This is the path that doesn't require exposing Postgres publicly.

## Environment variables

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Postgres DSN. See above |
| `GAMMA_API_URL` | `https://gamma-api.polymarket.com` |
| `RTDS_WS_URL` | `wss://ws-live-data.polymarket.com` |
| `CLOB_WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| `DISCOVERY_INTERVAL_SEC` | Default 30 |
| `LOG_LEVEL` | `INFO` |

## Schema migrations

There is no migration framework. `db.apply_schema()` runs `schema.sql` on every
collector startup. The SQL uses `CREATE TABLE IF NOT EXISTS`,
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, and `DROP COLUMN IF EXISTS` so it
is safe to re-run. Adding new columns means editing `schema.sql` and waiting
for the next deploy.

This is a known weakness — see [backlog.md](backlog.md).

## Disk

`/dev/sda2` is 155 GB on the current server. `pm_book` is the dominant table.
With the post-2026-06 collector behavior (no `price_change` writes) and the
7-day compactor, steady-state usage is ~1-2 GB/week. The DB status page
(`poly-btc-app` → "DB status") shows live numbers and a forecast.

When the disk fills:
1. Postgres goes into restart-loop with `could not write lock file
   postmaster.pid: No space left on device`. Coolify keeps trying to restart it.
2. You'll lose external access first (Coolify can't write web logs either).
3. **Recovery**: see [runbook.md](runbook.md).

## Backups

**There are none configured today.** This must change. The Coolify backups
section on the Postgres resource can be enabled to scheduled `pg_dump` to S3
or local. Local backups on the same disk are worse than nothing (false
security). See [backlog.md](backlog.md).
