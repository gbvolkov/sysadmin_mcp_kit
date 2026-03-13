# Docker daemon dependency failure troubleshooting log

## Executive summary
A `docker compose pull` failed because the Docker daemon was not reachable at `/var/run/docker.sock`. Docker is installed and enabled, but the `docker.service` is **inactive (dead)** and fails to start due to a **systemd dependency failure**. The failing dependency is a mount unit: **`mnt-zoospacemedia.mount`**.

## Context and symptoms
- Command run:
  - `sudo docker compose pull`
- Error:
  - `unable to get image 'photoprism/photoprism:latest': Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?`

## Service status checks
### Docker service status
- Command:
  - `sudo systemctl status docker --no-pager`
- Output (key lines):
  - `docker.service - Docker Application Container Engine`
  - `Loaded: loaded (/usr/lib/systemd/system/docker.service; enabled; preset: enabled)`
  - `Drop-In: /etc/systemd/system/docker.service.d/override.conf`
  - `Active: inactive (dead)`
  - `TriggeredBy: docker.socket`

### Attempt to start Docker
- Command:
  - `sudo systemctl start docker`
- Result:
  - `A dependency job for docker.service failed. See 'journalctl -xe' for details.`

## Logs collected
### Docker service journal
- Command:
  - `sudo journalctl -u docker.service -n 200 --no-pager`
- Observations (key details):
  - Numerous repeating log lines earlier:
    - `level=info msg="ignoring event" ... topic=/tasks/delete type="*events.TaskDelete"`
  - Later boot entries show systemd dependency failures:
    - `Dependency failed for docker.service - Docker Application Container Engine.`
    - `docker.service: Job docker.service/start failed with result 'dependency'.`

## Root cause identified (systemd dependency)
### Failed dependency listing
- Command:
  - `sudo systemctl list-dependencies docker.service --failed`
- Output:
  - `docker.service`
  - `  mnt-zoospacemedia.mount`

## Next diagnostic step
### Check failing mount unit
- Command requested (pending output):
  - `sudo systemctl status mnt-zoospacemedia.mount --no-pager -l`

## Action items / decisions
### Action items
- Run and capture mount unit status:
  - `sudo systemctl status mnt-zoospacemedia.mount --no-pager -l`
- Based on the mount error details, remediate `mnt-zoospacemedia.mount` (e.g., fix `/etc/fstab`, network storage availability, permissions, device path), then retry:
  - `sudo systemctl start docker`
  - `sudo systemctl status docker --no-pager`
  - `sudo docker compose pull`

### Decisions (current)
- Treat Docker startup failure as a systemd dependency issue, not a Docker daemon configuration issue.
- Investigate and fix `mnt-zoospacemedia.mount` before further Docker troubleshooting.
