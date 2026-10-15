# tana-boards

**Tana's plugin for voice boards: small ESP32 boards with a microphone, a speaker and a screen, placed around the
house, woken with "Hey Tana".**

Boards stream their microphone to `tana-boards`, which does everything a small chip cannot: it hears the wake
phrase, transcribes speech (Whisper), sends what was said to Tana's assistant backend, and speaks the reply
(Kokoro) back on the board. It also runs presence and lighting for the rooms the boards are in, and serves the
boards' firmware updates over Wi-Fi.

## What the boards do

- **Talk to the same assistant as the web app**, one conversation per board. With the `rameness` backend the
  assistant can call the board's own tools (volume, brightness, theme) and the home's (lights, rooms).
- **Short answers first.** Long answers are spoken as a concise version, then "want more detail, or the full
  answer?"; say "more" or "full".
- **Announce finished work.** Work the assistant handed to agents is announced on the board that asked for it, with
  a short summary, as soon as it is done.
- **Lights and rooms.** Rooms' lights can follow occupancy (movement, voices, Wi-Fi sensing) after dark, or run on
  a daily timetable (`light_schedules`).
- **Keep working without the server.** If the boards stop hearing from `tana-boards` for three minutes, each runs
  its room's automatic lights by itself (occupancy or timetable), from a kit the server sends every minute. Without
  the assistant backend, the boards keep their other functions; they just say the AI is unavailable.

## Hardware

Developed on the Waveshare ESP32-C6-Touch-AMOLED-2.16. The voice protocol is that of
[xiaozhi-esp32](https://github.com/78/xiaozhi-esp32), whose firmware supports many ESP32 boards with a microphone,
a speaker and a screen; Tana's additions (ambient listening with a server-side wake phrase, Wi-Fi presence sensing,
the offline light fallback) are in the firmware's shared code, but only this board has been built and tested.
Lights: SP530E LED controllers on the local network (`tools/sp530e/`).

## Run

Installed with Tana (`uv pip install -e ../plugins/boards` from `server/`):

```bash
cp config.example.yaml config.yaml       # rooms, lights, location, the assistant backend
../../server/.venv/bin/tana-boards -c config.yaml
```

Port 8003 serves the boards (voice, firmware updates) and a local API for the web app (`/api/boards`,
`/api/rooms`, `/metrics`): keep it on your local network. `tana-boards.service` is a systemd unit to adapt.
Test without hardware:

```bash
../../server/.venv/bin/python scripts/fake_device.py --ambient "Hey Tana, what's two plus two?"
```

## Firmware

The firmware is xiaozhi-esp32 with Tana's changes, kept as one patch (`firmware/patches/tana.patch`, against
upstream commit `8ce50d27`):

```bash
git clone https://github.com/78/xiaozhi-esp32 firmware/xiaozhi-esp32
git -C firmware/xiaozhi-esp32 checkout -b tana 8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c
git -C firmware/xiaozhi-esp32 apply ../patches/tana.patch
firmware/build.sh                       # ESP-IDF 6.1 (IDF_PATH); publishes to firmware/releases/
```

`build.sh` publishes straight to `firmware/releases/`, which `tana-boards` offers to every board at its next
check-in. Builds for testing go to `firmware/staging/`, offered only to the boards listed in
`firmware.test_devices`.

**First install, over Wi-Fi:** power-cycle a board and press BOOT while it shows the startup screen; it opens a
setup hotspot. Join it with a phone (or browse to `http://192.168.4.1`), set **OTA URL** in **Advanced** to
`http://<this machine>:8003/tana/ota/`, and reconnect it to your Wi-Fi. It checks in, installs Tana's firmware and
its assets, and ends on a sleepy face with "Say "Hey Tana"". Reserve this machine's address in your router: the
boards store it.

**USB fallback:** `esptool.py --chip esp32c6 -p /dev/ttyACM0 write_flash 0x0 firmware/releases/full-<ver>.bin`
(hold BOOT while plugging in if the port does not appear).

### What the firmware changes

- **Ambient listening** (`CONFIG_TANA_AMBIENT_LISTENING`): the board keeps its voice channel open and streams the
  microphone; the server decides when Tana is addressed and switches the board between dormant and awake.
- **BOOT button:** while listening, wakes Tana without the wake phrase; while speaking, interrupts.
- **Presence sensing:** Wi-Fi channel measurements and sound levels for the occupancy detector.
- **Offline light fallback:** the kit, the heartbeat, and running occupancy or timetable lights when the server is
  silent.
- On-device wake word off, English UI, setup hotspot `Tana-XXXX`, the update URL pointing at your server.

## License

Apache License 2.0, like Tana; the firmware it builds is based on xiaozhi-esp32 (MIT License). See the
[disclaimer](../../NOTICE): the boards listen through microphones and switch devices in your home, and you are
responsible for how you use them.
