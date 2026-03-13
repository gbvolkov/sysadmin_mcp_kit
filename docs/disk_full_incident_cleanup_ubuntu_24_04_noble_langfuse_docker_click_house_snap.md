# Disk Full Incident Cleanup (Ubuntu 24.04 Noble) — Langfuse/Docker/ClickHouse/Snap

## Executive summary
- Root filesystem (`/dev/mapper/ubuntu--vg-ubuntu--lv`) hit **100%** utilization, causing `apt update` failures (`write (28: No space left on device)` and `Splitting up ... InRelease ... failed`).
- Primary space drivers:
  - Docker data in `/var/lib/docker` and Docker-related datasets.
  - **ClickHouse system log tables** (`system.trace_log*`, `system.text_log`, etc.) bloating ClickHouse volumes.
  - **Snap Docker** leftover data under `/var/snap/docker` (~91G).
  - Langfuse MinIO bucket `langfuse/events/otel` (~15G) of old event objects.
- Key remediations:
  - Freed space immediately, fixed reserved ext4 blocks on `/`, cleaned Docker, truncated ClickHouse system logs, capped ClickHouse log retention, purged old MinIO objects, removed Snap Docker.
- Final state:
  - Root filesystem: **161G free (66% used)** after removing Snap Docker; later checks remained healthy.
  - Docker usage reduced to sane levels (images ~9.25G, volumes ~4.34G).
  - ClickHouse log growth prevented via TTL config.
  - MinIO/Langfuse events shrunk from ~16G → ~1.3G; total langfuse bucket storage ~2.5G.

---

## Symptoms and initial errors
- `apt update` errors:
  - GPG signature verification failures and “Splitting up ... InRelease into data and signature failed”.
  - Multiple repositories failing with: `write (28: No space left on device)`.

## Root cause
- Root filesystem (`/`) reached 100% utilization:
  - `df -hT` showed `/dev/mapper/ubuntu--vg-ubuntu--lv` at **466G used 464G, 0 avail**.
  - Inodes were not the issue (`df -i` at 21% used).

---

## Recovery actions performed

### 1) Immediate APT cleanup
- Commands:
  - `sudo apt clean`
  - `sudo rm -rf /var/lib/apt/lists/*`
  - `sudo mkdir -p /var/lib/apt/lists/partial`
  - `sudo apt update`
- Result:
  - Not enough space freed; `apt update` still failed due to disk full.

### 2) Identify space hogs on `/`
- Commands:
  - `sudo du -xhd1 / | sort -h`
  - `sudo du -xhd1 /var | sort -h`
  - `sudo du -xhd1 /var/lib | sort -h`
- Findings:
  - `/var` ~228G; `/var/lib/docker` ~110G.
  - `/var/snap` ~94G (later identified as Snap Docker data).

### 3) Docker cleanup
- Commands:
  - `sudo docker system df`
  - `sudo docker system prune -af`
- Result:
  - Reclaimed ~12G, but root still showed `Avail 0` due to ext4 reserved blocks.

### 4) Fix ext4 reserved blocks on `/`
- Check:
  - `sudo tune2fs -l /dev/mapper/ubuntu--vg-ubuntu--lv | egrep 'Block count|Reserved block count|Reserved blocks percentage|Block size'`
  - Reserved blocks: **5,236,352**; block size **4096** (~21.4GiB reserved).
- Change:
  - `sudo tune2fs -m 1 /dev/mapper/ubuntu--vg-ubuntu--lv`
- Result:
  - Freed space immediately (e.g., `Avail 11G`, `Use% 98%`), enabling successful `apt update`.

### 5) System updates
- Commands:
  - `sudo apt upgrade -y`
- Result:
  - Upgraded packages including Docker CE and firmware; no service restarts required.

---

## Langfuse data cleanup (OSS / no EE license)

### Key constraint
- Self-hosted **Langfuse OSS** does not expose “Data Retention” in UI; retention policies are **EE-only**.
- Verified no EE license env var:
  - `LANGFUSE_EE_LICENSE_KEY` not set.

### ClickHouse: real culprit was system logs
- Evidence:
  - ClickHouse `default` tables were small (e.g., `default.observations ~1.10GiB`), but ClickHouse volume was ~36GiB.
  - Biggest tables were system logs:
    - `system.trace_log_0 ~18.72GiB`
    - `system.trace_log_1 ~5.73GiB`
    - `system.trace_log ~5.39GiB`
    - `system.text_log ~1.70GiB`

#### Truncate heavy ClickHouse system log tables
- Command:
  - `TRUNCATE TABLE system.trace_log; system.trace_log_0; system.trace_log_1; system.text_log; system.metric_log; system.metric_log_0; system.asynchronous_metric_log; system.part_log`
- Result:
  - Root filesystem jumped to ~**43G free** and system logs shrank to KB/MB.

#### Prevent re-growth: ClickHouse log TTL configuration
- Added `/etc/clickhouse-server/config.d/langfuse-log-limits.xml`:
  - `query_log` TTL 7 days
  - `text_log` TTL 3 days
  - `trace_log` TTL 1 day
  - `metric_log` TTL 3 days
  - `asynchronous_metric_log` TTL 3 days
  - `part_log` TTL 3 days
- Restarted ClickHouse container and verified log tables remained small.

### MinIO: purge old Langfuse event objects
- Observed MinIO bucket usage:
  - `/data/langfuse/events` ~16G (dominant)
  - `/data/langfuse/exports` ~1.1G
- Since MinIO container lacked `find/awk`, used host volume path:
  - Volume mountpoint: `/var/lib/docker/volumes/langfuse_langfuse_minio_data/_data`
  - Oldest/newest in events: **2025-12-09** to **2026-01-25**.

#### Used `minio/mc` via temporary container (ENTRYPOINT fixed)
- Network:
  - `langfuse_default`
- Command pattern:
  - `docker run --rm --network langfuse_default --entrypoint /bin/sh minio/mc -lc 'mc alias set ...; mc du ...'`

#### Purge `events/otel` older than 14 days
- Before:
  - `langfuse/events/otel` ~15GiB
- Command:
  - `mc rm --recursive --force --older-than 14d lf/langfuse/events/otel`
- After:
  - `langfuse/events/otel` ~349MiB
  - Total `langfuse/events` ~1.3GiB
  - Host `du -sh .../langfuse` ~2.5GiB

#### Automate weekly purge
- Created root-only env file:
  - `/etc/langfuse-minio.env` (mode 600)
- Weekly job:
  - `/etc/cron.weekly/langfuse-minio-events-purge`
  - Sources `/etc/langfuse-minio.env`
  - Runs `mc rm --older-than 14d lf/langfuse/events/otel`
- Verified script execution:
  - `exit=0`

---

## Additional Docker/ClickHouse cleanup for “volkov” stack

### Problem
- `volkov_langfuse_clickhouse_data` volume was ~13.72GiB.
- Top tables were again ClickHouse system logs:
  - `system.trace_log ~11.34GiB`
  - `system.text_log ~600MiB`

### Actions
- Truncated system log tables on `volkov-clickhouse-1`:
  - `TRUNCATE TABLE system.trace_log; system.text_log; system.metric_log; system.asynchronous_metric_log; system.part_log`
- Result:
  - `volkov_langfuse_clickhouse_data` dropped to **~79.99MiB**.
- Added same TTL config and restarted `volkov-clickhouse-1`.

---

## Snap cleanup (major disk recovery)

### Finding
- `/var/snap/docker` consumed ~**91G**.
- `snap list` showed Docker snap installed but disabled:
  - `docker 28.4.0 ... disabled`

### Removal issues and resolution
- Initial `sudo snap remove docker` got stuck on creating an automatic snapshot.
- Cancelled snapshot (Ctrl+C) and used:
  - `sudo snap remove docker --purge`
- Removal failed due to leftover directory:
  - `failed to remove snap "docker" base directory: remove /root/snap/docker: directory not empty`
- Fixed by deleting leftover path:
  - `sudo rm -rf /root/snap/docker`
- Final removal succeeded:
  - `docker removed`
  - `snap list` confirmed: `no snap docker`

### Result
- Root filesystem improved to:
  - `df -h /` → **161G available**, **66% used**.
- `/var` dropped to ~66G total.

---

## Current state (key metrics)
- Root filesystem:
  - `df -h /` observed healthy levels after cleanup (e.g., 66% used, 161G free; later remained stable).
- Docker:
  - `docker system df`:
    - Images ~9.25G
    - Containers ~47MB
    - Volumes ~4.34G
- ClickHouse:
  - System logs are small; TTL controls applied for both stacks.
- Langfuse MinIO:
  - Total `langfuse` bucket storage reduced to ~2.5G.

---

## Action items / decisions

### Decisions made
- Do not use Langfuse EE retention features (no license); instead manage storage operationally.
- Treat ClickHouse **system logs** as the primary risk for unbounded growth; enforce TTL.
- Purge Langfuse `events/otel` objects older than 14 days.
- Remove Snap Docker entirely (duplicate/unused) to reclaim `/var/snap/docker`.

### Action items in place
- ✅ ClickHouse log growth prevention:
  - `langfuse-clickhouse-1`: `/etc/clickhouse-server/config.d/langfuse-log-limits.xml`
  - `volkov-clickhouse-1`: same config
- ✅ Weekly MinIO purge:
  - `/etc/cron.weekly/langfuse-minio-events-purge`
  - `/etc/langfuse-minio.env` root-only credentials

### Recommended follow-ups
- Monitor disk usage:
  - Periodic checks: `df -h /`, `du -xhd1 /var | sort -h`, `docker system df -v`.
- Consider purging old MinIO `exports/` if it grows (similar `mc rm --older-than` strategy).
- Address `volkov-postgres-1` restarting (seen during diagnostics) separately if still occurring.
- Review `/home/volkov` usage (not yet fully analyzed); Docker Desktop `Docker.raw` virtual disk noted for investigation.

