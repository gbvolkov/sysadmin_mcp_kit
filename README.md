# sysadmin-mcp-kit

OAuth-protected MCP server for controlled SSH command execution and remote config inspection over `streamable-http`.

## Features

- OAuth token introspection with MCP `AuthSettings` and per-client result ownership
- Remote command execution over SSH with mandatory approval and extra typed confirmation for sensitive commands
- Remote file browsing and reading through SFTP with strict target/path allowlists
- Redaction for common config formats plus fallback text-pattern scrubbing for secrets and credentials
- Summary-first responses, pagination, filtering, and progressive disclosure for large outputs
- Separate `stdout` and `stderr` payloads for commands with follow-up paging via `read_command_output`
- Progress reporting during long-running SSH operations
- Direct local CLI for invoking the same utility layer without MCP
- Native MCP client CLI that talks to the running server over `streamable-http`
- Interactive CLI mode for choosing actions and entering parameters step by step
- Persistent remote shell sessions for MCP and interactive CLI usage, so working directory, exported variables, and activated virtualenvs survive across calls
- Remote password and passphrase prompts can be fulfilled through MCP elicitation without logging the secret or mixing it into command output

## Installation

### Process overview

The project has three moving parts, and the installation flow makes more sense if you keep them separate:

1. The MCP server reads a TOML file, validates it, exposes a FastMCP `streamable-http` endpoint, and uses OAuth token introspection on incoming requests.
2. The SSH layer runs on the same machine as the MCP server. That machine must already be able to resolve every configured `ssh_alias` through its own SSH config file.
3. Clients can talk to the system in two ways:
   - The direct CLI reads the same TOML file and talks to SSH directly, without HTTP or OAuth in the middle.
   - The MCP client CLI talks to the running server over HTTP and either accepts an explicit bearer token or fetches one through `client_credentials`.

In practice, installation is:

1. Install Python dependencies.
2. Copy and edit `config/server.toml`.
3. Make sure the server host can SSH to the configured targets.
4. Start the MCP server.
5. Smoke test with the direct CLI.
6. Smoke test with the MCP client CLI.

### Prerequisites

- Python `3.13` or newer
- [`uv`](https://github.com/astral-sh/uv) for dependency and virtualenv management
- An OAuth provider that supports token introspection for the MCP server
- SSH connectivity from the machine running this project to every configured target
- An SSH config file that contains the aliases referenced by `[[targets]].ssh_alias`

### 1. Install dependencies

From the repository root:

```powershell
uv sync --all-groups --link-mode copy
```

This creates `.venv` and installs runtime plus test dependencies from `pyproject.toml` and `uv.lock`.

### 2. Create the server config

Copy the example file:

```powershell
Copy-Item config\server.example.toml config\server.toml
```

By default the server loads `config/server.toml`. If you want the config elsewhere, either:

- set `SYSADMIN_MCP_CONFIG` before starting the server, or
- use `--config <path>` with the direct CLI

Relative config paths are searched from the current working directory, then its parent directories, then the package root. Absolute paths are used as-is.

### 3. Fill in `config/server.toml`

Start from this shape:

```toml
ssh_config_path = "~/.ssh/config"

[server]
host = "127.0.0.1"
port = 8000
streamable_http_path = "/mcp"
json_response = false
stateless_http = false
list_limit = 50
default_page_lines = 200
max_page_lines = 500
hard_page_char_limit = 16000
cache_ttl_seconds = 900
max_file_bytes = 1048576
progress_report_interval_seconds = 1.0
log_level = "INFO"

[oauth]
issuer_url = "https://auth.example.com"
introspection_endpoint = "https://auth.example.com/oauth/introspect"
resource_server_url = "https://mcp.example.com/mcp"
client_id = "sysadmin-mcp"
client_secret = "replace-me"
required_scope = "sysadmin:mcp"
allow_insecure_transport = false

[[targets]]
target_id = "cheetan"
ssh_alias = "cheetan"
allowed_paths = ["/etc", "/opt/app/config"]
default_timeout_seconds = 300
connect_timeout_seconds = 10

[command_policy]
sensitive_patterns = [
  '(?i)\\bsudo\\b',
  '(?i)\\bsystemctl\\s+(restart|stop)\\b',
  '(?i)\\bservice\\s+.+\\s+(restart|stop)\\b',
]
blocked_patterns = [
  '(?i)rm\\s+-rf\\s+/\\s*$',
]
confirmation_token_length = 8

[redaction]
sensitive_key_patterns = [
  'password',
  'passphrase',
  'secret',
  'token',
  'api[_-]?key',
  'access[_-]?key',
  'client[_-]?secret',
  'private[_-]?key',
  'credential',
]
text_patterns = [
  '(?is)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----',
  '(?i)(bearer\\s+)([A-Za-z0-9._~+/=-]+)',
  '(?im)(^\\s*(?:password|passphrase|secret|token|api[_-]?key|client[_-]?secret|private[_-]?key)\\s*[:=]\\s*)(.+)$',
  '([a-z]+://[^\\s:/@]+:)([^\\s/@]+)(@)',
]
```

### 4. Configure the SSH side

The configured `ssh_config_path` is read on the server host. Every `[[targets]]` entry must point to an SSH alias that already works from that machine.

Before starting the server, validate the SSH side manually:

```powershell
ssh cheetan
```

If the alias does not connect outside this project, it will not work inside this project either.

### 5. Configure the OAuth side

The MCP server does not mint tokens. It validates incoming bearer tokens by calling the configured introspection endpoint.

At minimum you need:

- an issuer URL for metadata and MCP auth settings
- an introspection endpoint the server can call
- a client ID and client secret the server can use for introspection
- a required scope that must be present on accepted tokens
- a public resource server URL that matches the URL clients will use to reach this MCP server

For local development on `localhost` or `127.0.0.1`, non-HTTPS OAuth URLs are accepted automatically. For any non-local host, `https` is required unless you explicitly set `allow_insecure_transport = true`.

### 6. Start the MCP server

You can run the repo entrypoint:

```powershell
.\.venv\Scripts\python.exe main.py
```

Or the package module:

```powershell
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit
```

The server starts a FastMCP `streamable-http` endpoint using the host, port, and path from `config/server.toml`.

Two settings matter operationally:

- Keep `json_response = false`. Confirmed commands use MCP elicitation, and those prompts need stream responses rather than plain JSON responses.
- Keep `stateless_http = false` if you want shell-like behavior across repeated `run_command` calls. With that setting the server keeps one live remote shell per `(MCP session, target)`, so `cd`, exported variables, and activated virtualenvs survive across calls.

### 7. Smoke test the direct CLI

The direct CLI bypasses HTTP and OAuth. It is the fastest way to prove that config parsing, SSH, redaction, and command policy work.

Interactive mode:

```powershell
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit.cli
```

One-shot checks:

```powershell
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit.cli list-targets
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit.cli browse-files cheetan /etc --limit 10
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit.cli read-file cheetan /etc/app/config.env --page-lines 50
```

If these commands fail, fix `config/server.toml` or SSH before testing the MCP client.

### 8. Smoke test the MCP client CLI

If you already have a bearer token:

```powershell
sysadmin-mcp-client --url http://127.0.0.1:8000/mcp --token <access-token> list-targets
```

If you want the client to fetch a token via `client_credentials`, pass the Keycloak realm issuer URL plus service-account credentials:

```powershell
sysadmin-mcp-client `
  --url http://127.0.0.1:8000/mcp `
  --issuer-url https://oauth.example.test/realms/test `
  --client-id sysadmin-mcp-cli `
  --client-secret <client-secret> `
  list-targets
```

When `--issuer-url` is used, the client derives the token endpoint as:

```text
<issuer-url>/protocol/openid-connect/token
```

If your provider uses a different token URL, pass `--token-endpoint` explicitly instead.

## Configuration reference

### Top-level settings

| Setting | Required | Description |
| --- | --- | --- |
| `ssh_config_path` | Yes | Path to the SSH config file on the server host. `~` is expanded. The aliases referenced by `[[targets]].ssh_alias` must exist here. |

### `[server]`

| Setting | Required | Description |
| --- | --- | --- |
| `host` | No | Interface the FastMCP server binds to. Default: `127.0.0.1`. |
| `port` | No | TCP port for the HTTP listener. Default: `8000`. |
| `streamable_http_path` | No | MCP endpoint path. Must start with `/`. Trailing `/` is stripped. Default: `/mcp`. |
| `json_response` | No | Must stay `false`. `true` is rejected because confirmed commands rely on MCP elicitation over stream responses. |
| `stateless_http` | No | When `false`, repeated calls in the same MCP session reuse one remote shell per target. When `true`, each command is isolated. Default: `false`. |
| `list_limit` | No | Default page size for directory listings. Default: `50`. |
| `default_page_lines` | No | Default page size for file reads and command output. Must be positive and not exceed `max_page_lines`. Default: `200`. |
| `max_page_lines` | No | Hard upper bound for requested page sizes. Must be positive. Default: `500`. |
| `hard_page_char_limit` | No | Hard cap on characters returned in a single page, even if line count is lower. Default: `16000`. |
| `cache_ttl_seconds` | No | How long paged results remain available in the in-memory result store and terminal session cache. Default: `900`. |
| `max_file_bytes` | No | Maximum number of bytes read from a remote file before truncation. Default: `1048576` (1 MiB). |
| `progress_report_interval_seconds` | No | Progress update cadence for long-running SSH work. Default: `1.0`. |
| `log_level` | No | FastMCP log level. Default: `INFO`. |

### `[oauth]`

| Setting | Required | Description |
| --- | --- | --- |
| `issuer_url` | Yes | OAuth issuer URL exposed to MCP clients and used for validation rules. Must be `https` outside localhost unless `allow_insecure_transport = true`. |
| `introspection_endpoint` | Yes | Endpoint the server calls to validate incoming bearer tokens. Must be reachable from the server host. |
| `resource_server_url` | Yes | Public URL of this MCP server as clients will reach it. Used in MCP auth settings. |
| `client_id` | Yes | OAuth client ID used by the server when calling the introspection endpoint. |
| `client_secret` | Yes | Secret paired with `client_id` for token introspection. Treat this as sensitive. |
| `required_scope` | Yes | Scope that must be present on accepted tokens. Requests missing this scope are rejected. |
| `allow_insecure_transport` | No | Allows non-HTTPS OAuth URLs for non-local hosts. Use only for local or temporary development. Default: `false`. |

### `[[targets]]`

Add one table per remote host or environment you want to expose.

| Setting | Required | Description |
| --- | --- | --- |
| `target_id` | Yes | Stable logical ID exposed to clients. This is what users pass to `list-targets`, `browse-files`, `read-file`, and `run-command`. Must be unique across all targets. |
| `ssh_alias` | Yes | SSH host alias resolved through `ssh_config_path`. |
| `allowed_paths` | Yes | Absolute POSIX path prefixes users may browse or read on that target. Must not be empty. Each entry must start with `/`. |
| `default_timeout_seconds` | No | Default command timeout when the caller does not pass one explicitly. Default: `300`. |
| `connect_timeout_seconds` | No | SSH connection timeout for that target. Default: `10`. |

### `[command_policy]`

| Setting | Required | Description |
| --- | --- | --- |
| `sensitive_patterns` | No | Regex patterns that trigger the second confirmation step with a typed token. Use these for commands that are legitimate but risky, such as `sudo` or service restarts. |
| `blocked_patterns` | No | Regex patterns that reject a command outright before execution. Use these for commands that should never be allowed. |
| `confirmation_token_length` | No | Length of the typed approval token shown for sensitive commands. Must be between `4` and `32`. Default: `8`. |

### `[redaction]`

| Setting | Required | Description |
| --- | --- | --- |
| `sensitive_key_patterns` | No | Regex patterns used to identify secret-like keys in structured text such as YAML, JSON, `.env`, or config files. |
| `text_patterns` | No | Fallback regex patterns applied to raw text output. Use this for bearer tokens, private keys, URL credentials, and similar secret shapes. |

## Environment variables

Only a small set of environment variables is used by the codebase. Server behavior is otherwise driven by the TOML file.

### Server and direct CLI

| Variable | Used by | Description | Default / fallback |
| --- | --- | --- | --- |
| `SYSADMIN_MCP_CONFIG` | `main.py`, `python -m sysadmin_mcp_kit`, direct CLI | Path to the server TOML file. Useful when the config is not stored at `config/server.toml` or when the process starts from another working directory. | Falls back to `config/server.toml`. The direct CLI `--config` flag overrides it. |

### MCP client CLI

| Variable | Used by | Description | Default / fallback |
| --- | --- | --- | --- |
| `SYSADMIN_MCP_OAUTH_CLIENT_ID` | `sysadmin-mcp-client` | OAuth client ID for `client_credentials` token retrieval when `--token` is not provided. | No default. Can also come from `.env`. |
| `SYSADMIN_MCP_OAUTH_CLIENT_SECRET` | `sysadmin-mcp-client` | OAuth client secret paired with `SYSADMIN_MCP_OAUTH_CLIENT_ID`. Treat it as sensitive. | No default. Can also come from `.env`. |
| `SYSADMIN_MCP_OAUTH_TOKEN_ENDPOINT` | `sysadmin-mcp-client` | Full OAuth token endpoint URL used to fetch an access token directly. Use this when your provider does not match the Keycloak issuer convention. | If unset and `SYSADMIN_MCP_OAUTH_ISSUER_URL` is set, the client derives `<issuer-url>/protocol/openid-connect/token`. |
| `SYSADMIN_MCP_OAUTH_ISSUER_URL` | `sysadmin-mcp-client` | Issuer URL, typically the Keycloak realm URL. Used only to derive the token endpoint when `SYSADMIN_MCP_OAUTH_TOKEN_ENDPOINT` is missing. | No default. Ignored if token endpoint is already set. |
| `SYSADMIN_MCP_OAUTH_SCOPE` | `sysadmin-mcp-client` | Scope sent with the `client_credentials` token request. | Defaults to `sysadmin:mcp`. |

### Environment resolution rules

The client resolves auth settings in this order:

1. Explicit CLI flags such as `--client-id`, `--client-secret`, `--token-endpoint`, `--issuer-url`, and `--scope`
2. Existing process environment variables
3. A `.env` file loaded by `--env-file`, or an auto-discovered `.env` in the current directory or any parent directory
4. Built-in defaults, where they exist

Important details:

- If `--token` is passed, the client does not use the OAuth client credential env vars.
- `.env` loading never overwrites an already-defined process environment variable.
- There is no supported bearer-token environment variable. Pass `--token` explicitly when you already have an access token.

Example `.env` for `sysadmin-mcp-client`:

```dotenv
SYSADMIN_MCP_OAUTH_ISSUER_URL=https://oauth.example.test/realms/test
SYSADMIN_MCP_OAUTH_CLIENT_ID=sysadmin-mcp-cli
SYSADMIN_MCP_OAUTH_CLIENT_SECRET=replace-me
SYSADMIN_MCP_OAUTH_SCOPE=sysadmin:mcp
```

Then:

```powershell
sysadmin-mcp-client --url http://127.0.0.1:8000/mcp list-targets
```

## Run the MCP server

```powershell
uv sync --all-groups --link-mode copy
.\.venv\Scripts\python.exe main.py
```

The server starts a FastMCP `streamable-http` endpoint using the host, port, and path from the TOML config. Keep `json_response = false`: confirmed commands use MCP elicitation, and the SDK only delivers those interleaved prompts correctly over SSE stream responses. For terminal-like behavior across repeated `run_command` calls, keep `stateless_http = false` so the client reuses the same MCP transport session. In that mode the server keeps one live remote shell per `(mcp session, target)`, which preserves `cwd`, exported variables, and activated virtualenvs between calls.

## Use the direct CLI

Interactive mode:

```powershell
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit.cli
```

Or explicitly:

```powershell
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit.cli interactive
```

The menu lets you choose `list-targets`, `browse-files`, `read-file`, or `terminal-session`, then prompts for the needed parameters. Inside a terminal session, commands run in the same live remote shell, so `cd /etc`, `export FOO=bar`, or `source venv/bin/activate` affect later commands the way you would expect from a shell. Builtin `cd` and `pwd` are still handled safely, while ordinary commands still go through confirmation and redaction. Command errors are shown inline and the interactive session stays open so you can correct the command and continue. Terminal output is rendered directly as `stdout` plus red `stderr`; use `$info` to print the last full JSON payload when you need structured details. Browse/read actions still print structured JSON interactively.

One-shot commands still work:

```powershell
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit.cli list-targets
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit.cli browse-files cheetan /etc/app --limit 10
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit.cli read-file cheetan /etc/app/config.env --page-lines 50
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit.cli run-command cheetan "sudo systemctl restart nginx" --yes --confirmation-token ABCD1234
```

Installed environments can also use the `sysadmin-mcp-cli` entrypoint. The CLI prints structured JSON to stdout and prompts or progress updates to stderr.

For `browse-files` and `read-file`, the CLI supports `--cursor` with the returned `next_cursor`, or direct offsets via `--offset` and `--start-line`. For `run-command`, the command is executed immediately and returns full redacted `stdout` and `stderr` in separate fields. Interactive terminal sessions now preserve full shell state within the session, not just the working directory. Sensitive commands still require the confirmation token.

## Use the MCP client CLI

You can either pass an existing bearer token:

```powershell
sysadmin-mcp-client --url http://127.0.0.1:8000/mcp --token <access-token> list-targets
```

Or let the client obtain its own bearer token with `client_credentials`. This is the default path when `--token` is omitted. For Keycloak, pass the realm issuer URL plus the service-account client credentials:

```powershell
sysadmin-mcp-client `
  --url http://127.0.0.1:8000/mcp `
  --issuer-url https://oauth.gbvolkoff.name:8443/realms/<realm> `
  --client-id sysadmin-mcp-cli `
  --client-secret <client-secret> `
  list-targets
```

You can also pass the token endpoint explicitly instead of `--issuer-url`:

```powershell
sysadmin-mcp-client `
  --url http://127.0.0.1:8000/mcp `
  --token-endpoint https://oauth.gbvolkoff.name:8443/realms/<realm>/protocol/openid-connect/token `
  --client-id sysadmin-mcp-cli `
  --client-secret <client-secret> `
  list-targets
```

The native MCP client also auto-loads a `.env` file from the current directory or its parents before resolving auth settings. Exported process env vars still win over `.env`, and you can point to a specific file with `--env-file path/to/.env`.

Interactive mode against the running MCP server:

```powershell
.\.venv\Scripts\python.exe -m sysadmin_mcp_kit.mcp_client_cli --url http://127.0.0.1:8000/mcp
```

Or with the installed entrypoint:

```powershell
sysadmin-mcp-client --url http://127.0.0.1:8000/mcp
```

One-shot MCP calls:

```powershell
sysadmin-mcp-client --url http://127.0.0.1:8000/mcp list-targets
sysadmin-mcp-client --url http://127.0.0.1:8000/mcp browse-files cheetan /etc --limit 10
sysadmin-mcp-client --url http://127.0.0.1:8000/mcp read-file cheetan /etc/app/config.env --page-lines 50
sysadmin-mcp-client --url http://127.0.0.1:8000/mcp run-command cheetan "sudo systemctl restart nginx" --yes --confirmation-token ABCD1234
```

The native MCP client keeps one `ClientSession` open for the whole interactive run, so shell state persists across repeated `run_command` calls the same way it does for an agent using the server. Terminal mode renders `stdout` directly and `stderr` in red, supports `$info` for the last full JSON command result, and `$more stdout` or `$more stderr` for paged output returned by `read_command_output`. Separate CLI invocations create separate MCP sessions, so shell state does not carry across processes.

## MCP tools

- `list_targets`
- `browse_files`
- `read_file`
- `run_command`
- `read_command_output`

`run_command` always performs elicitation before opening SSH. Commands matching the configured sensitive policy require a second confirmation step with a typed digest token. It also accepts an optional `working_dir` argument, which is resolved against the current session directory when one exists and then passed to the SSH executor. When the same stateful MCP session is reused, `run_command` uses one persistent remote shell per target, so `cd /etc`, `export FOO=bar`, and `source venv/bin/activate` survive into later calls without separate start or stop tools.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
```

The pytest config limits discovery to `tests/` and ignores transient `pytest-cache-files-*` directories that can appear in the workspace root.
