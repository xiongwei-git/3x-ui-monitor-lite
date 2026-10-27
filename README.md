# ThreeXUIScriptMonitor

ThreeXUIScriptMonitor 是一套无 UI、低资源占用、可独立部署的 3x-ui 异常监控。
它只读访问本机 3x-ui SQLite 数据库和服务状态，不依赖其他监控或运维系统，也不
使用 Docker。

> 当前版本为 `0.1.0-dev`。代码已经完成本地测试，但尚未完成全部目标发行版的
> 真实主机安装验收。

## 生产部署记录

截至 **2026-10-28**，监控运行在 **JTTI-Japan-01**，读取该机迁移后的 3x-ui
数据库，每 300 秒检查一次，通过 AlertBridge 发送飞书通知。新加坡原实例已停用。
JTTI 的正常检查、测试通知最终投递及用户收信均已确认。

部署路径、监控节点、通知标识和迁移备份见 [JTTI 部署记录](docs/deployment-jtti.md)。
这是带日期的环境快照，后续运行状态应通过日志和定时器实时核验。

## 能力

- 自动发现并验证本机 3x-ui 可执行文件和 `x-ui.db`；找不到或数据库不兼容时，
  在写入产品文件前退出。
- 检查本机 3x-ui 服务状态。
- 数据库存在多节点表时，检查节点在线、心跳、Xray、配置同步、延迟、CPU 和内存；
  旧版或单机数据库缺少这些表时自动跳过，不影响其他检查。
- 统计客户端近期公网 IP 数和两次运行之间的流量增长。
- 可显式启用管理员私密 IP 明细，离线补充地区和运营商；原始 IP 不写入状态文件。
- 同一节点的不可用原因合并为一个事件，连续两次异常后通知，恢复只通知一次。
- 配置待同步、资源、延迟、IP 和流量异常继续检查并记录，但不发送通知。
- 支持 AlertBridge 的 HMAC Events、Bearer Notifications，以及飞书自定义机器人
  Webhook 直连。
- 通知标题、正文、实例名称和位置可配置，并内置默认模板。
- 运行周期可配置为 60–86400 秒。

监控不会修改 3x-ui 数据库、重启服务、封禁地址或自动修复。

### 通知规则

节点的非 online 状态（含 offline、unknown）、心跳超时、Xray 非 running 状态，
合并为一个 `node:<id>:availability` 事件。本机 3x-ui 服务异常单独作为一个服务
不可用事件。连续两次有效采样异常才发送通知；默认周期 300 秒，通常约 5–10 分钟
确认。异常原因变化或长时间持续不会重复通知。所有不可用条件消失后只发送一次
恢复，包含从首次异常采样起算的近似持续时间。

这里的“不可用”是管理面检测结论，不是用户侧代理连通性实测。节点停用、删除或
节点表不兼容不会被误判为恢复；缺少有效采样时无法断言节点健康。采样间隔超过
两个配置周期加 30 秒，尚未发出的告警会重新累计确认次数。

配置、CPU、内存、延迟、多公网 IP 和流量异常不再通知。旧配置中的
`checks.repeat_alert_seconds` 仍可加载，但不再触发周期提醒。旧版逐检查项告警
状态会静默退休，不在升级时集中发送恢复消息；正在发生的故障按新事件重新确认。
AlertBridge 中的旧事件历史不会由本脚本自动删除或关闭。

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

### 使用一个标准配置文件

除逐项传命令行参数外，也可以把部署所需的全部信息写入一个 JSON：

```sh
cp monitor/deployment.example.json deployment.json
chmod 600 deployment.json
sudo ./monitor/install.sh --config ./deployment.json
```

`deployment.json` 同时包含调度开关和周期、3x-ui 路径、实例信息、检查阈值、IP
明细策略、通知模板以及 AlertBridge 或飞书通道配置。安装器会严格校验后拆分为：

- 不含通知密钥的运行配置 `monitor.json`；
- 单独以 `0600` 保存的 AlertBridge 或飞书通知配置。

因为标准配置可能包含 Token、Secret 或完整 Webhook，它自身也必须设置为 `0600`，
不得提交到 Git。`--config` 不能与 `--enable`、`--interval`、
`--instance-name`、`--instance-location`、`--notification-config`、
`--xui-executable` 或 `--database` 混用；标准配置是本次安装的唯一配置来源。

运行时监控程序原本也支持 `--config /path/to/monitor.json`。两者用途不同：
`install.sh --config` 读取完整部署配置，`threexui-script-monitor --config` 读取安装后
的无密钥运行配置。

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

通知不是必须经过 AlertBridge。安装器会读取通知配置中的 `provider`，自动安装并
启用对应通知器。

### 方式一：通过 AlertBridge

[AlertBridge](https://github.com/xiongwei-git/alertbridge) 是独立的统一通知网关，
可把多个服务的事件集中路由到飞书、Telegram、邮件、ntfy 等渠道，并统一管理渠道
密钥、路由和投递记录。需要多服务共用通知、集中配置或告警生命周期时，推荐使用
这种方式。

ThreeXUIScriptMonitor 支持 AlertBridge 的两种入口：

| 接入模式 | 示例文件 | 特点 |
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

### 方式二：直连飞书自定义机器人

不部署 AlertBridge 也可以直接发送到飞书群机器人。该方式配置最少，适合只有一个
接收群的轻量部署；但没有 AlertBridge 提供的多渠道路由、集中投递记录和事件
生命周期。

1. 在目标飞书群的设置中添加“自定义机器人”，取得 Webhook 地址。
2. 建议开启“签名校验”；如机器人启用了关键词校验，把同一关键词写入配置。
3. 复制并填写示例文件：

```sh
cp monitor/feishu.example.json feishu.json
chmod 600 feishu.json
```

4. 使用同一个安装参数部署：

```sh
sudo ./monitor/install.sh \
  --instance-name 'SG-Panel-01' \
  --instance-location 'Singapore' \
  --interval 300 \
  --notification-config ./feishu.json \
  --enable
```

飞书 Webhook 完整地址和签名密钥都属于敏感信息，不要提交到 Git、粘贴到聊天或
直接写进安装命令。直连方式使用飞书文本消息，监控本身的异常去重和恢复通知仍然
有效。飞书侧的创建与安全设置见
[自定义机器人指南](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)。

安装器会把密钥配置复制到 `/etc/threexui-script-monitor/alertbridge.json` 或
`/etc/threexui-script-monitor/feishu.json`，不会把密钥写入监控主配置或状态文件。

## 安装参数

| 参数 | 说明 |
| --- | --- |
| `--config FILE` | 完整标准部署配置；不能与其他配置参数混用 |
| `--enable` | 安装后启用并启动调度 |
| `--interval SECONDS` | 运行周期，范围 60–86400 秒，默认 300 |
| `--instance-name NAME` | 通知中显示的实例名称 |
| `--instance-location TEXT` | 可选的位置说明 |
| `--notification-config FILE` | AlertBridge 或飞书 Webhook 配置；省略时通知保持关闭 |
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
  --notification-config ./feishu.json \
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
- `/etc/threexui-script-monitor/alertbridge.json` 或 `feishu.json`
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
  monitor/deployment_config.py \
  monitor/feishu_notify.py \
  monitor/xui_discovery.py
sh -n \
  monitor/install.sh \
  monitor/uninstall.sh \
  monitor/install_ip2region.sh \
  scripts/build-installer.sh
```

设计决策见 [ADR 0002](docs/adr/0002-standalone-script-product.md)。
