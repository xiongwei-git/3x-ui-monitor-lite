#!/bin/sh
set -eu

PRODUCT="threexui-script-monitor"
PURGE=0
STAGE_ROOT=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --purge)
      PURGE=1
      shift
      ;;
    --stage-root)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "--stage-root requires a value" >&2
        exit 2
      fi
      STAGE_ROOT=${2%/}
      shift 2
      ;;
    -h|--help)
      echo "usage: $0 [--purge] [--stage-root DIR]"
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [ -n "$STAGE_ROOT" ]; then
  case "$STAGE_ROOT" in
    /*) ;;
    *)
      echo "stage root must be an absolute path" >&2
      exit 2
      ;;
  esac
elif [ "$(id -u)" -ne 0 ]; then
  echo "uninstaller must run as root" >&2
  exit 1
fi

dest() {
  printf '%s%s\n' "$STAGE_ROOT" "$1"
}

if [ -z "$STAGE_ROOT" ]; then
  if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now "$PRODUCT.timer" 2>/dev/null || true
  fi
  if command -v launchctl >/dev/null 2>&1; then
    launchctl bootout system/com.threexui.script-monitor 2>/dev/null || true
  fi
fi

rm -f "$(dest "/etc/systemd/system/$PRODUCT.timer")"
rm -f "$(dest "/etc/systemd/system/$PRODUCT.service")"
rm -f "$(dest "/Library/LaunchDaemons/com.threexui.script-monitor.plist")"
rm -f "$(dest "/usr/local/bin/$PRODUCT")"
rm -rf "$(dest "/usr/local/lib/$PRODUCT")"
rm -rf "$(dest "/usr/local/share/doc/$PRODUCT")"

if [ -z "$STAGE_ROOT" ] && command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
fi

if [ "$PURGE" -eq 1 ]; then
  rm -rf "$(dest "/var/lib/$PRODUCT")"
  rm -rf "$(dest "/etc/$PRODUCT")"
  rm -rf "$(dest "/usr/local/share/$PRODUCT")"
  if [ -z "$STAGE_ROOT" ]; then
    rm -f /var/log/threexui-script-monitor.log
    rm -f /var/log/threexui-script-monitor.error.log
  fi
fi

echo "ThreeXUIScriptMonitor removed"
if [ "$PURGE" -eq 0 ]; then
  echo "configuration and state were preserved"
fi
