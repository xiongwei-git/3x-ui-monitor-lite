#!/bin/sh
set -eu

PRODUCT="threexui-script-monitor"
ENABLE=0
INTERVAL=300
INTERVAL_SET=0
INSTANCE_NAME="3x-ui Monitor"
INSTANCE_NAME_SET=0
INSTANCE_LOCATION=""
INSTANCE_LOCATION_SET=0
XUI_EXECUTABLE=""
DATABASE=""
NOTIFICATION_CONFIG=""
DEPLOYMENT_CONFIG=""
DEPLOYMENT_PLAN=""
DEPLOYMENT_PLAN_LOADED=0
CONFIG_MODE=0
CLI_CONFIGURATION_SET=0
NOTIFICATION_PROVIDER="alertbridge"
NOTIFIER_FILENAME="alertbridge_notify.py"
NOTIFICATION_CONFIG_FILENAME="alertbridge.json"
STAGE_ROOT=""
PLATFORM=""

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --config FILE               Load all settings from one deployment JSON file
  --enable                    Enable and start the scheduled monitor
  --interval SECONDS          Run interval from 60 to 86400 (default: 300)
  --instance-name NAME        Name shown in notifications
  --instance-location TEXT    Optional location shown in templates
  --notification-config FILE  AlertBridge or Feishu webhook JSON config
  --xui-executable FILE       Override automatic 3x-ui executable discovery
  --database FILE             Override automatic x-ui.db discovery
  --stage-root DIR            Build a filesystem staging tree without activation
  --platform linux|macos      Override platform (mainly for staging validation)
  -h, --help                  Show this help
EOF
}

require_value() {
  if [ "$#" -lt 2 ] || [ -z "$2" ]; then
    echo "$1 requires a value" >&2
    exit 2
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --config)
      require_value "$@"
      DEPLOYMENT_CONFIG=$2
      CONFIG_MODE=1
      shift 2
      ;;
    --enable)
      ENABLE=1
      CLI_CONFIGURATION_SET=1
      shift
      ;;
    --interval)
      require_value "$@"
      INTERVAL=$2
      INTERVAL_SET=1
      CLI_CONFIGURATION_SET=1
      shift 2
      ;;
    --instance-name)
      require_value "$@"
      INSTANCE_NAME=$2
      INSTANCE_NAME_SET=1
      CLI_CONFIGURATION_SET=1
      shift 2
      ;;
    --instance-location)
      require_value "$@"
      INSTANCE_LOCATION=$2
      INSTANCE_LOCATION_SET=1
      CLI_CONFIGURATION_SET=1
      shift 2
      ;;
    --notification-config)
      require_value "$@"
      NOTIFICATION_CONFIG=$2
      CLI_CONFIGURATION_SET=1
      shift 2
      ;;
    --xui-executable)
      require_value "$@"
      XUI_EXECUTABLE=$2
      CLI_CONFIGURATION_SET=1
      shift 2
      ;;
    --database)
      require_value "$@"
      DATABASE=$2
      CLI_CONFIGURATION_SET=1
      shift 2
      ;;
    --stage-root)
      require_value "$@"
      STAGE_ROOT=$2
      shift 2
      ;;
    --platform)
      require_value "$@"
      PLATFORM=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ "$CONFIG_MODE" -eq 1 ] && [ "$CLI_CONFIGURATION_SET" -eq 1 ]; then
  echo "--config cannot be combined with other configuration options" >&2
  exit 2
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
  PYTHON=$(command -v python3 || true)
fi

load_deployment_plan() {
  if [ ! -f "$DEPLOYMENT_CONFIG" ]; then
    echo "deployment config was not found" >&2
    exit 1
  fi
  if [ ! -f "$SCRIPT_DIR/deployment_config.py" ]; then
    echo "missing installation file: deployment_config.py" >&2
    exit 1
  fi
  DEPLOYMENT_PLAN=$(
    "$PYTHON" "$SCRIPT_DIR/deployment_config.py" inspect \
      --config "$DEPLOYMENT_CONFIG" \
      --allow-non-root-owner
  )
  ENABLE=$(
    "$PYTHON" -c \
      'import json,sys; print(1 if json.loads(sys.argv[1])["scheduler_enabled"] else 0)' \
      "$DEPLOYMENT_PLAN"
  )
  INTERVAL=$(
    "$PYTHON" -c \
      'import json,sys; print(json.loads(sys.argv[1])["interval_seconds"])' \
      "$DEPLOYMENT_PLAN"
  )
  XUI_EXECUTABLE=$(
    "$PYTHON" -c \
      'import json,sys; print(json.loads(sys.argv[1])["xui_executable"])' \
      "$DEPLOYMENT_PLAN"
  )
  DATABASE=$(
    "$PYTHON" -c \
      'import json,sys; print(json.loads(sys.argv[1])["database"])' \
      "$DEPLOYMENT_PLAN"
  )
  RUNTIME_SERVICE=$(
    "$PYTHON" -c \
      'import json,sys; print(json.loads(sys.argv[1])["service_name"])' \
      "$DEPLOYMENT_PLAN"
  )
  NOTIFICATION_PROVIDER=$(
    "$PYTHON" -c \
      'import json,sys; print(json.loads(sys.argv[1])["notification_provider"])' \
      "$DEPLOYMENT_PLAN"
  )
  CONFIG_NOTIFICATIONS_ENABLED=$(
    "$PYTHON" -c \
      'import json,sys; print("true" if json.loads(sys.argv[1])["notifications_enabled"] else "false")' \
      "$DEPLOYMENT_PLAN"
  )
  DEPLOYMENT_PLAN_LOADED=1
}

if [ "$CONFIG_MODE" -eq 1 ] && [ -n "$PYTHON" ] && [ -x "$PYTHON" ]; then
  load_deployment_plan
fi

case "$INTERVAL" in
  ''|*[!0-9]*)
    echo "interval must be an integer from 60 to 86400" >&2
    exit 2
    ;;
esac
if [ "$INTERVAL" -lt 60 ] || [ "$INTERVAL" -gt 86400 ]; then
  echo "interval must be an integer from 60 to 86400" >&2
  exit 2
fi

if [ -n "$STAGE_ROOT" ]; then
  case "$STAGE_ROOT" in
    /*) ;;
    *)
      echo "stage root must be an absolute path" >&2
      exit 2
      ;;
  esac
  STAGE_ROOT=${STAGE_ROOT%/}
  if [ "$ENABLE" -eq 1 ] && [ "$CONFIG_MODE" -eq 0 ]; then
    echo "--enable cannot be used with --stage-root" >&2
    exit 2
  fi
elif [ "$(id -u)" -ne 0 ]; then
  echo "installer must run as root" >&2
  exit 1
fi

if [ -n "$PLATFORM" ] && [ -z "$STAGE_ROOT" ]; then
  echo "--platform is only allowed with --stage-root" >&2
  exit 2
fi
if [ -z "$PLATFORM" ]; then
  case "$(uname -s)" in
    Linux) PLATFORM=linux ;;
    Darwin) PLATFORM=macos ;;
    *)
      echo "unsupported platform; Linux and macOS are supported" >&2
      exit 1
      ;;
  esac
fi
if [ "$PLATFORM" != "linux" ] && [ "$PLATFORM" != "macos" ]; then
  echo "platform must be linux or macos" >&2
  exit 2
fi

find_executable() {
  if [ -n "$XUI_EXECUTABLE" ]; then
    [ -x "$XUI_EXECUTABLE" ] && [ -f "$XUI_EXECUTABLE" ] || return 1
    printf '%s\n' "$XUI_EXECUTABLE"
    return 0
  fi
  for candidate in \
    /usr/local/x-ui/x-ui \
    /usr/bin/x-ui \
    /opt/x-ui/x-ui \
    /usr/local/bin/x-ui
  do
    if [ -x "$candidate" ] && [ -f "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

find_database() {
  if [ -n "$DATABASE" ]; then
    [ -f "$DATABASE" ] || return 1
    printf '%s\n' "$DATABASE"
    return 0
  fi
  for candidate in \
    /etc/x-ui/x-ui.db \
    /usr/local/x-ui/x-ui.db \
    /usr/local/etc/x-ui/x-ui.db \
    /opt/x-ui/x-ui.db
  do
    if [ -f "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

if ! XUI_EXECUTABLE=$(find_executable); then
  echo "3x-ui executable was not found; nothing was installed" >&2
  exit 1
fi
if ! DATABASE=$(find_database); then
  echo "3x-ui SQLite database was not found; nothing was installed" >&2
  exit 1
fi

if [ -z "$STAGE_ROOT" ]; then
  for environment_file in \
    /etc/default/x-ui \
    /etc/conf.d/x-ui \
    /etc/sysconfig/x-ui
  do
    if [ -r "$environment_file" ] &&
      grep -Eiq '^[[:space:]]*XUI_DB_TYPE[[:space:]]*=[[:space:]]*(postgres|postgresql|pg)[[:space:]]*$' \
        "$environment_file"
    then
      echo "3x-ui uses PostgreSQL, which this monitor does not support; nothing was installed" >&2
      exit 1
    fi
  done
fi

if [ -z "$PYTHON" ] && [ -z "$STAGE_ROOT" ] && [ "$PLATFORM" = "linux" ]; then
  echo "Python 3 was not found; installing the distribution package"
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install python3
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm python
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3
  else
    echo "unable to install Python 3 with this distribution's package manager" >&2
    exit 1
  fi
  PYTHON=$(command -v python3 || true)
fi
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
  echo "Python 3.9 or newer is required" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Python 3.9 or newer is required" >&2
  exit 1
fi
if [ "$CONFIG_MODE" -eq 1 ] && [ "$DEPLOYMENT_PLAN_LOADED" -eq 0 ]; then
  load_deployment_plan
fi

for file in \
  anomaly_monitor.py \
  alertbridge_notify.py \
  deployment_config.py \
  feishu_notify.py \
  xui_discovery.py \
  install_ip2region.sh \
  uninstall.sh \
  monitor.example.json \
  alertbridge-events.example.json \
  alertbridge-notifications.example.json \
  deployment.example.json \
  feishu.example.json
do
  if [ ! -f "$SCRIPT_DIR/$file" ]; then
    echo "missing installation file: $file" >&2
    exit 1
  fi
done

if [ -n "$STAGE_ROOT" ]; then
  DISCOVERY_JSON=$(
    "$PYTHON" "$SCRIPT_DIR/xui_discovery.py" \
      --executable "$XUI_EXECUTABLE" \
      --database "$DATABASE" \
      --skip-backend-environment-check \
      --json
  )
else
  DISCOVERY_JSON=$(
    "$PYTHON" "$SCRIPT_DIR/xui_discovery.py" \
      --executable "$XUI_EXECUTABLE" \
      --database "$DATABASE" \
      --json
  )
fi
XUI_EXECUTABLE=$(
  "$PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["executable"])' \
    "$DISCOVERY_JSON"
)
DATABASE=$(
  "$PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["database"])' \
    "$DISCOVERY_JSON"
)

if [ "$PLATFORM" = "linux" ]; then
  RUNTIME_MANAGER=systemd
  if [ "$CONFIG_MODE" -eq 0 ]; then
    RUNTIME_SERVICE=x-ui.service
  fi
  if [ -z "$STAGE_ROOT" ] && {
    [ ! -d /run/systemd/system ] || ! command -v systemctl >/dev/null 2>&1
  }; then
    echo "this Linux installation requires systemd" >&2
    exit 1
  fi
else
  RUNTIME_MANAGER=process
  if [ "$CONFIG_MODE" -eq 0 ]; then
    RUNTIME_SERVICE=x-ui
  fi
  if [ -z "$STAGE_ROOT" ] && ! command -v launchctl >/dev/null 2>&1; then
    echo "launchd is required on macOS" >&2
    exit 1
  fi
fi

"$PYTHON" - \
  "$SCRIPT_DIR/anomaly_monitor.py" \
  "$DATABASE" \
  "$XUI_EXECUTABLE" \
  "$RUNTIME_MANAGER" \
  "$RUNTIME_SERVICE" \
  "$INTERVAL" \
  "$INSTANCE_NAME" \
  "$INSTANCE_LOCATION" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("anomaly_monitor", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
config = module.default_config()
config["database_path"] = sys.argv[2]
config["runtime"].update(
    {
        "service_executable": sys.argv[3],
        "service_manager": sys.argv[4],
        "service_name": sys.argv[5],
        "interval_seconds": int(sys.argv[6]),
    }
)
config["checks"]["traffic_interval_seconds"] = int(sys.argv[6])
config["instance"].update(
    {
        "name": sys.argv[7],
        "location": sys.argv[8],
    }
)
module.validate_config(config)
PY

select_notification_provider() {
  NOTIFICATION_PROVIDER=$1
  case "$NOTIFICATION_PROVIDER" in
    alertbridge)
      NOTIFIER_FILENAME="alertbridge_notify.py"
      NOTIFICATION_CONFIG_FILENAME="alertbridge.json"
      ;;
    feishu_webhook)
      NOTIFIER_FILENAME="feishu_notify.py"
      NOTIFICATION_CONFIG_FILENAME="feishu.json"
      ;;
    *)
      echo "notification provider is unsupported" >&2
      exit 1
      ;;
  esac
}

if [ "$CONFIG_MODE" -eq 1 ]; then
  select_notification_provider "$NOTIFICATION_PROVIDER"
elif [ -n "$NOTIFICATION_CONFIG" ]; then
  if [ ! -f "$NOTIFICATION_CONFIG" ]; then
    echo "notification config was not found" >&2
    exit 1
  fi
  NOTIFICATION_PROVIDER=$(
    "$PYTHON" - "$NOTIFICATION_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(data, dict):
    raise SystemExit("notification config must be an object")
print(data.get("provider", "alertbridge"))
PY
  )
  select_notification_provider "$NOTIFICATION_PROVIDER"
  "$PYTHON" - "$SCRIPT_DIR/$NOTIFIER_FILENAME" "$NOTIFICATION_CONFIG" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("notification_provider", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.load_config(sys.argv[2], require_owner=False)
PY
fi

dest() {
  printf '%s%s\n' "$STAGE_ROOT" "$1"
}

LIB_DIR=$(dest "/usr/local/lib/$PRODUCT")
CONFIG_DIR=$(dest "/etc/$PRODUCT")
STATE_DIR=$(dest "/var/lib/$PRODUCT")
DOC_DIR=$(dest "/usr/local/share/doc/$PRODUCT")
BIN_PATH=$(dest "/usr/local/bin/$PRODUCT")
MONITOR_CONFIG="$CONFIG_DIR/monitor.json"

if [ "$CONFIG_MODE" -eq 0 ] &&
  [ -z "$NOTIFICATION_CONFIG" ] &&
  [ -f "$MONITOR_CONFIG" ]
then
  NOTIFICATION_PROVIDER=$(
    "$PYTHON" - "$MONITOR_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(data.get("notifications", {}).get("provider", "alertbridge"))
PY
  )
  select_notification_provider "$NOTIFICATION_PROVIDER"
fi
INSTALLED_NOTIFICATION_CONFIG="$CONFIG_DIR/$NOTIFICATION_CONFIG_FILENAME"

install -d -m 0750 "$LIB_DIR"
install -d -m 0700 "$CONFIG_DIR"
install -d -m 0700 "$STATE_DIR"
install -d -m 0755 "$DOC_DIR"
install -d -m 0755 "$(dirname "$BIN_PATH")"
for file in anomaly_monitor.py alertbridge_notify.py deployment_config.py feishu_notify.py xui_discovery.py install_ip2region.sh uninstall.sh; do
  install -m 0750 "$SCRIPT_DIR/$file" "$LIB_DIR/$file"
done
install -m 0644 "$SCRIPT_DIR/README.md" "$DOC_DIR/README.md"
install -m 0600 "$SCRIPT_DIR/monitor.example.json" "$CONFIG_DIR/monitor.example.json"
install -m 0600 "$SCRIPT_DIR/alertbridge-events.example.json" "$CONFIG_DIR/alertbridge-events.example.json"
install -m 0600 "$SCRIPT_DIR/alertbridge-notifications.example.json" "$CONFIG_DIR/alertbridge-notifications.example.json"
install -m 0600 "$SCRIPT_DIR/deployment.example.json" "$CONFIG_DIR/deployment.example.json"
install -m 0600 "$SCRIPT_DIR/feishu.example.json" "$CONFIG_DIR/feishu.example.json"

NOTIFICATIONS_ENABLED=false
if [ "$CONFIG_MODE" -eq 1 ]; then
  NOTIFICATIONS_ENABLED=$CONFIG_NOTIFICATIONS_ENABLED
elif [ -n "$NOTIFICATION_CONFIG" ]; then
  install -m 0600 "$NOTIFICATION_CONFIG" "$INSTALLED_NOTIFICATION_CONFIG"
  NOTIFICATIONS_ENABLED=true
elif [ -f "$INSTALLED_NOTIFICATION_CONFIG" ]; then
  NOTIFICATIONS_ENABLED=true
fi

if [ "$CONFIG_MODE" -eq 1 ]; then
  "$PYTHON" "$LIB_DIR/deployment_config.py" materialize \
    --config "$DEPLOYMENT_CONFIG" \
    --platform "$PLATFORM" \
    --database "$DATABASE" \
    --executable "$XUI_EXECUTABLE" \
    --monitor-output "$MONITOR_CONFIG" \
    --notification-output "$INSTALLED_NOTIFICATION_CONFIG" \
    --allow-non-root-owner
elif [ ! -e "$MONITOR_CONFIG" ]; then
  "$PYTHON" - \
    "$MONITOR_CONFIG" \
    "$DATABASE" \
    "$XUI_EXECUTABLE" \
    "$RUNTIME_MANAGER" \
    "$RUNTIME_SERVICE" \
    "$INTERVAL" \
    "$INSTANCE_NAME" \
    "$INSTANCE_LOCATION" \
    "$NOTIFICATIONS_ENABLED" \
    "$NOTIFICATION_PROVIDER" \
    "$NOTIFIER_FILENAME" \
    "$NOTIFICATION_CONFIG_FILENAME" <<'PY'
import json
import os
import sys
from pathlib import Path

(
    config_path,
    database,
    executable,
    manager,
    service,
    interval,
    instance_name,
    instance_location,
    notifications_enabled,
    notification_provider,
    notifier_filename,
    notification_config_filename,
) = sys.argv[1:]
config = {
    "version": 1,
    "database_path": database,
    "state_path": "/var/lib/threexui-script-monitor/state.json",
    "lock_path": "/var/lib/threexui-script-monitor/monitor.lock",
    "instance": {
        "name": instance_name,
        "location": instance_location,
    },
    "runtime": {
        "interval_seconds": int(interval),
        "service_manager": manager,
        "service_name": service,
        "service_executable": executable,
    },
    "notifications": {
        "enabled": notifications_enabled == "true",
        "provider": notification_provider,
        "notifier_path": (
            f"/usr/local/lib/threexui-script-monitor/{notifier_filename}"
        ),
        "config_path": (
            f"/etc/threexui-script-monitor/{notification_config_filename}"
        ),
        "title_template": "{instance_name}：{event_title}",
        "message_template": (
            "监控对象：{instance_name}\n"
            "事件状态：{event_status}\n"
            "详细信息：{event_message}\n"
            "{event_details}"
        ),
    },
    "client_ip_details": {
        "exposure": "none",
        "max_entries": 8,
        "database_path": (
            "/usr/local/share/threexui-script-monitor/ip2region_v4.xdb"
        ),
    },
    "checks": {
        "node_heartbeat_max_age_seconds": 180,
        "node_latency_warn_ms": 2000,
        "node_cpu_warn_percent": 95,
        "node_memory_warn_percent": 90,
        "ip_window_seconds": 1800,
        "ip_warn_count": 4,
        "ip_critical_count": 8,
        "traffic_interval_seconds": int(interval),
        "traffic_min_elapsed_seconds": 60,
        "traffic_max_elapsed_seconds": max(1800, int(interval) * 3),
        "traffic_warn_bytes_per_interval": 5368709120,
        "traffic_critical_bytes_per_interval": 16106127360,
        "repeat_alert_seconds": 21600,
    },
}
path = Path(config_path)
path.write_text(
    json.dumps(config, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
os.chmod(path, 0o600)
PY
else
  "$PYTHON" - \
    "$LIB_DIR/anomaly_monitor.py" \
    "$MONITOR_CONFIG" \
    "$DATABASE" \
    "$XUI_EXECUTABLE" \
    "$RUNTIME_MANAGER" \
    "$RUNTIME_SERVICE" \
    "$INTERVAL_SET" \
    "$INTERVAL" \
    "$INSTANCE_NAME_SET" \
    "$INSTANCE_NAME" \
    "$INSTANCE_LOCATION_SET" \
    "$INSTANCE_LOCATION" \
    "$NOTIFICATION_CONFIG" \
    "$NOTIFICATION_PROVIDER" \
    "$NOTIFIER_FILENAME" \
    "$NOTIFICATION_CONFIG_FILENAME" <<'PY'
import importlib.util
import json
import os
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("anomaly_monitor", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
path = Path(sys.argv[2])
config = module.load_config(str(path))
config["database_path"] = sys.argv[3]
config["runtime"].update(
    {
        "service_executable": sys.argv[4],
        "service_manager": sys.argv[5],
        "service_name": sys.argv[6],
    }
)
if sys.argv[7] == "1":
    config["runtime"]["interval_seconds"] = int(sys.argv[8])
    config["checks"]["traffic_interval_seconds"] = int(sys.argv[8])
if sys.argv[9] == "1":
    config["instance"]["name"] = sys.argv[10]
if sys.argv[11] == "1":
    config["instance"]["location"] = sys.argv[12]
if sys.argv[13]:
    config["notifications"].update(
        {
            "enabled": True,
            "provider": sys.argv[14],
            "notifier_path": (
                "/usr/local/lib/threexui-script-monitor/" + sys.argv[15]
            ),
            "config_path": (
                "/etc/threexui-script-monitor/" + sys.argv[16]
            ),
        }
    )
module.validate_config(config)
temporary = path.with_name(path.name + ".tmp")
temporary.write_text(
    json.dumps(config, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
fi

"$PYTHON" - "$LIB_DIR/anomaly_monitor.py" "$MONITOR_CONFIG" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("anomaly_monitor", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.load_config(sys.argv[2])
PY
INTERVAL=$(
  "$PYTHON" -c \
    'import json,sys; from pathlib import Path; print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["runtime"]["interval_seconds"])' \
    "$MONITOR_CONFIG"
)
NOTIFICATIONS_ENABLED=$(
  "$PYTHON" -c \
    'import json,sys; from pathlib import Path; print("true" if json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["notifications"]["enabled"] else "false")' \
    "$MONITOR_CONFIG"
)

ln -sfn "/usr/local/lib/$PRODUCT/anomaly_monitor.py" "$BIN_PATH"

if [ "$PLATFORM" = "linux" ]; then
  SYSTEMD_DIR=$(dest "/etc/systemd/system")
  install -d -m 0755 "$SYSTEMD_DIR"
  install -m 0644 \
    "$SCRIPT_DIR/systemd/threexui-script-monitor.service" \
    "$SYSTEMD_DIR/threexui-script-monitor.service"
  "$PYTHON" - \
    "$SCRIPT_DIR/systemd/threexui-script-monitor.timer.in" \
    "$SYSTEMD_DIR/threexui-script-monitor.timer" \
    "$INTERVAL" <<'PY'
import os
import sys
from pathlib import Path

source, destination, interval = sys.argv[1:]
payload = Path(source).read_text(encoding="utf-8").replace(
    "@INTERVAL_SECONDS@",
    interval,
)
path = Path(destination)
path.write_text(payload, encoding="utf-8")
os.chmod(path, 0o644)
PY
else
  LAUNCHD_DIR=$(dest "/Library/LaunchDaemons")
  install -d -m 0755 "$LAUNCHD_DIR"
  "$PYTHON" - \
    "$SCRIPT_DIR/launchd/com.threexui.script-monitor.plist.in" \
    "$LAUNCHD_DIR/com.threexui.script-monitor.plist" \
    "$INTERVAL" <<'PY'
import os
import sys
from pathlib import Path

source, destination, interval = sys.argv[1:]
payload = Path(source).read_text(encoding="utf-8").replace(
    "@INTERVAL_SECONDS@",
    interval,
)
path = Path(destination)
path.write_text(payload, encoding="utf-8")
os.chmod(path, 0o644)
PY
fi

"$PYTHON" -m py_compile \
  "$LIB_DIR/anomaly_monitor.py" \
  "$LIB_DIR/alertbridge_notify.py" \
  "$LIB_DIR/deployment_config.py" \
  "$LIB_DIR/feishu_notify.py" \
  "$LIB_DIR/xui_discovery.py"

if [ -z "$STAGE_ROOT" ]; then
  ROOT_GROUP=$(id -gn root)
  chown -R root:"$ROOT_GROUP" "$LIB_DIR" "$CONFIG_DIR" "$STATE_DIR" "$DOC_DIR"
  chown -h root:"$ROOT_GROUP" "$BIN_PATH"
  if [ "$PLATFORM" = "linux" ]; then
    systemctl daemon-reload
    if command -v systemd-analyze >/dev/null 2>&1; then
      systemd-analyze verify \
        /etc/systemd/system/threexui-script-monitor.service \
        /etc/systemd/system/threexui-script-monitor.timer
    fi
    if [ "$ENABLE" -eq 1 ]; then
      "$BIN_PATH" --dry-run >/dev/null
      systemctl start threexui-script-monitor.service
      systemctl enable --now threexui-script-monitor.timer
    fi
  elif [ "$ENABLE" -eq 1 ]; then
    "$BIN_PATH" --dry-run >/dev/null
    launchctl bootout system/com.threexui.script-monitor 2>/dev/null || true
    launchctl bootstrap system \
      /Library/LaunchDaemons/com.threexui.script-monitor.plist
  fi
fi

echo "ThreeXUIScriptMonitor installed"
echo "3x-ui database: $DATABASE"
echo "run interval: ${INTERVAL}s"
if [ "$NOTIFICATIONS_ENABLED" = "true" ]; then
  echo "notifications: configured"
else
  echo "notifications: disabled until a notification config is installed"
fi
if [ -n "$STAGE_ROOT" ]; then
  echo "scheduler: staged but not enabled"
elif [ "$ENABLE" -eq 0 ]; then
  echo "scheduler: installed but not enabled"
fi
