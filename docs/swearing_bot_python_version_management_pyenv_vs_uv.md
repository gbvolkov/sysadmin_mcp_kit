# SwearingBot: Python 3.13 Setup and Version-Manager Consistency (pyenv vs uv)

## Executive summary
- The repository contained a **pyenv selector file** (`.python-version`) set to **`3.13`**.
- **pyenv could not satisfy that request** because it did not have an installed version named exactly `3.13` (it only had `3.12.6` and `3.12.9`).
- Meanwhile, the machine already had **Python 3.13.9** installed, but it came from **uv’s managed CPython distribution**, not pyenv.
- Result: tools consulting pyenv (directly or indirectly) produced errors, while `python` in the shell resolved to uv’s Python.
- Most consistent path for **Python 3.13.9** on this machine: **standardize on uv for this repo**, and remove or neutralize the pyenv trigger in the repo.

---

## Context
Directory listing (repo root): `~/SwearingBot`.

Attempted command:
- `uv venv .venv`

Observed error:
- `pyenv: version \`3.13\` is not installed (set by /home/volkov/SwearingBot/.python-version)`

Repository had:
- `.python-version` containing: `3.13`

---

## Evidence collected
### 1) pyenv state
Command:
- `cat .python-version && pyenv versions`

Output:
- `.python-version`: `3.13`
- pyenv installed versions:
  - `system`
  - `3.12.6`
  - `3.12.9`

pyenv error:
- `pyenv: version \`3.13\` is not installed (set by /home/volkov/SwearingBot/.python-version)`

### 2) Which Python is actually running
Commands:
- `which -a python`
- `python -V`
- `pyenv version`
- `pyenv which python`

Output:
- `python` resolves first to: `/home/volkov/.local/bin/python`
- `python -V`: `Python 3.13.9`
- `pyenv version`: errors due to missing `3.13`
- `pyenv which python`: `/home/volkov/.local/bin/python`

Interpretation:
- Even though pyenv shims exist (`~/.pyenv/shims/python`), the shell’s `python` is currently coming from **uv’s python** in `~/.local/bin`.

### 3) uv-managed Python 3.13.9 path
Commands:
- `ls -l /home/volkov/.local/bin/python`
- `/home/volkov/.local/bin/python -V`
- `/home/volkov/.local/bin/python -c "import sys; print(sys.executable); print(sys.version)"`

Output:
- `/home/volkov/.local/bin/python` is a symlink to:
  - `/home/volkov/.local/share/uv/python/cpython-3.13.9-linux-x86_64-gnu/bin/python3.13`
- Version confirmed:
  - `Python 3.13.9`
  - `3.13.9 (main, Oct 28 2025, 12:10:42) [Clang 20.1.4 ]`

---

## Root cause
- `.python-version` is **pyenv’s project-local selector**.
- It was set to `3.13`.
- pyenv expects that selector to match an installed pyenv version name. It did not.
- The machine’s Python 3.13.9 exists but is **managed by uv**, not installed through pyenv.
- Therefore:
  - pyenv emits the error any time it tries to resolve the repo’s requested version.
  - The shell still runs uv’s Python because of PATH ordering and the presence of `~/.local/bin/python`.

---

## What pyenv is (why it might be used)
pyenv is a Python version manager that:
- installs multiple CPython versions for a user
- selects versions globally (`pyenv global`) or per-project (`pyenv local` → writes `.python-version`)
- uses shims (`~/.pyenv/shims/python`) to route `python` to the selected version

pyenv is useful when:
- you want consistent per-project selection via `.python-version`
- you want to build/install versions via pyenv and reuse them across repos

---

## What uv is (as used here)
uv can:
- manage Python runtimes (download/keep its own CPython distributions)
- create virtual environments using a specified Python
- manage dependencies (lockfiles, sync, etc.)

In this system:
- Python 3.13.9 is provided via uv under:
  - `~/.local/share/uv/python/...`

---

## Consistency strategies (choose one)
### Option A — Standardize on uv (recommended for Python 3.13.9)
**Goal:** use uv-managed Python 3.13.9 and avoid pyenv conflicts.

Key points:
- Do not rely on `.python-version` to select Python for this repo.
- Create the venv explicitly using uv’s Python.

Example venv creation:
- Remove existing venv and create fresh:
  - `rm -rf .venv`
  - `uv venv .venv --python /home/volkov/.local/bin/python`
  - `source .venv/bin/activate`
  - `python -V`

To prevent pyenv interference:
- remove the repo-level pyenv selector file:
  - `rm -f .python-version`

Pros:
- Aligns with existing Python 3.13.9 installation
- Avoids pyenv’s missing-version error
- Single source of truth for Python in this repo

Cons:
- If other repos rely on pyenv `.python-version`, keep those separate and avoid mixing signals.

### Option B — Standardize on pyenv
**Goal:** use pyenv for Python selection and venv creation.

What’s available from pyenv in this environment:
- Installable 3.13 versions listed by pyenv:
  - `3.13.0`, `3.13.1`, `3.13.2` (and `t` variants)

Actions:
- `pyenv install 3.13.2`
- `pyenv local 3.13.2` (writes `.python-version`)

Pros:
- Classic per-repo `.python-version` workflow

Cons:
- Does not provide Python 3.13.9 in the observed pyenv install list
- Requires PATH and shim ordering to be correct

### Option C — Use both (advanced)
**Goal:** combine pyenv for version selection and uv for dependency management.

Requirements:
- `.python-version` must always match an installed pyenv version
- PATH must be configured so pyenv shims win consistently

Risk:
- Easiest approach to break; can produce confusing behavior.

---

## Recommended decision for this repo
Because the requirement is **Python 3.13.9**, and that runtime is already present via uv:
- **Use Option A (uv-only) for this repository.**

---

## Action items / decisions
### Decisions
- **Decision:** Standardize on **uv-managed Python 3.13.9** for `~/SwearingBot`.
- **Decision:** Avoid pyenv selection in this repo by removing `.python-version`.

### Actions
1. Remove repo-level pyenv selector:
   - `cd ~/SwearingBot && rm -f .python-version`
2. Recreate venv explicitly with uv Python 3.13.9:
   - `cd ~/SwearingBot`
   - `rm -rf .venv`
   - `uv venv .venv --python /home/volkov/.local/bin/python`
   - `source .venv/bin/activate`
   - `python -V` (expect `Python 3.13.9`)
3. (Optional verification) Confirm no `.python-version` remains:
   - `ls -la .python-version || echo "no .python-version"`

