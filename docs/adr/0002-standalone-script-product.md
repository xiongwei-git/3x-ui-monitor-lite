# ADR 0002：独立、无 UI 的一键部署监控产品

- 状态：Accepted
- 日期：2026-07-30

## 背景

ThreeXUIScriptMonitor 需要成为一套独立产品：无 UI、低资源占用、只读访问本机
3x-ui 数据、可在已安装 3x-ui 的主机上单独部署。它没有其他监控或运维系统的
运行时、配置或发布依赖。

目标部署系统为使用 systemd 的主流 Linux 发行版，包括 Ubuntu、Debian、CentOS、
Fedora、RHEL 系、Alibaba Cloud Linux；若 macOS 上已经存在可验证的 3x-ui
安装，则提供实验性的 launchd 调度支持。Windows 不在范围内。3x-ui 官方当前未把
macOS 列为受支持系统，因此不能承诺在 macOS 上安装或维护 3x-ui 本身。

## 决策

1. 保留 Python 监控核心，使用一个 POSIX shell 安装器完成探测、依赖检查、配置、
   安装和调度。第一阶段不重写为常驻服务或 Web 应用。
2. 安装器必须先探测 3x-ui 可执行文件和本地 SQLite 数据库，并验证所需表结构。
   找不到或不兼容时，在创建任何本项目文件前退出。PostgreSQL 数据源暂不支持。
3. Linux 使用 systemd oneshot + timer；macOS 使用 LaunchDaemon。运行周期来自
   `runtime.interval_seconds`，范围为 60–86400 秒。
4. 配置继续使用 JSON `version: 1`，新增三个稳定分组：
   - `instance`：实例名称、位置；
   - `runtime`：周期、服务管理器、3x-ui 可执行文件；
   - `notifications`：开关、通知器、模板。
5. 配置使用严格字段校验，未知字段直接报错。主配置和通知密钥配置都必须为 root
   所有，且不得对 group/other 可写；包含密钥的文件必须为 `0600`。
6. 通知器使用可配置的 `provider`。默认通道为 AlertBridge，并支持两种接入模式：
   - `events`：HMAC `/api/v1/events`，支持 `firing/resolved`、幂等和去重；
   - `notifications`：Bearer `/api/v1/notifications`，只发送一次性通知，不表达
     AlertBridge 活跃故障生命周期。
   另支持 `feishu_webhook`，直接调用飞书或 Lark 自定义机器人 Webhook；该方式保留
   监控本地的异常去重和恢复通知，但不提供网关侧路由、投递历史和事件生命周期。
7. 标题和正文使用有限占位符模板。模板不能执行表达式，未知占位符拒绝加载。
8. 对外部署产物为带版本和 SHA-256 的单文件自解压安装器。用户可以用一条 shell
   命令下载后执行；不采用 Docker。
9. 产品安装路径统一使用 `threexui-script-monitor` 命名。
10. 安装器支持 `--config` 读取版本化的完整标准部署配置。该配置是单次安装的唯一
    配置来源，包含调度、3x-ui 路径、监控阈值、模板和通知通道；安装时拆分为无
    密钥运行配置和独立密钥配置。原有逐项命令行参数保持兼容，但两种方式不混用。

## 配置契约

标题模板允许：

- `{instance_name}`
- `{instance_location}`
- `{event_title}`
- `{severity}`

正文模板允许：

- 标题模板的全部字段；
- `{event_status}`
- `{event_message}`
- `{event_details}`

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

## 备选方案

- Docker：需要挂载 3x-ui 数据库和宿主机服务状态，增加权限面，拒绝。
- 纯 shell 监控核心：JSON、SQLite、IPv6、HMAC 和状态原子写入难以可靠维护，拒绝。
- 立即改写静态 Go 二进制：能消除 Python 运行时，但重写风险高于当前阶段收益；
  若跨平台 Python 依赖成为真实部署阻塞，再单独决策。
- 把功能并入另一套 Web 监控：违背独立、无 UI 的产品定位，拒绝。

## 影响

- Linux 安装器可以按发行版补齐 Python 3；macOS 若没有可用 Python 3.9+，第一阶段
  会在零安装状态下退出并给出明确原因，不自动安装 Homebrew。
- Bearer 模式的“恢复通知”只是另一条一次性消息；需要 AlertBridge 故障闭环时必须
  使用默认 HMAC 模式。
- 飞书直连减少了部署依赖，但绑定单个机器人所在群；需要多渠道、集中密钥管理或
  投递审计时应使用 AlertBridge。
- 自动探测不能替代显式覆盖。高级用户仍可通过受校验的安装参数指定 3x-ui
  可执行文件和 SQLite 路径。
