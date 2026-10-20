<p align="center"><img src="docs/tana-logo.png" width="160" alt="Tana logo: a magenta iris made of code"></p>

<h1 align="center">Tana</h1>

<p align="center">
  <a href="https://github.com/Prog-Ramen/Tana/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Prog-Ramen/Tana" alt="License"></a>
  <a href="https://github.com/Prog-Ramen/Tana/releases"><img src="https://img.shields.io/github/v/release/Prog-Ramen/Tana" alt="Release"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB" alt="Python">
</p>
<p align="center">
  <a href="#trade-offs"><img src="https://img.shields.io/badge/advantage__tradeoffs-Adapter%20pattern%2C%20Plugin%20architecture%2C%20Graceful%20degradation%2C%20Data%20locality%2C%20Progressive%20disclosure-2ea44f" alt="advantage_tradeoffs: Adapter pattern, Plugin architecture, Graceful degradation, Data locality, Progressive disclosure"></a>
  <a href="#trade-offs"><img src="https://img.shields.io/badge/disadvantage__tradeoffs-External%20dependency%2C%20Thin%20clients%2C%20Narrow%20hardware%20support%2C%20Ambient%20authority-d97706" alt="disadvantage_tradeoffs: External dependency, Thin clients, Narrow hardware support, Ambient authority"></a>
</p>

**Tana is an all-in-one AI assistant you host yourself: a web app for your phone, laptop or car, voice in and
out, and small voice boards around the house, with the agent harness of your choice behind it.**

Tana does not have a brain of its own: you pick the backend. It ships with adapters for
[Rameness](https://github.com/Prog-Ramen/Rameness), Claude Code, Codex, pi and dsh, and switching is one setting.
Tana is the interface; the backend does the thinking, the coding and the research.

## What you get

- **Chats with Tana** in the web app: type or talk, read or listen. Replies stream in and can be read aloud;
  hands-free mode keeps listening after each reply.
- **A swappable backend.** `assistant.backend` picks what answers: `rameness` (a Rameness manager: answers simple
  questions itself, starts agents for real work, calls the client's tools), `claude-code`, `codex`, `pi` or `dsh`
  (each chat continues one session of that CLI). Older chats carry their history over when you switch.
- **Claude chats** (Anthropic's API) and **Claude Code** sessions: every session on the machine is listed, and
  you can continue one from the browser.
- **Every harness in one place.** Sessions of Codex, pi and dsh on the machine are listed read-only; Rameness's
  chats, agents and the decisions waiting on you have their own views, where you can answer, message or retire an
  agent.
- **Voice everywhere.** A phone can be the microphone for another screen (a car's browser, a laptop); a Siri
  Shortcut asks Tana with the phone locked.
- **Voice boards** (the [`boards` plugin](plugins/boards/README.md)): small ESP32 boards with a microphone, speaker
  and screen, woken with "Hey Tana", which talk to the same backend, switch lights and announce finished work.
- **Only what you set up is shown.** No API key, no Claude chats; no boards server, no boards screen; a harness
  with no sessions gets no tab.
- **Monitoring.** Prometheus metrics from the web app and the boards, and Grafana dashboards for both.

## Layout

| Path | What |
|---|---|
| `server/` | The web app (`tana`): `tana/app.py` (entry point), `webapp.py` (HTTP API), `static/` (UI), `assistant.py` (backends), `voice/` (speech recognition and synthesis, shared with plugins), `clients.py` (Rameness and tana-boards APIs) |
| `plugins/boards/` | `tana-boards`: the voice boards' server, its firmware build and tools |
| `dashboards/` | Grafana dashboards |

## Install

Requirements: Linux, Python 3.12, [uv](https://docs.astral.sh/uv/), and an HTTPS reverse proxy if you want to
reach the web app from outside your network.

```bash
git clone https://github.com/Prog-Ramen/Tana.git /opt/tana && cd /opt/tana/server
uv venv --python 3.12 && uv pip install -e .            # add `-e ../plugins/boards` for the voice boards
cp config.example.yaml config.yaml                        # pick the backend, the proxy's address, ...
```

Speech models: Whisper is downloaded on first start; put Kokoro's `kokoro-v1.0.onnx` and `voices-v1.0.bin`
([kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx)) in `~/.cache/tana/` or point `tts.model` and
`tts.voices` at them.

```bash
.venv/bin/tana -c config.yaml                             # web app on :8004, setup and metrics on :8005
```

Then, **from your own network**, open `http://<this machine>:8005/setup` to set a password and an authenticator
code (TOTP). Port 8005 must never be forwarded to the internet. Sign in on `:8004` (through your proxy).
`server/tana.service` is a systemd unit to adapt; `tana-admin reset-auth` and `tana-admin logout-all` manage logins.

### Backends

| `assistant.backend` | Needs | Notes |
|---|---|---|
| `rameness` | a Rameness manager (`rameness fleet up --home`) at `assistant.url` | Can call the client's tools (a board's volume, the lights) and announce work that finishes later. |
| `claude-code` | `claude` on the PATH, signed in | One Claude Code session per chat. |
| `codex` | `codex` on the PATH, signed in | One Codex thread per chat. |
| `pi` | `pi` with a provider configured | One pi session per chat. |
| `dsh` | `dsh` with a provider key | No sessions to resume: the adapter sends each chat's recent exchanges along. |

Adding another is a class in `server/tana/assistant.py`: implement `turn()` and register it.

## Security

- **Put the web app behind HTTPS.** It refuses connections from any address but `web.trusted_proxies` and trusts
  `X-Forwarded-For` only from them.
- **Login:** a password (argon2) plus an authenticator code, replay-protected; sessions are HttpOnly, Secure,
  SameSite=Strict cookies. Repeated failures lock logins out for 15 minutes. Mutating requests need a custom header,
  and the pages have a strict Content Security Policy.
- **Agents run commands as the user running Tana.** Claude Code from the browser runs without per-action
  approval, and Rameness agents act on your machine. Both need the login *and* a connection from
  `allowed_networks` (private networks by default). Every such action is logged.
- **Siri Shortcut keys** can only ask questions: no history, no settings, no agents. Revoke them in the app.

## Monitoring

The web app serves `GET /metrics` on its local port (`:8005/metrics`) and tana-boards on its own (`:8003/metrics`),
in the Prometheus format. Import [`dashboards/tana.json`](dashboards/tana.json) and
[`dashboards/tana-boards.json`](dashboards/tana-boards.json) in Grafana.

```yaml
scrape_configs:
  - job_name: tana
    static_configs: [{targets: ["127.0.0.1:8005"]}]
  - job_name: tana-boards
    static_configs: [{targets: ["127.0.0.1:8003"]}]
```

## Tests

```bash
cd server && .venv/bin/python -m unittest discover -s tests
```

## Trade-offs

The design choices behind the badges at the top, named after the patterns and trade-offs they are.

| | Trade-off | What it means here |
|---|---|---|
| Advantage | **Adapter pattern** | One assistant interface, with adapters for Rameness, Claude Code, Codex, pi and dsh; switching is one setting. |
| Advantage | **Plugin architecture** | The voice boards are a separate service (`tana-boards`) that Tana doesn't need in order to run. |
| Advantage | **Graceful degradation** | Without the server, boards keep running their room's automatic lights; without a backend, they keep their non-AI functions. |
| Advantage | **Data locality** | Speech recognition (Whisper) and synthesis (Kokoro) run on your machine. |
| Advantage | **Progressive disclosure** | Only what is set up is shown: no API key, no Claude chats; no boards server, no boards screen. |
| Disadvantage | **External dependency** | Tana has no brain of its own; every answer needs a configured backend. |
| Disadvantage | **Thin clients** | Boards stream audio to your server, which hears the wake phrase; voice doesn't work offline. |
| Disadvantage | **Narrow hardware support** | One board model has been built and tested. |
| Disadvantage | **Ambient authority** | Agents started from the web run with the permissions of the user running Tana. |

## License and disclaimer

Licensed under the [Apache License 2.0](LICENSE). Copyright 2026 Prog-Ramen. The board firmware is built from
[xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) (MIT License).

Tana is provided **as is, without warranty of any kind**. It controls devices, listens through microphones, can be
exposed to the internet, and starts AI agents that run commands on your machine. You are solely responsible for
how you install, secure and use it, for the people and places its microphones can hear, for every action taken
through it, and for complying with applicable laws and the terms of the services you connect. Tana is not a safety
or security system. To the maximum extent permitted by law, the authors, contributors and Prog-Ramen are not liable
for any damages arising from its use. See [NOTICE](NOTICE) and sections 7 and 8 of the license.
