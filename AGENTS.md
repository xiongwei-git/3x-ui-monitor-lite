# ThreeXUIScriptMonitor 工作规则

- 开始前读取 `PROJECT_MEMORY.md`、README、operator runbook、相关代码和当前运行日志。
- 项目只读检查本机 3x-ui；不得修改数据库、重启 3x-ui、封禁地址或自动修复。
- 生产主机、计时器、配置、通知、AlertBridge/飞书投递和日志都须实时核验；先检查“检测”与“投递”两个环节，再判断无告警原因。
- 测试通知或变更运行配置必须有明确授权；不记录 Token、Secret、Webhook、原始 IP 或完整私密 URL。
