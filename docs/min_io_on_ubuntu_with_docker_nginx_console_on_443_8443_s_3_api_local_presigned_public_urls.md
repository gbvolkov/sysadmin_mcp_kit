# MinIO on Ubuntu with Docker + Nginx (Console via HTTPS, S3 API local-only, Public Presigned URLs)

## Executive summary
This document captures the final working setup for a MinIO deployment on an Ubuntu server using Docker Compose and Nginx.

- MinIO runs in Docker with persistent data/config under `/opt/minio/`.
- MinIO **S3 API** is bound to **localhost only** (`127.0.0.1:9000`) for server-local clients.
- MinIO **Console** is also bound to localhost (`127.0.0.1:9001`) and exposed publicly via **Nginx on HTTPS 443**.
- The upstream router/firewall forwards **external `:8443` → server `:443`**.
- A separate public hostname (`minio-s3.gbvolkoff.name`) is proxied to MinIO API (`127.0.0.1:9000`) to support **remote presigned downloads**.
- Application code uses **two S3 clients**: internal endpoint for upload, external endpoint for presigning.

---

## 1) Prerequisites
- Docker Engine and Docker Compose available:
  - Docker Engine: `29.2.1`
  - Docker Compose: `v2.34.0`

---

## 2) Directory layout
All MinIO files live under:
- `/opt/minio/`
  - `/opt/minio/data` (persistent object data)
  - `/opt/minio/config` (MinIO config)
  - `/opt/minio/.env` (root credentials)
  - `/opt/minio/docker-compose.yml`

---

## 3) Credentials file (`/opt/minio/.env`)
Credentials are stored in a root-owned file with strict permissions.

- Permissions: `600 root:root`
- Variables:
  - `MINIO_ROOT_USER=minioadmin`
  - `MINIO_ROOT_PASSWORD=<random>`

Security note:
- If a password is ever pasted into chat/logs, rotate it immediately.

---

## 4) Docker Compose: MinIO local-only (API + Console)
MinIO listens only on localhost ports. Public access is via Nginx.

`/opt/minio/docker-compose.yml`

```yaml
services:
  minio:
    image: minio/minio:latest
    container_name: minio
    restart: unless-stopped
    env_file:
      - .env
    environment:
      # Console is public via nginx on 443 (external is :8443 via router)
      MINIO_BROWSER_REDIRECT_URL: "https://minio.gbvolkoff.name:8443"

      # API is local-only (same host)
      MINIO_SERVER_URL: "http://127.0.0.1:9000"

    command: server /data --console-address ":9001"
    ports:
      - "127.0.0.1:9000:9000"
      - "127.0.0.1:9001:9001"
    volumes:
      - /opt/minio/data:/data
      - /opt/minio/config:/root/.minio
```

Start and verify:
- `sudo docker compose -f /opt/minio/docker-compose.yml up -d`
- `sudo docker ps --filter "name=^minio$"`
- `sudo ss -lntp | egrep ':(9000|9001)\b'`

Health check:
- `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9000/minio/health/ready` → `200`

---

## 5) Nginx: Console (HTTPS 443)
Console hostname:
- `minio.gbvolkoff.name`

Console URL from outside:
- `https://minio.gbvolkoff.name:8443` (because router maps external 8443 → server 443)

Nginx site:
- `/etc/nginx/sites-available/minio.gbvolkoff.name`
- enabled via symlink in `/etc/nginx/sites-enabled/`

Key requirements:
- Proxy to MinIO console: `127.0.0.1:9001`
- Websocket headers:
  - `Upgrade` and `Connection: upgrade`
- Use the correct certificate chain for the same hostname.

Example console vhost (core parts):

```nginx
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;

    server_name minio.gbvolkoff.name;

    ssl_certificate     /etc/letsencrypt/live/minio.gbvolkoff.name/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/minio.gbvolkoff.name/privkey.pem;

    client_max_body_size 0;
    proxy_buffering off;
    proxy_request_buffering off;

    location / {
        proxy_pass http://127.0.0.1:9001;

        proxy_http_version 1.1;
        proxy_set_header Host              $http_host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;
        proxy_set_header X-Forwarded-Port  $server_port;

        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        "upgrade";

        proxy_connect_timeout 300;
        proxy_read_timeout    300;
    }
}
```

Validation:
- Local SNI TLS check:
  - `openssl s_client -connect 127.0.0.1:443 -servername minio.gbvolkoff.name -brief </dev/null`
- Local vhost/proxy check bypassing DNS:
  - `curl -vkI --resolve minio.gbvolkoff.name:443:127.0.0.1 https://minio.gbvolkoff.name/`

---

## 6) Nginx: Public S3/API endpoint (for presigned URLs)
Problem addressed:
- Presigned URLs were being generated as `http://127.0.0.1:9000/...` and remote clients could not download.

Solution:
- Create a dedicated hostname for the S3/API endpoint and proxy it to MinIO API:
  - `minio-s3.gbvolkoff.name` → Nginx (443) → `127.0.0.1:9000`
- External access is on `:8443` because the router forwards external 8443 → server 443.

DNS:
- Create A record:
  - `minio-s3.gbvolkoff.name` → `95.165.168.65`

Certificate:
- Issue Let’s Encrypt cert for `minio-s3.gbvolkoff.name`.

Nginx site:
- `/etc/nginx/sites-available/minio-s3.gbvolkoff.name`

```nginx
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;

    server_name minio-s3.gbvolkoff.name;

    ssl_certificate     /etc/letsencrypt/live/minio-s3.gbvolkoff.name/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/minio-s3.gbvolkoff.name/privkey.pem;

    client_max_body_size 0;
    proxy_buffering off;
    proxy_request_buffering off;

    location / {
        proxy_pass http://127.0.0.1:9000;

        proxy_http_version 1.1;
        proxy_set_header Host              $http_host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 300;
        proxy_read_timeout    300;
    }
}
```

Enable and reload:
- `sudo ln -sf /etc/nginx/sites-available/minio-s3.gbvolkoff.name /etc/nginx/sites-enabled/minio-s3.gbvolkoff.name`
- `sudo nginx -t && sudo systemctl reload nginx`

Test locally (bypass DNS/routing):
- `curl -sk -o /dev/null -w "%{http_code}\n" --resolve minio-s3.gbvolkoff.name:443:127.0.0.1 https://minio-s3.gbvolkoff.name/minio/health/ready` → `200`

Test from remote:
- `curl -kI https://minio-s3.gbvolkoff.name:8443/` (expected `400 Bad Request` on `/` is normal for S3)

Note on server-side `mc alias` using the public hostname:
- `mc alias set ... https://minio-s3.gbvolkoff.name ...` may time out from inside the server if hairpin NAT is not supported. This does not indicate remote failure.

---

## 7) MinIO Client (`mc`) usage
Installation:
- Downloaded to `/usr/local/bin/mc` and made executable.

Aliases are per-user:
- Running `mc alias set ...` with `sudo` writes to `/root/.mc/config.json`.
- Running as normal user writes to `/home/<user>/.mc/config.json`.

Example admin check (root alias):
- `sudo mc admin info localminio` returned MinIO status OK.

---

## 8) Application code: upload locally, presign publicly
Goal:
- Upload via local-only endpoint (`127.0.0.1:9000`).
- Generate presigned URLs that remote clients can use via public endpoint (`minio-s3.gbvolkoff.name:8443`).

Use two boto3 clients:

```python
MINIO_INTERNAL_URL = "http://127.0.0.1:9000"          # server-local
MINIO_PUBLIC_URL   = "https://minio-s3.gbvolkoff.name:8443"  # remote clients

s3_internal = boto3.client(
    "s3",
    endpoint_url=MINIO_INTERNAL_URL,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    region_name="us-east-1",
)

s3_public = boto3.client(
    "s3",
    endpoint_url=MINIO_PUBLIC_URL,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    region_name="us-east-1",
)

def upload_and_get_link(file_path: str, prefix: str = "documents/", expires_seconds: int = 3600) -> str:
    p = Path(file_path)
    content_type, _ = mimetypes.guess_type(str(p))
    content_type = content_type or "application/octet-stream"

    key = f"{prefix}{uuid4().hex}_{p.name}"

    s3_internal.upload_file(
        Filename=str(p),
        Bucket=BUCKET,
        Key=key,
        ExtraArgs={"ContentType": content_type},
    )

    return s3_public.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=expires_seconds,
        HttpMethod="GET",
    )
```

Environment variables:
- Internal:
  - `MINIO_URL=http://127.0.0.1:9000`
- Public for presigning:
  - `MINIO_PUBLIC_URL=https://minio-s3.gbvolkoff.name:8443`

---

## 9) Notes and warnings encountered
### Nginx warnings
- “protocol options redefined for 443” warnings were due to inconsistent `listen 443` options across vhosts (some had `http2`, some not).
- OCSP stapling warnings (“no OCSP responder URL”) appeared for various certificates; they did not block operation.

### Router / port mapping
- External clients reach the server at `:8443`, which is forwarded to server `:443`.
- Console redirect must include `:8443` to avoid incorrect redirects.

---

## Decisions
- Keep MinIO API bound to localhost (`127.0.0.1:9000`) for server-local operations.
- Expose MinIO Console via Nginx on HTTPS 443 (reachable externally as `:8443` due to router forwarding).
- Add a dedicated S3 hostname (`minio-s3.gbvolkoff.name`) proxied to `127.0.0.1:9000` to support presigned URLs.
- Generate presigned URLs using the external S3 endpoint (`https://minio-s3.gbvolkoff.name:8443`).

---

## Action items
- [ ] Ensure the application uses a public endpoint for presigning (`MINIO_PUBLIC_URL=https://minio-s3.gbvolkoff.name:8443`).
- [ ] Confirm router forwards external `8443` → server `443` for both `minio.gbvolkoff.name` and `minio-s3.gbvolkoff.name` (SNI-based vhosts).
- [ ] Optional: align `listen 443` options across all Nginx vhosts to remove “protocol options redefined” warnings.
- [ ] Optional hardening: restrict Console access by IP allowlist or `auth_basic` at Nginx.

