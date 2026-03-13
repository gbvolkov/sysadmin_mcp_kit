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

## Configuration

Copy [`config/server.example.toml`](C:/Projects/sysadmin_mcp_kit/config/server.example.toml) to [`config/server.toml`](C:/Projects/sysadmin_mcp_kit/config/server.toml) and set:

- OAuth issuer, introspection endpoint, resource server URL, client credentials, and required scope
- SSH config path used to resolve target aliases from the server host
- Allowed targets and remote path prefixes
- Sensitive and blocked command patterns
- Redaction patterns and paging defaults

The server reads `config/server.toml` by default. Override it with `SYSADMIN_MCP_CONFIG`.

## Run The MCP Server

```powershell
uv sync --all-groups --link-mode copy
.\.venv\Scripts\python.exe main.py
```

The server starts a FastMCP `streamable-http` endpoint using the host, port, and path from the TOML config. Keep `json_response = false`: confirmed commands use MCP elicitation, and the SDK only delivers those interleaved prompts correctly over SSE stream responses. For terminal-like behavior across repeated `run_command` calls, keep `stateless_http = false` so the client reuses the same MCP transport session. In that mode the server keeps one live remote shell per `(mcp session, target)`, which preserves `cwd`, exported variables, and activated virtualenvs between calls.

## Use The Direct CLI

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

## Use The MCP Client CLI

You can either pass an existing bearer token:

```powershell
sysadmin-mcp-client --url http://127.0.0.1:8000/mcp --token <access-token> list-targets
```

Or let the client obtain its own bearer token with `client_credentials`. This is now the default path when `--token` is omitted. For Keycloak, pass the realm issuer URL plus the service-account client credentials:

```powershell
sysadmin-mcp-client   --url http://127.0.0.1:8000/mcp   --issuer-url https://oauth.gbvolkoff.name:8443/realms/<realm>   --client-id sysadmin-mcp-cli   --client-secret <client-secret>
```

The client derives the token endpoint as:

```text
<issuer-url>/protocol/openid-connect/token
```

You can also pass the token endpoint explicitly instead of `--issuer-url`:

```powershell
sysadmin-mcp-client   --url http://127.0.0.1:8000/mcp   --token-endpoint https://oauth.gbvolkoff.name:8443/realms/<realm>/protocol/openid-connect/token   --client-id sysadmin-mcp-cli   --client-secret <client-secret>
```

Environment variable fallbacks are supported:

- `SYSADMIN_MCP_OAUTH_CLIENT_ID`
- `SYSADMIN_MCP_OAUTH_CLIENT_SECRET`
- `SYSADMIN_MCP_OAUTH_TOKEN_ENDPOINT`
- `SYSADMIN_MCP_OAUTH_ISSUER_URL`
- `SYSADMIN_MCP_OAUTH_SCOPE`

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

The native MCP client keeps one `ClientSession` open for the whole interactive run, so shell state persists across repeated `run-command` calls the same way it does for an agent using the server. Terminal mode renders `stdout` directly and `stderr` in red, supports `$info` for the last full JSON command result, and `$more stdout` or `$more stderr` for paged output returned by `read_command_output`. Separate CLI invocations create separate MCP sessions, so shell state does not carry across processes.

## MCP Tools

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