# Changelog

## v0.2.2

- 新增 `/ghwatchsub owner/repo` 重复订阅提示：当前会话已订阅该仓库时，直接提示“已经订阅过”，不再重复写配置

## v0.2.1

- `/ghwatchcheck` 成功时改为完全静默，不再回复“已执行”或摘要
- 文档补充说明 `/ghwatchsub` 会自动把当前会话补进默认推送会话列表
- 重新整理测试打包内容，避免携带本地 `__pycache__`
- 修复 `/ghwatchsub` 动态写入配置时缺少 `template_list` 模板标记，导致配置面板不同步的问题
- 修复 `/ghwatchsubs` 未统计默认推送会话继承仓库的问题
- 新增启动时自动迁移旧脏配置，自动修复旧版 `default_targets` / `repositories` 的格式和订阅路由字段

## v0.2.0

- 简化推送目标配置，默认仅需填写会话 `UMO`
- 去掉目标名称依赖，仓库路由改为直接使用 `target_umos`
- 新增 `/ghwatchsub owner/repo`，可在当前会话快捷订阅仓库
- `/ghwatchsub` 订阅时会自动把当前会话补进默认推送会话列表
- 新增 `/ghwatchunsub owner/repo`，可在当前会话取消订阅仓库
- 新增 `/ghwatchsubs`，查看当前会话已订阅仓库
- 调整 `/ghwatchtest` 为直接测试当前会话
- 调整 `/ghwatchcheck`，成功时完全静默，不再向会话发送执行结果，只在后台日志记录
- 保留对旧配置字段 `target_names`、旧式目标配置的兼容读取
- 增强调试日志，便于 Linux 服务器排障

## v0.1.0

- 初始版本
- 支持监控 GitHub 仓库的 commit/push 与 release
- 支持可选附带 CHANGELOG 增量
- 支持多仓库与主动推送到 QQ 会话
