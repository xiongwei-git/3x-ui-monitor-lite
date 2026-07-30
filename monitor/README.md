# ThreeXUIScriptMonitor Runtime

此目录是单文件安装包的运行时 payload。

## 组件

- `anomaly_monitor.py`：只读数据库检查、去重、恢复和通知模板。
- `alertbridge_notify.py`：AlertBridge HMAC Events / Bearer Notifications。
- `xui_discovery.py`：3x-ui 可执行文件、SQLite 结构和能力探测。
- `install.sh`：Linux systemd / macOS LaunchDaemon 安装。
- `uninstall.sh`：默认保留配置与状态的卸载器。
- `install_ip2region.sh`：可选的固定版本离线 IPv4 地区和运营商数据。

## 源码目录安装

```sh
sudo ./install.sh \
  --instance-name '3x-ui Monitor' \
  --interval 300 \
  --notification-config ./alertbridge.json
```

安装器先探测 3x-ui，再创建产品目录。若只想生成可检查的文件树，可使用
`--stage-root /absolute/path`；该模式不会要求 root、不会启动服务。

完整说明见项目根目录 [README](../README.md) 和
[运维手顺](../docs/operator-runbook.md)。
