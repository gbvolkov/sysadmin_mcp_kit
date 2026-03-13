# Process Kill Script Debugging and Fix

## Executive Summary

We created a shell script intended to kill specific development processes by matching exact service names in the command line and verifying their working directories. During testing, one `uvicorn` process was visible in `ps aux`, but the script appeared not to find or kill it. The root cause was not an initialization issue with the process. The actual bug was in the script logic: `set -euo pipefail` combined with a `pgrep ... | while read ...` pipeline caused the script to exit early whenever any `pgrep` returned no matches. The fix was to replace the pipeline with a safer `mapfile`-based approach that tolerates zero matches and continues processing the remaining patterns.

## Goal

Create a shell script to kill processes like these, using exact service names and optionally verifying their working directories:

- `bot_service.main:app`
- `openai_proxy.main:app`
- `app.main:app`
- `services.task_queue.worker`
- PM2 God Daemon associated with `/home/volkov/.pm2`

Expected working directories:

- `/home/volkov/bot_platform`
- `/home/volkov/webchat/backend`
- `/home/volkov/webchat` for PM2

## Original Process Examples

Processes the user wanted to target included:

```text
volkov    870337  0.0  0.1 123028 35544 pts/0    Sl   14:28   0:08 /home/volkov/bot_platform/.venv/bin/python /home/volkov/bot_platform/.venv/bin/uvicorn bot_service.main:app --reload

volkov    902758  0.0  0.1 123032 35584 pts/0    Sl   14:40   0:07 /home/volkov/bot_platform/.venv/bin/python /home/volkov/bot_platform/.venv/bin/uvicorn openai_proxy.main:app --reload --port 8084 --http openai_proxy.http_logging:LoggingH11Protocol

volkov    903955  0.0  0.0 120628 30080 pts/1    Sl   14:40   0:06 /home/volkov/webchat/backend/.venv/bin/python3 /home/volkov/webchat/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8009 --reload

volkov    901608  0.0  0.1 153264 56572 pts/0    Sl   14:39   0:02 python -m services.task_queue.worker

volkov    905423  0.0  0.2 1053900 65000 ?       Ssl  14:41   0:00 PM2 v6.0.13: God Daemon (/home/volkov/.pm2)
```

## Initial Script

The initial script matched service names in the command line and checked `/proc/$pid/cwd` before killing:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Map: pattern in command line -> expected working directory
declare -A SERVICES=(
  ["bot_service.main:app"]="/home/volkov/bot_platform"
  ["openai_proxy.main:app"]="/home/volkov/bot_platform"
  ["app.main:app"]="/home/volkov/webchat/backend"
  ["services.task_queue.worker"]="/home/volkov/bot_platform"
)

kill_matching() {
  local pattern="$1"
  local target_dir="$2"

  pgrep -f "$pattern" 2>/dev/null | while read -r pid; do
    [ -z "${pid:-}" ] && continue
    [ ! -d "/proc/$pid" ] && continue

    local cwd
    cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)

    if [[ "$cwd" == "$target_dir"* ]]; then
      echo "Killing PID $pid ($pattern), cwd=$cwd"
      kill "$pid" || true
    fi
  done
}

# Kill uvicorn and worker processes with matching cwd
for pattern in "${!SERVICES[@]}"; do
  kill_matching "$pattern" "${SERVICES[$pattern]}"
done

# Handle PM2 God Daemon started from webchat
PM2_DIR="/home/volkov/.pm2"
WEBCHAT_DIR="/home/volkov/webchat"

pgrep -f "PM2 v.*God Daemon" 2>/dev/null | while read -r pid; do
  [ -z "${pid:-}" ] && continue
  [ ! -d "/proc/$pid" ] && continue

  cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
  cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")

  if [[ "$cwd" == "$WEBCHAT_DIR"* && "$cmdline" == *"$PM2_DIR"* ]]; then
    echo "Killing PM2 God Daemon PID $pid, cwd=$cwd"
    kill "$pid" || true
  fi
done
```

## Observed Issue

The user reported that the script did not find a process that was visible in `ps aux | grep uvi`:

```text
volkov   1700603  1.8  0.1 122496 34516 pts/0    Sl   19:14   0:01 /home/volkov/bot_platform/.venv/bin/python /home/volkov/bot_platform/.venv/bin/uvicorn bot_service.main:app --reload
```

A question arose whether the process might be "not full initialized" even though it was visible in `ps` output.

## Verification Performed

The user verified that `pgrep` could see the target process and that its working directory matched expectations:

```bash
for pid in $(pgrep -f "bot_service.main:app"); do echo -n "$pid  "; readlink -f "/proc/$pid/cwd"; done
```

Output:

```text
1724233  /home/volkov/bot_platform
```

This confirmed:

- the command line contained `bot_service.main:app`
- the working directory was `/home/volkov/bot_platform`
- the process was not missing due to partial initialization

## Root Cause

The real problem was the combination of:

```bash
set -euo pipefail
```

with:

```bash
pgrep -f "$pattern" 2>/dev/null | while read -r pid; do
    ...
done
```

### Why this failed

- `pgrep` exits with status `1` when it finds no matches.
- With `set -e` and `pipefail`, that non-zero exit makes the whole pipeline fail.
- Because the script loops over several patterns, any earlier pattern with no matches can terminate the entire script.
- As a result, the script can exit before it ever reaches `bot_service.main:app`, even though that process exists and would match.

This means the issue was not:

- incorrect command line matching for `bot_service.main:app`
- incorrect working directory for the observed process
- a special "not fully initialized" state of `uvicorn`

## Corrected Function

The fix was to avoid the failing pipeline and safely collect PIDs first:

```bash
kill_matching() {
  local pattern="$1"
  local target_dir="$2"

  # Collect PIDs safely even if there are no matches
  mapfile -t pids < <(pgrep -f "$pattern" 2>/dev/null || true)

  for pid in "${pids[@]}"; do
    [ -z "${pid:-}" ] && continue
    [ ! -d "/proc/$pid" ] && continue

    local cwd
    cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)

    if [[ "$cwd" == "$target_dir"* ]]; then
      echo "Killing PID $pid ($pattern), cwd=$cwd"
      kill "$pid" || true
    fi
  done
}
```

## Technical Conclusion

The process shown in `ps aux` was valid and matched the intended filter. The failure was entirely in control flow:

- `ps aux | grep uvi` was not misleading
- `pgrep -f "bot_service.main:app"` was capable of finding the process
- `/proc/$pid/cwd` confirmed the expected working directory
- the script exited too early because an unrelated earlier `pgrep` returned no results

## Decisions and Action Items

### Decisions

- Keep matching by exact service names in the command line.
- Keep checking working directory as an additional safety filter.
- Do not attribute this behavior to incomplete process initialization.
- Replace pipeline-based PID iteration with `mapfile` to avoid premature exit.

### Action Items

- Update the `kill_matching` function to the corrected `mapfile` version.
- Re-run the kill script.
- Verify removal of the target process with:

```bash
pgrep -fa "bot_service.main:app" || echo "no match"
```

- Apply the same safe pattern to any similar PM2 or service-ki