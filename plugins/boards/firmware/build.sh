#!/usr/bin/env bash
# Build Tana firmware for the Waveshare ESP32-C6-Touch-AMOLED-2.16 and publish it
# to firmware/releases/, where tana-server offers it to devices over OTA.
#
#   ./build.sh                                   # bake in this machine's LAN address
#   OTA_URL=http://192.168.1.10:8003/tana/ota/ ./build.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IDF="${IDF_PATH:-$HOME/esp/esp-idf-v6.1}"
LAN_IP=$(ip route get 1.1.1.1 | grep -oP 'src \K\S+')
OTA_URL="${OTA_URL:-http://$LAN_IP:8003/tana/ota/}"
BOARD_CFG="$HERE/xiaozhi-esp32/main/boards/waveshare/esp32-c6-touch-amoled-2.16/config.json"

# Default OTA URL baked into the image (the Wi-Fi portal's "OTA URL" setting overrides it)
python3 - "$BOARD_CFG" "$OTA_URL" <<'EOF'
import json, sys
path, url = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
build = next(b for b in cfg["builds"] if b["name"] == "tana-c6-amoled-2.16")
opts = [o for o in build["sdkconfig_append"] if not o.startswith("CONFIG_OTA_URL=")]
build["sdkconfig_append"] = opts + [f'CONFIG_OTA_URL="{url}"']
open(path, "w").write(json.dumps(cfg, indent=4) + "\n")
EOF

# shellcheck disable=SC1091
source "$IDF/export.sh" >/dev/null
cd "$HERE/xiaozhi-esp32"
python scripts/build.py waveshare/esp32-c6-touch-amoled-2.16 \
    --name tana-c6-amoled-2.16 --language en-US --wake-word disabled

VER=$(grep -oP 'set\(PROJECT_VER "\K[^"]+' CMakeLists.txt)
mkdir -p "$HERE/releases"
cp build/xiaozhi.bin "$HERE/releases/tana-$VER.bin"
cp build/generated_assets.bin "$HERE/releases/assets-$VER.bin"
cp build/merged-binary.bin "$HERE/releases/full-$VER.bin"  # USB flashing: write at 0x0
echo "published Tana $VER (OTA URL $OTA_URL) to $HERE/releases"
