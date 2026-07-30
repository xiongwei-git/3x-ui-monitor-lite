#!/bin/sh
set -eu

UPSTREAM_COMMIT="cd40e3a1d532d645697999d646cf0e10481cef33"
BASE_URL="https://raw.githubusercontent.com/lionsoul2014/ip2region/$UPSTREAM_COMMIT"
MODULE_DIR="/usr/local/lib/threexui-script-monitor/ip2region"
DATA_DIR="/usr/local/share/threexui-script-monitor"
LICENSE_DIR="/usr/local/share/doc/threexui-script-monitor"
PYTHON=${PYTHON:-}

if [ "$#" -ne 0 ]; then
  echo "usage: $0" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "ip2region installer must run as root" >&2
  exit 1
fi
for command in curl install; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "$command is required" >&2
    exit 1
  fi
done
if command -v sha256sum >/dev/null 2>&1; then
  CHECKSUM_COMMAND="sha256sum --check --strict"
elif command -v shasum >/dev/null 2>&1; then
  CHECKSUM_COMMAND="shasum -a 256 --check"
else
  echo "sha256sum or shasum is required" >&2
  exit 1
fi
if [ -z "$PYTHON" ]; then
  PYTHON=$(command -v python3 || true)
fi
if [ ! -x "$PYTHON" ]; then
  echo "Python 3 is required" >&2
  exit 1
fi

temporary=$(mktemp -d /tmp/threexui-ip2region.XXXXXX)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
mkdir "$temporary/ip2region"

download() {
  source_path=$1
  destination=$2
  curl \
    --proto '=https' \
    --tlsv1.2 \
    --fail \
    --silent \
    --show-error \
    --location \
    --retry 3 \
    --connect-timeout 10 \
    --max-time 180 \
    "$BASE_URL/$source_path" \
    --output "$destination"
}

download "binding/python/ip2region/__init__.py" "$temporary/ip2region/__init__.py"
download "binding/python/ip2region/searcher.py" "$temporary/ip2region/searcher.py"
download "binding/python/ip2region/util.py" "$temporary/ip2region/util.py"
download "binding/python/LICENSE" "$temporary/LICENSE"
download "data/ip2region_v4.xdb" "$temporary/ip2region_v4.xdb"

(
  cd "$temporary"
  printf '%s  %s\n' \
    "1cb7dfe6b8b19feff29bf9c9a3107b34ee856d19817a5ac1e464070ddb330b5d" "ip2region/__init__.py" \
    "621c93ae7a3bc858cef994b2ed99551550c3574fb0e6d537affd1b4458d9a0d8" "ip2region/searcher.py" \
    "eda5592a77007bf3a2cf4255a7eb7372cf57a7db844e5238231843bf3bfeab9e" "ip2region/util.py" \
    "4416735adc725546aea1d352879eac20932e76bc3050a4c81ec9fa6a3a3a125c" "LICENSE" \
    "6307a9696f5711f84bcb8b25f07894de68a64a0ed4a1cc7e990562dd3084f210" "ip2region_v4.xdb" \
    | $CHECKSUM_COMMAND -
)

PYTHONPATH="$temporary" "$PYTHON" - "$temporary/ip2region_v4.xdb" <<'PY'
import sys
import ip2region.searcher as xdb
import ip2region.util as util

database_path = sys.argv[1]
util.verify_from_file(database_path)
searcher = xdb.new_with_file_only(util.IPv4, database_path)
try:
    if not searcher.search("1.1.1.1"):
        raise SystemExit("ip2region validation query returned no data")
finally:
    searcher.close()
PY

ROOT_GROUP=$(id -gn root)
install -d -o root -g "$ROOT_GROUP" -m 0755 "$MODULE_DIR" "$DATA_DIR" "$LICENSE_DIR"
install -o root -g "$ROOT_GROUP" -m 0644 "$temporary/ip2region/__init__.py" "$MODULE_DIR/__init__.py"
install -o root -g "$ROOT_GROUP" -m 0644 "$temporary/ip2region/searcher.py" "$MODULE_DIR/searcher.py"
install -o root -g "$ROOT_GROUP" -m 0644 "$temporary/ip2region/util.py" "$MODULE_DIR/util.py"
install -o root -g "$ROOT_GROUP" -m 0644 "$temporary/LICENSE" "$LICENSE_DIR/ip2region-LICENSE"
install -o root -g "$ROOT_GROUP" -m 0644 "$temporary/ip2region_v4.xdb" "$DATA_DIR/ip2region_v4.xdb.new"
mv -f "$DATA_DIR/ip2region_v4.xdb.new" "$DATA_DIR/ip2region_v4.xdb"

manifest="$DATA_DIR/ip2region-version.txt.new"
{
  printf 'upstream=https://github.com/lionsoul2014/ip2region\n'
  printf 'commit=%s\n' "$UPSTREAM_COMMIT"
  printf 'database=ip2region_v4.xdb\n'
  printf 'database_sha256=6307a9696f5711f84bcb8b25f07894de68a64a0ed4a1cc7e990562dd3084f210\n'
  printf 'database_created_at_utc=2026-07-09T15:52:51Z\n'
} >"$manifest"
chown root:"$ROOT_GROUP" "$manifest"
chmod 0644 "$manifest"
mv -f "$manifest" "$DATA_DIR/ip2region-version.txt"

"$PYTHON" -m py_compile "$MODULE_DIR/__init__.py" "$MODULE_DIR/searcher.py" "$MODULE_DIR/util.py"
echo "ip2region installed commit=$UPSTREAM_COMMIT database_created_at_utc=2026-07-09T15:52:51Z"
