# Troubleshooting

Concrete diagnostic flows for the most common failure modes when running Eburon Hub. Each entry has the symptom, the diagnostic commands you should run *before* opening an issue, and the fix that has worked for past reporters.

If your symptom isn't listed and the diagnostics don't narrow it down, file a bug at https://github.com/nesquena/eburon-webui/issues — include the **full output** of every command in the relevant section.

---

## "AIAgent not available -- check that beatrice-agent is on sys.path"

**Symptom.** WebUI starts, shows the chat interface, but every chat request fails immediately with this error in the response or the server log. As of v0.51.6 the error includes a diagnostic block with the running Python interpreter, the relevant `sys.path` entries, and the most-common fix; on older versions the message is bare.

**Why it happens.** The WebUI imports the agent class at chat time via `from run_agent import AIAgent`. That import only succeeds if the running Python's `sys.path` contains either the beatrice-agent checkout or a pip-installed copy of the agent. Three common failure modes:

1. **Agent installed but not on `sys.path`.** Most common. The agent is checked out somewhere (e.g. `~/Programmes/beatrice-agent`), the WebUI was launched with a Python that doesn't know about it, and there's no `pip install -e .` linking the two.
2. **Symlink with a typo or wrong target.** A symlink to the agent looks correct on `ls`, but `readlink` resolves to a path that doesn't exist or doesn't contain `agent/__init__.py`.
3. **`EBURON_WEBUI_AGENT_DIR` set to the wrong directory.** Override env var beats auto-discovery and points at a directory that has no agent code.

### Step 1 — confirm the agent location

```bash
# If you have ~/beatrice-agent (the default location):
ls -la ~/beatrice-agent
readlink ~/beatrice-agent          # if it's a symlink, where does it resolve?
ls ~/beatrice-agent/agent/__init__.py 2>&1
```

The third command must succeed (the file must exist). If it fails, your symlink is broken or pointing at a directory that's missing the agent module — fix that first.

### Step 2 — confirm the WebUI is using the right Python

```bash
cd ~/eburon-webui && ./start.sh 2>&1 | grep -iE 'agent|python|eburon_webui_python' | head -20
```

The startup banner prints which Python and agent dir it resolved. If the agent dir is empty or the Python is the wrong one, set the override:

```bash
export EBURON_WEBUI_AGENT_DIR=/absolute/path/to/beatrice-agent
export EBURON_WEBUI_PYTHON=/absolute/path/to/agent/venv/bin/python
./start.sh
```

### Step 3 — install the agent in editable mode

This is the most common fix and resolves the original issue #1695:

```bash
cd /path/to/beatrice-agent          # the directory holding pyproject.toml + the agent/ module
pip install -e .                  # use the same python that runs the WebUI
```

Then restart the WebUI:

```bash
cd ~/eburon-webui
./start.sh
```

### Step 4 — verify by importing manually

If steps 1-3 still don't work, check whether the WebUI's Python can import the agent at all:

```bash
$EBURON_WEBUI_PYTHON -c "from run_agent import AIAgent; print('ok')" 2>&1
```

(Replace `$EBURON_WEBUI_PYTHON` with the actual Python path from step 2 if the env var isn't set.) If this prints `ok`, the agent IS on `sys.path` for that Python — and the WebUI should work.

If this fails, `import run_agent` itself is broken — check that the agent's pyproject.toml lists `run_agent` as a top-level module or that the agent dir is on PYTHONPATH:

```bash
PYTHONPATH=/path/to/beatrice-agent $EBURON_WEBUI_PYTHON -c "from run_agent import AIAgent; print('ok')"
```

If adding PYTHONPATH fixes it, persist the path either via `pip install -e .` (preferred) or by setting `EBURON_WEBUI_AGENT_DIR` to that directory.

### When to file a bug

If after running steps 1-4 the import still fails *and* `pip install -e .` succeeded *and* `PYTHONPATH=... python -c "from run_agent import AIAgent"` succeeds — that's a real WebUI bug. File at https://github.com/nesquena/eburon-webui/issues with:

- The output of every command in steps 1-4
- The full diagnostic block printed by the WebUI's `ImportError` (v0.51.6+)
- Your OS, Python version, and how the agent was installed

---

## Other troubleshooting

This document grows over time. If a recurring failure mode isn't covered here yet, add it via PR. The format for each entry: **Symptom → Why → Diagnostic commands → Fix → When to file a bug**.

Related references:

- [`docs/supervisor.md`](supervisor.md) — process-supervisor setup (launchd, systemd, supervisord, runit/s6) including the bootstrap supervisor-foreground flag.
- [`docs/docker.md`](docker.md) — Docker compose setup, common failure modes, bind-mount migration.
- [`docs/wsl-autostart.md`](wsl-autostart.md) — WSL2 auto-start at login on Windows.
- [`docs/EXTENSIONS.md`](EXTENSIONS.md) — WebUI extension injection, security model, examples.
