#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(sed -n '1p' "$ROOT/VERSION")
case "$VERSION" in
  ''|*[!A-Za-z0-9._-]*)
    echo "VERSION contains unsupported characters" >&2
    exit 1
    ;;
esac

for command in tar awk tail install; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "$command is required" >&2
    exit 1
  fi
done
if command -v sha256sum >/dev/null 2>&1; then
  sha256_file() {
    sha256sum "$1" | awk '{print $1}'
  }
elif command -v shasum >/dev/null 2>&1; then
  sha256_file() {
    shasum -a 256 "$1" | awk '{print $1}'
  }
else
  echo "sha256sum or shasum is required" >&2
  exit 1
fi

TEMPORARY=$(mktemp -d /tmp/threexui-package.XXXXXX)
trap 'rm -rf "$TEMPORARY"' EXIT HUP INT TERM
PACKAGE="$TEMPORARY/package"
mkdir -p "$PACKAGE/monitor/systemd" "$PACKAGE/monitor/launchd"

for file in \
  README.md \
  THIRD_PARTY_NOTICES.md \
  VERSION
do
  cp "$ROOT/$file" "$PACKAGE/$file"
done
for file in \
  README.md \
  anomaly_monitor.py \
  alertbridge_notify.py \
  deployment_config.py \
  feishu_notify.py \
  xui_discovery.py \
  install.sh \
  uninstall.sh \
  install_ip2region.sh \
  monitor.example.json \
  alertbridge-events.example.json \
  alertbridge-notifications.example.json \
  deployment.example.json \
  feishu.example.json
do
  cp "$ROOT/monitor/$file" "$PACKAGE/monitor/$file"
done
cp \
  "$ROOT/monitor/systemd/threexui-script-monitor.service" \
  "$PACKAGE/monitor/systemd/threexui-script-monitor.service"
cp \
  "$ROOT/monitor/systemd/threexui-script-monitor.timer.in" \
  "$PACKAGE/monitor/systemd/threexui-script-monitor.timer.in"
cp \
  "$ROOT/monitor/launchd/com.threexui.script-monitor.plist.in" \
  "$PACKAGE/monitor/launchd/com.threexui.script-monitor.plist.in"
chmod 0755 \
  "$PACKAGE/monitor/anomaly_monitor.py" \
  "$PACKAGE/monitor/alertbridge_notify.py" \
  "$PACKAGE/monitor/deployment_config.py" \
  "$PACKAGE/monitor/feishu_notify.py" \
  "$PACKAGE/monitor/xui_discovery.py" \
  "$PACKAGE/monitor/install.sh" \
  "$PACKAGE/monitor/uninstall.sh" \
  "$PACKAGE/monitor/install_ip2region.sh"

ARCHIVE="$TEMPORARY/payload.tar.gz"
COPYFILE_DISABLE=1 tar -C "$PACKAGE" -czf "$ARCHIVE" .
ARCHIVE_SHA256=$(sha256_file "$ARCHIVE")

HEADER_TEMPLATE="$TEMPORARY/header.template"
cat >"$HEADER_TEMPLATE" <<'EOF'
#!/bin/sh
set -eu

PRODUCT_VERSION="@VERSION@"
ARCHIVE_SHA256="@ARCHIVE_SHA256@"

for command in awk tail tar; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "$command is required" >&2
    exit 1
  fi
done
if command -v sha256sum >/dev/null 2>&1; then
  sha256_file() {
    sha256sum "$1" | awk '{print $1}'
  }
elif command -v shasum >/dev/null 2>&1; then
  sha256_file() {
    shasum -a 256 "$1" | awk '{print $1}'
  }
else
  echo "sha256sum or shasum is required" >&2
  exit 1
fi

TEMPORARY=$(mktemp -d /tmp/threexui-installer.XXXXXX)
trap 'rm -rf "$TEMPORARY"' EXIT HUP INT TERM
PAYLOAD_LINE=$(awk '/^__THREEXUI_PAYLOAD_BELOW__$/ {print NR + 1; exit}' "$0")
if [ -z "$PAYLOAD_LINE" ]; then
  echo "installer payload marker is missing" >&2
  exit 1
fi
ARCHIVE="$TEMPORARY/payload.tar.gz"
tail -n +"$PAYLOAD_LINE" "$0" >"$ARCHIVE"
ACTUAL_SHA256=$(sha256_file "$ARCHIVE")
if [ "$ACTUAL_SHA256" != "$ARCHIVE_SHA256" ]; then
  echo "installer payload checksum mismatch" >&2
  exit 1
fi
echo "ThreeXUIScriptMonitor $PRODUCT_VERSION payload verified" >&2
mkdir "$TEMPORARY/payload"
tar -C "$TEMPORARY/payload" -xzf "$ARCHIVE"
sh "$TEMPORARY/payload/monitor/install.sh" "$@"
exit $?
__THREEXUI_PAYLOAD_BELOW__
EOF

HEADER="$TEMPORARY/header"
sed \
  -e "s/@VERSION@/$VERSION/g" \
  -e "s/@ARCHIVE_SHA256@/$ARCHIVE_SHA256/g" \
  "$HEADER_TEMPLATE" >"$HEADER"

DIST="$ROOT/dist"
install -d -m 0755 "$DIST"
OUTPUT="$DIST/threexui-script-monitor-$VERSION-installer.sh"
cat "$HEADER" "$ARCHIVE" >"$OUTPUT"
chmod 0755 "$OUTPUT"
OUTPUT_SHA256=$(sha256_file "$OUTPUT")
printf '%s  %s\n' "$OUTPUT_SHA256" "$(basename "$OUTPUT")" >"$OUTPUT.sha256"
printf '%s\n' "$OUTPUT"
