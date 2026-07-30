# ThreeXUIScriptMonitor

ThreeXUIScriptMonitor 是一套无 UI、低资源占用、可独立部署的 3x-ui 异常监控。
它只读访问本机 3x-ui SQLite 数据库和服务状态，不依赖其他监控或运维系统，也不
使用 Docker。

> 当前版本为 `0.1.0-dev`。代码已经完成本地测试，但尚未完成全部目标发行版的
> 真实主机安装验收。

## 能力

- 自动发现并验证本机 3x-ui 可执行文件和 `x-ui.db`；找不到或数据库不兼容时，
  在写入产品文件前退出。
- 检查本机 3x-ui 服务状态。
- 数据库存在多节点表时，检查节点在线、心跳、Xray、配置同步、延迟、CPU 和内存；
  旧版或单机数据库缺少这些表时自动跳过，不影响其他检查。
- 统计客户端近期公网 IP 数和两次运行之间的流量增长。
- 可显式启用管理员私密 IP 明细，离线补充地区和运营商；原始 IP 不写入状态文件。
- 对异常去重、定期重复提醒，并发送恢复通知。
- 支持 AlertBridge 的 HMAC Events 和 Bearer Notifications 两种接入方式。
- 通知标题、正文、实例名称和位置可配置，并内置默认模板。
- 运行周期可配置为 60–86400 秒。

监控不会修改 3x-ui 数据库、重启服务、封禁地址或自动修复。

## 平台

- Linux：Ubuntu、Debian、CentOS、Fedora、RHEL 系、Alibaba Cloud Linux 等
  使用 systemd 的发行版。
- macOS：仅实验性支持已经存在且能通过探测的自定义 3x-ui 安装；调度使用
  LaunchDaemon。3x-ui 官方目前不支持 macOS，因此本项目不会安装或维护 3x-ui。
- Windows：不支持。

Linux 缺少 Python 时，安装器会在确认 3x-ui 文件存在后，尝试使用系统包管理器
安装 Python 3。实际运行要求 Python 3.9 或更高版本。

## 快速开始

克隆仓库：

```sh
git clone https://github.com/xiongwei-git/3x-ui-monitor-lite.git
cd 3x-ui-monitor-lite
```

如果暂时不需要通知，可直接安装并启用：

```sh
sudo ./monitor/install.sh \
  --instance-name '3x-ui Monitor' \
  --interval 300 \
  --enable
```

安装器会自动寻找 3x-ui 可执行文件和 `x-ui.db`。找不到或数据库不兼容时会退出，
不会创建 ThreeXUIScriptMonitor 产品文件。

如 3x-ui 使用了自定义路径：

```sh
sudo ./monitor/install.sh \
  --xui-executable /opt/x-ui/x-ui \
  --database /opt/x-ui/x-ui.db \
  --instance-name 'My 3x-ui' \
  --interval 300 \
  --enable
```

## 配置通知

AlertBridge 支持两种接入方式：

| 模式 | 示例文件 | 特点 |
| --- | --- | --- |
| HMAC Events | `monitor/alertbridge-events.example.json` | 推荐；支持告警、恢复、去重和事件生命周期 |
| Bearer Notifications | `monitor/alertbridge-notifications.example.json` | 每次发送一条独立普通通知 |

复制对应示例并填写参数：

```sh
cp monitor/alertbridge-events.example.json alertbridge.json
chmod 600 alertbridge.json
```

然后安装：

```sh
sudo ./monitor/install.sh \
  --instance-name 'SG-Panel-01' \
  --instance-location 'Singapore' \
  --interval 300 \
  --notification-config ./alertbridge.json \
  --enable
```

安装器只把密钥配置复制到 `/etc/threexui-script-monitor/alertbridge.json`，不会把
密钥写入监控主配置或状态文件。

## 安装参数

| 参数 | 说明 |
| --- | --- |
| `--enable` | 安装后启用并启动调度 |
| `--interval SECONDS` | 运行周期，范围 60–86400 秒，默认 300 |
| `--instance-name NAME` | 通知中显示的实例名称 |
| `--instance-location TEXT` | 可选的位置说明 |
| `--notification-config FILE` | AlertBridge 配置文件；省略时通知保持关闭 |
| `--xui-executable FILE` | 手动指定 3x-ui 可执行文件 |
| `--database FILE` | 手动指定 3x-ui SQLite 数据库 |
| `--stage-root DIR` | 只生成安装文件树，不启用服务，用于测试 |

不加 `--enable` 时，程序和调度文件会安装，但不会启动。可先执行试运行：

```sh
sudo /usr/local/bin/threexui-script-monitor --dry-run --json
```

`--dry-run` 不发送通知、不修改状态，也不建立流量基线。

## 构建单文件安装器

```sh
sh scripts/build-installer.sh
```

产物位于 `dist/`，包含版本号，并生成同名 `.sha256`。安装器会在解包前再次校验
内嵌 payload。正式“一行下载并安装”还需要把该产物发布到一个稳定 HTTPS 地址；
当前仓库没有虚构下载地址。

将生成的安装器和通知配置安全传到目标主机后，可以只用该安装器完成部署：

```sh
sudo sh threexui-script-monitor-0.1.0-dev-installer.sh \
  --instance-name 'SG-Panel-01' \
  --instance-location 'Singapore' \
  --interval 300 \
  --notification-config ./alertbridge.json \
  --enable
```

完整参数、运维与卸载见 [运维手顺](docs/operator-runbook.md)。

## 通知模板

默认标题：

```text
{instance_name}：{event_title}
```

默认正文：

```text
监控对象：{instance_name}
事件状态：{event_status}
详细信息：{event_message}
{event_details}
```

标题允许 `{instance_name}`、`{instance_location}`、`{event_title}`、
`{severity}`；正文还允许 `{event_status}`、`{event_message}`、
`{event_details}`。未知字段、格式化表达式和转换表达式会被拒绝。

## 安装后的路径

- `/usr/local/bin/threexui-script-monitor`
- `/usr/local/lib/threexui-script-monitor/`
- `/etc/threexui-script-monitor/monitor.json`
- `/etc/threexui-script-monitor/alertbridge.json`
- `/var/lib/threexui-script-monitor/`
- Linux：`threexui-script-monitor.service` 和 `.timer`
- macOS：`com.threexui.script-monitor.plist`

## 启停和日志

Linux：

```sh
sudo systemctl status threexui-script-monitor.timer --no-pager
sudo systemctl start threexui-script-monitor.service
sudo journalctl -u threexui-script-monitor.service -n 100 --no-pager
```

macOS：

```sh
sudo launchctl print system/com.threexui.script-monitor
sudo tail -n 100 /var/log/threexui-script-monitor.error.log
```

## 更新和卸载

使用新版源码或单文件安装器重新运行安装命令即可更新。已有 `monitor.json` 会保留
并重新校验；未显式传入的新周期、实例名称和通知开关不会被重置。

默认卸载会保留配置和状态：

```sh
sudo /usr/local/lib/threexui-script-monitor/uninstall.sh
```

彻底删除：

```sh
sudo /usr/local/lib/threexui-script-monitor/uninstall.sh --purge
```

`--purge` 会删除配置、状态和可选 IP 数据，执行前应先备份。

## 本地验证

```sh
python3 -m unittest discover -s test -p '*_test.py'
python3 -m py_compile \
  monitor/anomaly_monitor.py \
  monitor/alertbridge_notify.py \
  monitor/xui_discovery.py
sh -n \
  monitor/install.sh \
  monitor/uninstall.sh \
  monitor/install_ip2region.sh \
  scripts/build-installer.sh
```

设计决策见 [ADR 0002](docs/adr/0002-standalone-script-product.md)。
