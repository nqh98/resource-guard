# Resource Guard

**Keep AI coding agents and the builds they start from freezing your desktop.**

[Tiếng Việt](README.vi.md)

Coding agents such as Claude Code and Codex run type-checkers, test suites and builds on
their own. Each of those wants every core and a few gigabytes of RAM. Run two or three
agents next to a browser and the machine stops responding: the mouse stutters, typing lags,
and when memory runs out the system swaps to disk and freezes.

Resource Guard is a small local web dashboard that fixes this. Agents keep working; they
just give way to you.

- No dependencies: Python 3.10+ standard library only.
- Runs only on your machine, at `http://127.0.0.1:8765`.
- English and Vietnamese interface.

## What it does

| | Linux | macOS |
|---|---|---|
| Live CPU, memory, temperature, pressure and per-app usage | ✅ | ✅ (no temperature: it needs root) |
| **Lower agent priority**: agents, everything they start, and common build tools run at a low `nice` level | ✅ | ✅ |
| **CPU ceiling** for agents started from the terminal | ✅ hard limit (systemd scope `CPUQuota`) | ✅ QoS clamp (`taskpolicy`: utility, or background = efficiency cores) |
| **Soft memory limit** per agent: above it the agent is slowed down and reclaimed, never killed | ✅ (`MemoryHigh`) | ❌ no equivalent on macOS |
| Limits for **Claude Desktop** and the agents inside it | ✅ (GNOME app scope drop-in) | ❌ |
| **Low-memory rescue**: when free RAM drops below a threshold, stop the biggest *build* an agent started. Agents themselves are never stopped | ✅ | ✅ |
| Power mode switch | ✅ (`powerprofilesctl`) | shows Low Power Mode (changing it needs sudo) |
| zram status and setup commands | ✅ | not needed (macOS compresses memory) |
| Start at login | ✅ systemd user service | ✅ LaunchAgent |
| Stop or lower the priority of any of your processes; desktop components are protected | ✅ | ✅ |

## Install

```sh
pipx install git+https://github.com/nqh98/resource-guard
resource-guard --open
```

Or without installing:

```sh
git clone https://github.com/nqh98/resource-guard
cd resource-guard
python3 -m resource_guard --open
```

Then turn on **Start at login** in the dashboard so the guard keeps running.

## Using it

1. **Lower agent & build priority** is on by default. The process lists (agents, build
   tools) are editable in the dashboard.
2. **Limits for agents**: click *Install* to add an alias to `~/.bashrc` or `~/.zshrc`.
   From then on `claude` and `codex` started in a new terminal run inside the limits. The
   file is backed up to `~/.bashrc.resource-guard.bak` (or `.zshrc…`) the first time.
3. **Low-memory rescue** is on by default at 5% free RAM.

Options: `--port N` (or `RESOURCE_GUARD_PORT`), `--open`.

## What it changes on your system

Everything is reversible from the dashboard:

| Change | File | Undo |
|---|---|---|
| Shell alias | a marked block in `~/.bashrc` / `~/.zshrc` | *Remove* button, or delete the block |
| Claude Desktop limits (Linux) | `~/.config/systemd/user/app-com.anthropic.Claude-.scope.d/resource-guard.conf` | toggle off |
| Start at login | `~/.config/systemd/user/resource-guard.service` or `~/Library/LaunchAgents/io.github.resource-guard.plist` | toggle off |
| Settings | `~/.config/resource-guard/config.json` | delete it |

Lowering a process's priority can't be undone without root; the process returns to normal
priority when it restarts.

It never needs or asks for root. Processes owned by root (for example Docker containers)
can't be changed; give them limits with `docker run --cpus=4 --memory=8g`.

## Security

The server listens on `127.0.0.1` only and rejects requests whose `Host` isn't its own
address (DNS rebinding). Every action needs a per-run token sent in a custom header, which
other websites can't send without a CORS preflight the server never answers. It can only
affect processes and files of the user running it.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The platform tests run against the real OS; CI runs everything on Ubuntu and macOS.
Code layout: `linux.py` and `macos.py` are the OS backends, `monitor.py` samples and
enforces, `server.py` holds the config, actions and HTTP API, `static/index.html` is the
dashboard.

Claude is a trademark of Anthropic. This project isn't affiliated with Anthropic or OpenAI.

## License

MIT
