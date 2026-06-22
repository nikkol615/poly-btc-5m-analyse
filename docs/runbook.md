# Runbook

What to do when something is on fire.

## Disk full → Postgres in restart loop

**Symptom**: `docker logs <postgres>` shows
`FATAL: could not write lock file "postmaster.pid": No space left on device`,
repeating once per minute.

**Triage**:
```bash
df -h /                                              # confirm 100% used
du -h -d 1 / 2>/dev/null | sort -hr | head -10       # find the elephant
du -h -d 1 /var/lib/docker/volumes 2>/dev/null | sort -hr | head
docker system df
```

**Free space without losing data, in priority order**:
1. `journalctl --vacuum-size=100M` (often 2-3 GB)
2. `find /var/log -type f \( -name "*.log.*" -o -name "*.gz" \) -delete`
3. `find /var/log -type f -name "*.log" -exec truncate -s 0 {} \;`
4. `apt-get clean && rm -rf /var/cache/apt/archives/*`
5. `docker image prune -af --filter "until=24h"`
6. `find /data/coolify/backups -type f -mtime +1 -delete`
7. Identify any application volumes / models you don't need
   (`/data/coolify/applications/<uuid>/...`)
8. Truncate stuck container's log file from host (path from
   `find / -size +500M`)

Once you have **3-5 GB free**, Postgres will complete recovery on its own
within minutes. Don't restart it manually — that just resets the recovery.

**After Postgres comes up**: it accepts connections. Go to "DB status" page in
the Streamlit app for current breakdown, or shell into the DB:

```bash
docker exec -it <postgres_container> psql -U postgres -d poly_btc
\dt+                                  # tables with sizes
SELECT pg_size_pretty(pg_total_relation_size('pm_book'));
```

If `pm_book` is the elephant (it usually is), see the next section.

## `pm_book` ate all the disk

Three options, listed from "fastest+destructive" to "slowest+lossless":

### A. `TRUNCATE pm_book`
- Instant, returns 100% of pm_book's disk to the OS
- Keeps `markets`, `btc_spot`, `pm_trades` (so resolved windows are still
  there for the backtester — only entry prices are gone)
- Use when disk is critical and you can afford to lose recent book data
- After truncate, redeploy the collector (post-2026-06 code writes 3-4× less
  per market)

### B. Rebuild table keeping only desired rows
```sql
CREATE TABLE pm_book_new (LIKE pm_book INCLUDING ALL);
INSERT INTO pm_book_new
  SELECT * FROM pm_book WHERE event_type IN ('book','best_bid_ask','compact');
DROP TABLE pm_book;
ALTER TABLE pm_book_new RENAME TO pm_book;
```
Requires `~1×` table size free during INSERT. Verify with `df -h` first.

### C. Delete unwanted in chunks, no disk return
```sql
DELETE FROM pm_book WHERE event_type = 'price_change'
  AND ctid IN (SELECT ctid FROM pm_book WHERE event_type = 'price_change' LIMIT 100000);
VACUUM pm_book;
-- loop
```
Frees pages for reuse but does NOT shrink the file. Useless for a disk-full
situation alone. Useful if you want to keep history but reclaim future writes.

## Postgres up, but collector can't connect

**Symptom**: collector logs show
`server closed the connection unexpectedly`,
yet `docker exec <postgres> pg_isready` succeeds.

**Cause**: collector's `DATABASE_URL` points at the host's public IP, but
Postgres is no longer published to the host. Hairpin-NAT path is broken.

**Verify**:
```bash
docker port <postgres_container>                              # empty = not published
docker inspect <postgres_container> --format \
  '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}'
docker inspect <collector_container>  --format \
  '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}'
# both should show coolify=...
```

**Fix**: in Coolify env for `poly-btc-collector` and `poly-btc-app`, change
DATABASE_URL host from the public IP to the Postgres container name. Redeploy.

For local development from your laptop, set up an SSH tunnel:
```bash
ssh -L 5432:<postgres_container>:5432 root@<server_ip> -N &
```

## Coolify won't load / restart loop

**Symptom**: `docker ps` shows `coolify` as `Restarting (...)`.

**Triage**:
```bash
docker logs coolify --tail 100
# Look for: out of disk, DB connection failed, PHP fatal, etc.
```

If the issue is disk → fix disk first, Coolify will restart cleanly.
If the issue is its own Postgres (`coolify-db` container) → also a disk
or recovery issue, same playbook.

If neither — try a one-time manual restart: `docker restart coolify`. Wait
30s, check `docker ps`. Browse to the UI.

## Lost SSH password, can't get in

Use the Vertex web console (VNC). In the panel: VPS → console → web terminal.
There you can reset root password without SSH. The VPS doesn't reboot, but the
new password takes effect immediately for next SSH attempt.

**Before doing anything destructive**, create a snapshot in the Vertex panel.
Snapshots are taken live and save the disk image — your safety net if
something goes wrong while you're poking around.

## Other useful one-liners

```bash
# Top files >100MB outside Docker
find / -xdev -type f -size +100M 2>/dev/null | xargs ls -lah 2>/dev/null | sort -k5 -hr | head -20

# Total Coolify resource for the database directory
docker exec <postgres_container> du -sh /var/lib/postgresql/data

# WAL size (should normally be <1-2 GB after checkpoint)
docker exec <postgres_container> du -sh /var/lib/postgresql/data/pg_wal

# Active connections vs limit
docker exec <postgres_container> psql -U postgres -c \
  "SELECT count(*) AS active, current_setting('max_connections') FROM pg_stat_activity;"
```
