# Nginx Recovery and `agents.gbvolkoff.name` Vhost Cleanup

## Executive summary

The immediate Nginx failure was caused by a broken virtual host referencing a missing Let's Encrypt certificate for `palimpsest.gbvolkoff.name`. That site was disabled from `sites-enabled`, after which `nginx -t` succeeded and Nginx reloaded cleanly.

A secondary warning about a conflicting server name for `agents.gbvolkoff.name` was traced to duplicate configuration blocks in the default site and the dedicated `agents.gbvolkoff.name` vhost. The duplicate `agents` blocks were removed from `/etc/nginx/sites-available/default`, leaving the dedicated vhost as the canonical configuration.

Final validation confirmed that:
- Nginx syntax is valid.
- Nginx reloads successfully.
- `https://agents.gbvolkoff.name:8443` returns `HTTP/2 200`.
- External access on plain `:443` is blocked upstream by a front firewall/NAT, not by this Ubuntu server.

## Initial problem

`sudo nginx -t` failed with a hard error:

- `cannot load certificate "/etc/letsencrypt/live/palimpsest.gbvolkoff.name/fullchain.pem": BIO_new_file() failed`

Additional warnings were also present:
- `protocol options redefined` on `:443`
- `ssl_stapling ignored, no OCSP responder URL in the certificate ...`
- `conflicting server name "agents.gbvolkoff.name" on 0.0.0.0:443, ignored`

The hard certificate error was the blocking issue preventing successful config validation.

## Root cause 1: broken `palimpsest.gbvolkoff.name` site

### Findings

A grep showed that the `palimpsest` vhost existed in both `sites-enabled` and `sites-available` and referenced these files:

- `/etc/letsencrypt/live/palimpsest.gbvolkoff.name/fullchain.pem`
- `/etc/letsencrypt/live/palimpsest.gbvolkoff.name/privkey.pem`

However, the certificate directory did not exist:

- `ls: cannot access '/etc/letsencrypt/live/palimpsest.gbvolkoff.name': No such file or directory`

The file `/etc/nginx/sites-available/palimpsest.gbvolkoff.name` was then inspected. Important detail:

- The file name was `palimpsest.gbvolkoff.name`
- But inside it, the configured `server_name` was `langfuse.gbvolkoff.name`

This strongly suggested a stale or miscopied reverse proxy config. It was also a PhotoPrism-style config template pointing to:

- `proxy_pass http://localhost:5002;`

### Decision

Temporarily disable the broken vhost so Nginx can start, instead of trying to recreate a missing certificate during recovery.

### Action taken

Removed the enabled symlink/file:

```bash
sudo rm /etc/nginx/sites-enabled/palimpsest.gbvolkoff.name
```

### Result

After removal, `sudo nginx -t` passed syntax validation. The hard failure was resolved.

## Root cause 2: duplicate `agents.gbvolkoff.name` definitions

### Findings

A grep for `agents.gbvolkoff.name` showed it was defined in two places:

- Dedicated site: `/etc/nginx/sites-enabled/agents.gbvolkoff.name`
- Default site: `/etc/nginx/sites-enabled/default`

The relevant duplicate content in `default` included:

- An HTTPS block with:
  - `server_name agents.gbvolkoff.name;`
  - `listen [::]:443 ssl ipv6only=on;`
  - `listen 443 ssl;`
  - `ssl_certificate /etc/letsencrypt/live/agents.gbvolkoff.name/fullchain.pem;`
  - `ssl_certificate_key /etc/letsencrypt/live/agents.gbvolkoff.name/privkey.pem;`
- A separate HTTP block that redirected or handled `agents.gbvolkoff.name` on port 80.

The dedicated `agents.gbvolkoff.name` vhost was also inspected and already contained the intended reverse proxy configuration:

- `listen 443 ssl http2;`
- `listen [::]:443 ssl http2;`
- `server_name agents.gbvolkoff.name;`
- `proxy_pass http://localhost:5173;` for `/`
- `proxy_pass http://localhost:5173;` for `/webchat`
- `proxy_pass http://localhost:8009;` for `/api/`
- `proxy_pass http://localhost:3333;` for `/monitor/`
- `proxy_pass http://localhost:5002;` for `/anonimizer/`

### Decision

Keep the dedicated `agents.gbvolkoff.name` vhost and remove the duplicate `agents` definitions from the default site.

### Action taken

Edited `/etc/nginx/sites-available/default`:

- Changed the first server block from:

```nginx
server_name agents.gbvolkoff.name; # managed by Certbot
```

to:

```nginx
server_name _;
```

- Deleted or commented out the bottom block:

```nginx
server {
    if ($host = agents.gbvolkoff.name) {
        return 301 https://$host$request_uri;
    } # managed by Certbot

    listen 80 ;
    listen [::]:80 ;
    server_name agents.gbvolkoff.name;
    return 404; # managed by Certbot
}
```

### Result

The `conflicting server name "agents.gbvolkoff.name"` warning disappeared on the next config test.

## Validation steps and outcomes

### 1) Nginx syntax test after disabling broken `palimpsest` site

Result:
- `nginx: the configuration file /etc/nginx/nginx.conf syntax is ok`
- `nginx: configuration file /etc/nginx/nginx.conf test is successful`

Warnings remained, but there were no fatal errors.

### 2) Nginx reload after recovery

Command run:

```bash
sudo systemctl reload nginx
```

Result:
- Reload completed successfully with no output.

### 3) Nginx syntax test after removing duplicate `agents` definitions

Result:
- Syntax remained valid.
- Only warnings left were:
  - `protocol options redefined`
  - `ssl_stapling ignored, no OCSP responder URL in the certificate ...`

### 4) Runtime connectivity test from the server

Attempted:

```bash
curl -Ik https://agents.gbvolkoff.name
```

Outcome:
- Hung and was interrupted.
- User noted there is a firewall before the server, explaining this behavior.

Confirmed working test:

```bash
curl -Ik https://agents.gbvolkoff.name:8443
```

Returned:

```http
HTTP/2 200
server: nginx/1.24.0 (Ubuntu)
date: Tue, 02 Dec 2025 17:06:34 GMT
content-type: text/html
vary: Origin
cache-control: no-cache
etag: W/"274-fbfdugzz4PT2q0ZvEAklOk3pCtc"
strict-transport-security: max-age=172800; includeSubdomains
```

### 5) External verification

User confirmed that external access works.

## Remaining warnings

These warnings remained after cleanup:

- `protocol options redefined for [::]:443 in /etc/nginx/sites-enabled/default:143`
- `protocol options redefined for 0.0.0.0:443 in /etc/nginx/sites-enabled/gbvolkoff.name:7`
- `protocol options redefined for [::]:443 in /etc/nginx/sites-enabled/gbvolkoff.name:8`
- `ssl_stapling ignored, no OCSP responder URL in the certificate ...`

### Interpretation

- The `ssl_stapling` warnings are non-fatal and were treated as harmless for this recovery.
- The `protocol options redefined` warnings are also non-fatal. They indicate overlapping `listen ... ssl/http2` declarations on the same socket across server blocks and can be cleaned up later, but they do not prevent operation.

## Notable technical details preserved

- The missing certificate path was:
  - `/etc/letsencrypt/live/palimpsest.gbvolkoff.name/fullchain.pem`
- The matching key path was:
  - `/etc/letsencrypt/live/palimpsest.gbvolkoff.name/privkey.pem`
- The `palimpsest` site file contained `server_name langfuse.gbvolkoff.name;`, which did not match the filename.
- The dedicated `agents` site proxies to these local services:
  - `localhost:5173` for `/` and `/webchat`
  - `localhost:8009` for `/api/`
  - `localhost:3333` for `/monitor/`
  - `localhost:5002` for `/anonimizer/`
- Successful server-side HTTPS verification was on port `8443`, not plain `443`.
- Plain `443` behavior is governed by an upstream firewall/NAT in front of the server.

## Final decisions

- Disable the broken `palimpsest.gbvolkoff.name` vhost rather than attempting certificate recovery during incident response.
- Keep the dedicated `agents.gbvolkoff.name` vhost as the source of truth.
- Remove duplicate `agents` declarations from the default site.
- Accept the remaining `ssl_stapling` warnings as non-blocking.
- Treat plain `443` reachability as an upstream firewall/NAT concern, not an Ubuntu/Nginx problem on this host.

## Action items

### Completed

- Disabled `/etc/nginx/sites-enabled/palimpsest.gbvolkoff.name`
- Restored successful `nginx -t`
- Reloaded Nginx successfully
- Removed duplicate `agents.gbvolkoff.name` definitions from `/etc/nginx/sites-available/default`
- Verified `https://agents.gbvolkoff.name:8443` returns `HTTP/2 200`
- Confirmed external access works

### Optional follow-up work

- Clean up `protocol options redefined` warnings by normalizing `listen` directives across the active vhosts.
- Remove or adjust `ssl_stapling` directives for certificates that do not expose an OCSP responder URL.
- Recreate or permanently retire the `palimpsest.gbvolkoff.name` site, depending on whether that hostname is still needed.
- If plain external `443` is desired, adjust the upstream firewall/NAT to forward traffic appropriately.

## Current status

Nginx is operational, configuration tests pass, the `agents` service is reachable on the configured published port, and the