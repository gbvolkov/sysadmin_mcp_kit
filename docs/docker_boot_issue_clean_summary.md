# Docker Boot Failure Investigation and Resolution

## Executive summary

The recurring issue was **not Docker itself** and **not the vendor `docker.service` / `docker.socket` units**.

The root cause was a **custom local systemd unit**:

`/etc/systemd/system/docker-ensure-iptables.service`

That unit declared:

```ini
[Unit]
Description=Ensure Docker iptables chains exist before Docker starts
Before=docker.service docker.socket
```

This created a **boot ordering cycle** involving `docker.socket`. On reboot, systemd reported:

- `sockets.target: Found ordering cycle on docker.socket/start`
- `sockets.target: Job docker.socket/start deleted to break ordering cycle starting with sockets.target/start`

As a result, after reboot:

- `docker.service` was inactive
- `docker.socket` was inactive
- `/run/docker.sock` was absent
- `docker compose up -d` failed with:
  - `Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?`

The sustainable fix was:

- restore **vendor Docker units**
- remove the custom boot helper from the active systemd path
- keep Docker using the packaged **socket-activated startup**

Final stable state:

- `docker.service` loaded from `/usr/lib/systemd/system/docker.service`
- `docker.socket` loaded from `/usr/lib/systemd/system/docker.socket`
- `dockerd` running with `-H fd://`
- reboot works
- Photoprism works
- Langfuse works

---

## 1. Original symptoms

The issue repeatedly appeared after reboot.

Typical failure:

```bash
sudo docker compose up -d
unable to get image 'photoprism/photoprism:latest': Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?
```

Initial observations showed:

- the Docker CLI context had previously been switched to `desktop-linux`
- Docker Desktop socket path was in use:
  - `unix:///home/volkov/.docker/desktop/docker.sock`
- switching back to the normal context fixed that first issue:
  - `docker context use default`

However, the deeper reboot problem remained.

---

## 2. Early Docker daemon findings

Checking systemd showed Docker was enabled but inactive after reboot:

```bash
systemctl status docker --no-pager -l
```

Output indicated:

- `docker.service` loaded and enabled
- `Active: inactive (dead)`

`containerd` itself was healthy and running, so the problem was not containerd startup.

The custom drop-in for Docker at one point was:

```ini
# /etc/systemd/system/docker.service.d/override.conf
[Unit]
Requires=
Requires=mnt-zoospacemedia.mount
After=
After=network-online.target mnt-zoospacemedia.mount nftables.service
Wants=
Wants=network-online.target

[Service]
ExecStart=
ExecStart=/usr/bin/dockerd -H unix:///var/run/docker.sock --containerd=/run/containerd/containerd.sock
```

That override was problematic because it changed Docker boot ordering and dependencies around:

- `mnt-zoospacemedia.mount`
- `nftables.service`
- socket activation behavior

---

## 3. Boot log evidence

The decisive boot log message was:

```text
sockets.target: Found ordering cycle on docker.socket/start
sockets.target: Job docker.socket/start deleted to break ordering cycle starting with sockets.target/start
```

This proved the reboot failure was caused by a **systemd ordering cycle**.

Other relevant boot observations:

- `mnt-zoospacemedia.automount` was active
- the CIFS mount eventually succeeded
- `containerd.service` started successfully
- the failure happened before Docker socket activation could complete

This meant the root cause was not simply “Docker is broken” or “mount is unavailable,” but specifically a **boot graph dependency cycle**.

---

## 4. Temporary non-vendor workaround that worked

A temporary workaround was created by bypassing socket activation entirely.

A local unit was placed at:

```text
/etc/systemd/system/docker.service
```

with `dockerd` started directly using:

```bash
/usr/bin/dockerd -H unix:///run/docker.sock --containerd=/run/containerd/containerd.sock
```

and `docker.socket` was masked.

This worked because it removed socket activation from the boot path.

Result in that mode:

- Docker came up on boot
- containers started
- updates no longer reintroduced socket activation immediately

Why it was only a workaround:

- it replaced the vendor unit with a local one
- future vendor unit improvements would not automatically apply
- it moved ownership of Docker’s startup policy from the package to the local system

The user explicitly requested to return to the **vendor socket-activated setup**, so this path was reversed.

---

## 5. Why the vendor setup originally seemed to fail

When vendor units were restored, reboot reproduced the original cycle exactly:

- `docker.service` inactive
- `docker.socket` inactive
- boot log again showed the ordering cycle on `docker.socket/start`

That proved the vendor units were not the true root cause.

At that point, effective dependency inspection showed Docker also referenced a helper unit:

```text
docker-ensure-iptables.service
```

Inspecting it revealed:

```ini
# /etc/systemd/system/docker-ensure-iptables.service
[Unit]
Description=Ensure Docker iptables chains exist before Docker starts
Before=docker.service docker.socket

[Service]
Type=oneshot
ExecStart=/bin/sh /usr/local/sbin/docker-ensure-iptables.sh

[Install]
WantedBy=multi-user.target
```

This was **not vendor Docker**. It was a custom local unit.

That unit was the real cause of the ordering cycle.

---

## 6. Sustainable fix actually applied

The sustainable fix was:

### Restore