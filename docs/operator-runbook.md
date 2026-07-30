# ThreeXUIScriptMonitor 运维手顺

最后整理：2026-07-30

## 1. 边界

ThreeXUIScriptMonitor 是一套独立、无 UI 的本机监控。它读取 3x-ui 服务状态和
SQLite 数据库，不修改数据库、不重启服务、不变更防火墙，也不自动封禁客户端。

本地监控无法报告整台主机宕机、断网或通知服务不可达，主机可用性仍需独立的外部
监控。

## 2. 安装前提

- 目标主机已经安装 3x-ui。
- 数据源为本机 SQLite；PostgreSQL 暂不支持。
- Linux 使用 systemd；macOS 只实验性支持已有的自定义 3x-ui。
- 发送通知时，目标主机必须能访问 AlertBridge 或飞书官方 HTTPS 地址。
- Python 3.9+；Linux 安装器可尝试通过系统包管理器补齐。

安装器只检查固定的常见路径，不扫描整台磁盘。自定义安装可传：

```sh
--xui-executable /absolute/path/to/x-ui
--database /absolute/path/to/x-ui.db
```

数据库至少必须包含兼容的 `settings` 和 `inbounds` 表。节点、IP 和流量表按实际
能力启用；缺少可选表不会导致整个监控失败。

## 3. 准备通知配置

安装器根据配置中的 `provider` 自动选择通知器。可选 AlertBridge 或飞书自定义
机器人 Webhook。

### 3.1 AlertBridge

[AlertBridge](https://github.com/xiongwei-git/alertbridge) 是独立的统一通知网关，
适合集中管理多个服务、多个通知渠道、路由和投递记录。

#### HMAC Events，推荐

从 `monitor/alertbridge-events.example.json` 复制。该模式调用
`/api/v1/events`，支持 `firing` / `resolved`、稳定 `dedupe_key` 和事件生命周期。

#### Bearer Notifications

从 `monitor/alertbridge-notifications.example.json` 复制。该模式调用
`/api/v1/notifications`。异常和恢复都是互相独立的普通消息，不形成 AlertBridge
事件生命周期。

配置文件必须设为仅管理员可读：

```sh
chmod 600 alertbridge.json
```

### 3.2 直连飞书自定义机器人

适合只有一个飞书接收群、不需要统一通知网关的部署。飞书自定义机器人 Webhook
只能向其所在群发送消息，不提供 AlertBridge 的多渠道路由、集中投递记录和事件
生命周期。

在目标群中添加自定义机器人，建议开启签名校验；如启用关键词校验，也要填写同一
关键词。随后：

```sh
cp monitor/feishu.example.json feishu.json
chmod 600 feishu.json
```

配置字段：

- `webhook_url`：飞书或 Lark 自定义机器人的完整 Webhook 地址；
- `signing_secret`：签名密钥，建议开启；未启用签名时填空字符串；
- `keyword`：机器人启用的关键词；未启用时填空字符串。

飞书官方说明见
[自定义机器人指南](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)。

不要把 Token、Secret 或完整 Webhook URL 写进项目、聊天、截图或命令参数。

## 4. 构建和校验安装包

```sh
sh scripts/build-installer.sh
cd dist
shasum -a 256 -c threexui-script-monitor-0.1.0-dev-installer.sh.sha256
```

Linux 可使用 `sha256sum -c`。单文件安装器还会自行验证内嵌 payload。

## 5. 安装

```sh
sudo sh threexui-script-monitor-0.1.0-dev-installer.sh \
  --instance-name '3x-ui Monitor' \
  --instance-location 'Singapore' \
  --interval 300 \
  --notification-config ./feishu.json
```

此时仅安装，不启用调度。先执行：

```sh
sudo /usr/local/bin/threexui-script-monitor --dry-run --json
```

`--dry-run` 不发通知、不写状态，也不建立流量基线。确认无误后：

Linux：

```sh
sudo systemctl start threexui-script-monitor.service
sudo systemctl enable --now threexui-script-monitor.timer
sudo systemctl list-timers threexui-script-monitor.timer --all --no-pager
```

macOS：

```sh
sudo launchctl bootstrap system \
  /Library/LaunchDaemons/com.threexui.script-monitor.plist
sudo launchctl print system/com.threexui.script-monitor
```

也可在首次安装时直接加 `--enable`，但正式环境仍建议先试运行。

## 6. 周期、实例和模板

安装参数 `--interval` 接受 60–86400 秒，并写入：

- `runtime.interval_seconds`
- Linux timer 的 `OnUnitActiveSec`
- macOS plist 的 `StartInterval`

`instance.name` 和 `instance.location` 用于通知模板。修改
`/etc/threexui-script-monitor/monitor.json` 后，先运行试检查。修改周期时还要重新
运行安装器生成调度文件，或同步修改 timer/plist，避免配置和实际周期不一致。

## 7. 日常检查

Linux：

```sh
sudo systemctl status threexui-script-monitor.timer --no-pager
sudo systemctl status threexui-script-monitor.service --no-pager
sudo journalctl -u threexui-script-monitor.service -n 100 --no-pager
```

macOS：

```sh
sudo launchctl print system/com.threexui.script-monitor
sudo tail -n 100 /var/log/threexui-script-monitor.error.log
```

试运行：

```sh
sudo /usr/local/bin/threexui-script-monitor --dry-run --json
```

第一次正式运行只建立流量基线。通知失败时状态不会保存，下一轮会再次尝试。

AlertBridge 返回 `queued` 只证明入口接受了请求，不代表最终渠道已送达；还要检查
AlertBridge 的下游投递记录。飞书直连返回 `sent` 表示飞书接口已接受消息，不等于
用户已阅读。

## 8. 手动测试通知

命令从 root-only 配置读取密钥，不在命令行暴露。

AlertBridge：

```sh
sudo /usr/local/lib/threexui-script-monitor/alertbridge_notify.py \
  --status test \
  --severity info \
  --title '3x-ui 通知链路测试' \
  --message 'ThreeXUIScriptMonitor test'
```

HMAC 模式会发送 `status=test` 事件；Bearer 模式会发送一条普通通知。

飞书直连：

```sh
sudo /usr/local/lib/threexui-script-monitor/feishu_notify.py \
  --status test \
  --severity info \
  --title '3x-ui 通知链路测试' \
  --message 'ThreeXUIScriptMonitor test'
```

## 9. 管理员 IP 明细

默认 `client_ip_details.exposure=none`，通知中只出现 IP 数量。只有收件人全部具备
查看原始 IP 权限时，才可启用明细。

安装固定版本的离线数据：

```sh
sudo /usr/local/lib/threexui-script-monitor/install_ip2region.sh
sudo cat /usr/local/share/threexui-script-monitor/ip2region-version.txt
```

然后修改：

```json
{
  "client_ip_details": {
    "exposure": "full",
    "max_entries": 8,
    "database_path": "/usr/local/share/threexui-script-monitor/ip2region_v4.xdb"
  }
}
```

离线库提供 IPv4 地区和运营商，不提供 ASN。IPv6 按 `/64` 聚合，当前无离线归属。
原始 IP 不写入 `state.json`，但会出现在私密通知和 `--dry-run --json` 输出中。

## 10. 更新与回滚

更新前备份：

- `/etc/threexui-script-monitor/`
- `/var/lib/threexui-script-monitor/`
- 当前安装包和校验文件

运行新安装器时，已有 `monitor.json` 会保留并重新校验；代码和调度文件会更新。
先不加 `--enable`，完成试运行后再重启或启用调度。

旧版或其他同类监控不能与本产品长期同时启用，否则可能重复告警。迁移已有实例时
必须单独制定备份、停旧、安装新、试运行、启新和回滚步骤。

## 11. 卸载

默认保留配置和状态：

```sh
sudo /usr/local/lib/threexui-script-monitor/uninstall.sh
```

连配置、状态和可选 IP 数据一起删除：

```sh
sudo /usr/local/lib/threexui-script-monitor/uninstall.sh --purge
```

`--purge` 不可逆，执行前先备份。

## 12. 常见故障

| 现象 | 处理 |
| --- | --- |
| `3x-ui executable was not found` | 用 `--xui-executable` 指定真实可执行文件 |
| `database is not a compatible 3x-ui SQLite database` | 确认是 3x-ui SQLite，而非 PostgreSQL 或其他数据库 |
| `unknown config key` | 修正拼写；配置采用严格字段校验 |
| `notification config permissions must be 0600` | 修正通知密钥文件权限 |
| 服务 active 但没消息 | 先看日志和试运行；没有异常时本来就不会通知 |
| `queued` 但下游没收到 | 检查 AlertBridge 路由和最终投递记录 |
| `Feishu rejected the notification` | 检查机器人是否启用了正确的签名或关键词 |
| 监控主机宕机没有通知 | 增加独立外部可用性监控 |

## 13. 安全规则

- 主配置不得对 group/other 可写，通知密钥配置必须为 `0600`。
- 安装器与监控只读打开 `x-ui.db`。
- 不把带原始 IP 的试运行输出复制到公开位置。
- 不用真实 `firing` 事件做日常链路测试。
- 不因告警自动执行服务重启、防火墙修改或客户端封禁。
