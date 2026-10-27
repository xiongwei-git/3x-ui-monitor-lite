# JTTI 生产部署记录

核验日期：**2026-10-28**，文内时间均为北京时间（UTC+8）。这是环境快照，
实时状态以服务器检查与通知投递记录为准。本文件不保存服务器地址、密钥或私密 URL。

## 部署位置

新加坡的 3x-ui 主面板及 ThreeXUIScriptMonitor 已迁至 **JTTI-Japan-01**。
监控与 3x-ui 同机运行，只读访问本机数据库。

| 项目 | 已核验配置或状态 |
| --- | --- |
| 主机 | JTTI-Japan-01，日本 |
| 系统 / Python | CentOS Stream 9 / Python 3.9.25 |
| 调度 | systemd，300 秒一次，timer 为 enabled/active |
| 3x-ui | x-ui.service 为 enabled/active，数据库 quick_check=ok |
| 监控实例名 | JTTI-Japan-01 / 3x-ui 集群 |
| 通知标题模板 | JTTI 日本 3x-ui 监控：{event_title} |
| 通知方式 | AlertBridge HMAC Events → 飞书 |
| 通知 source | vpsops-jtti-japan |
| 通知 server 标签 | JTTI-Japan-01 |
| 代码基线 | 6415aa4，可用性事件聚合版本；本次迁移收尾未修改代码 |

服务器系统时区显示可能与北京时间不同；排查 journal 时须核对时区，避免错认
事件发生时间。项目通过 conn-hub 中的登记连接管理服务器访问，不在本仓库保存
连接配置。

## 安装与数据路径

| 用途 | JTTI 上的路径 |
| --- | --- |
| 3x-ui 可执行文件 | /usr/local/x-ui/x-ui |
| 3x-ui SQLite | /etc/x-ui/x-ui.db |
| 监控入口 | /usr/local/bin/threexui-script-monitor |
| 监控程序 | /usr/local/lib/threexui-script-monitor/anomaly_monitor.py |
| 运行配置 | /etc/threexui-script-monitor/monitor.json |
| 通知配置 | /etc/threexui-script-monitor/alertbridge.json |
| 状态文件 | /var/lib/threexui-script-monitor/state.json |
| 运行锁 | /var/lib/threexui-script-monitor/monitor.lock |
| systemd 单元 | threexui-script-monitor.service / threexui-script-monitor.timer |

运行配置、通知配置和状态文件以 `0600` 保存。已部署监控程序
`anomaly_monitor.py` 的 SHA-256 为：

```text
1777b20e676a33c80f36ade1f63165989e9b24dfb9374624212f62278b3fefd8
```

## 监控范围与通知规则

2026-10-28 02:36 核验的启用节点如下，均为 online、Xray running、配置待同步标记为 0：

| 节点 ID | 节点名称 |
| --- | --- |
| 4 | LISA-US |
| 5 | LLY-Japan |
| 6 | DMIT-US |
| 8 | Dogyun-HK-01 |

同一节点的状态、心跳、Xray 异常合并为一个不可用事件，连续两次有效采样异常
才通知，原因变化和持续异常不重复提醒；不可用条件全部消失后只恢复一次。
本机 3x-ui 服务使用独立事件。配置待同步、CPU、内存、延迟、IP 和流量异常只
检查并记录。详细规则见 [ADR 0003](adr/0003-availability-only-notifications.md)。

DogYun 香港作为节点列在 JTTI 面板中。本次没有把 ThreeXUIScriptMonitor
另行部署到香港，监控程序与数据源均位于 JTTI。

## 迁移收尾与验证

- **2026-10-28 02:08**：通过 DMIT 中转停用新加坡的监控；新旧 timer 均为
  disabled/inactive，监控进程停止。旧程序、配置和状态保留，未经切换决定不恢复。
- **2026-10-28 02:36**：确认 JTTI 上 3x-ui 和监控已在运行，脚本与代码基线
  一致，4 个启用节点状态正常。
- **2026-10-28 02:39**：将通知 source 从 vpsops-singapore 改为
  vpsops-jtti-japan，server 标签从 Ali-Singapore-01 改为 JTTI-Japan-01。
  确认节点表中已无 id=7 后，仅移除状态文件的 `node:7:availability`
  （旧 QQG-US）孤立记录，其他状态和流量基线保留。
- 修改后正常监控执行成功，`issues=0 notifications=0`、活动告警为空；timer
  已恢复 enabled/active，3x-ui 未重启。
- **2026-10-28 02:39:16**：新 source 的一次 `status=test` 事件被 AlertBridge
  接收，飞书渠道最终记录为 `sent`、HTTP 200、attempts=1；用户随后确认收到。

测试事件记录 ID：`068815407749b90c8bd54ac7b437eda5`。历史事件及其旧 source
未删除；清理本机孤立状态不会回写、删除或自动关闭 AlertBridge 历史 incident。

这些证据验证了监控正常执行与通知链路，未通过人为停节点验证完整故障/恢复流程，
也不表示用户侧代理连通性或长期 SLA 已验收。

## 备份与后续运维

本次收尾前的备份保存在 **JTTI**：

```text
/root/threexui-monitor-cleanup-20261028-023903/
  alertbridge.json.before
  monitor.json.before
  state.json.before
```

备份目录为 `0700`，文件为 `0600`；通知备份含密钥，只保留在服务器管理员目录。
该备份用于回退本次通知标识和状态修改，不是完整 3x-ui 或系统备份。
恢复备份会重新带回旧 source 和 QQG-US 状态，须在明确回滚时执行。

日常启停和日志命令见 [运维手顺](operator-runbook.md#8-日常检查)。再次迁移时，
先停旧调度并备份；更新实例名、标题、source 和 server 标签；核对新数据库节点
ID 后再处理状态，避免不同节点 ID 的状态混用。修改状态须持有监控锁，完成后
验证正常检查及测试通知的最终投递。
