# astrbot_plugin_github_repo_watch

监控多个 GitHub 仓库的以下事件，并主动推送到指定 QQ 会话：

- Commit / Push 变化
- Release 发布
- CHANGELOG 新增内容（如果仓库存在且配置启用）

## 特点

- 默认目标配置已简化，通常只需要填写会话 `UMO`
- 支持在当前会话直接快捷订阅仓库
- 支持多仓库
- 支持每个仓库单独开关 commit、release、CHANGELOG
- 采用后台轮询 GitHub REST API，无需额外部署 webhook

## 安装依赖

```powershell
python -m pip install -r requirements.txt
```

## 最简单的使用方式

1. 在想接收通知的 QQ 会话里执行 `/ghwatchumo`
2. 把这个 `UMO` 填到插件配置的 `default_targets`
3. 在同一个会话里执行：

```text
/ghwatchsub owner/repo
```

例如：

```text
/ghwatchsub Whereis-Alice/astrbot_plugin_github_repo_watch
```

这样当前会话就会开始接收这个仓库的更新，并自动补进默认推送会话列表。

## 配置说明

### GitHub Token

在插件配置里填写 `github_token`。

- 公共仓库不填也能用，但 API 额度更低
- 私有仓库建议使用具备读取权限的 Token

### 默认推送会话

`default_targets` 里推荐直接填 `umo`，例如：

```text
default:GroupMessage:1091576468
```

使用 `/ghwatchsub owner/repo` 时，插件也会自动把当前会话的 `UMO` 补进这个默认推送列表，方便直接在聊天里完成订阅。

### 仓库列表

在 `repositories` 中可手动添加多个仓库，仓库名使用 `owner/repo` 格式。

如果想指定某个仓库只发到特定会话，可填写 `target_umos`，每行一个 UMO。

### 调试模式

建议 Linux 服务器首次联调时先打开 `debug_mode`。

开启后，AstrBot 日志里会额外输出：

- GitHub API 请求地址、状态码、剩余额度
- 每个仓库本轮检查分支、状态键、新提交数
- CHANGELOG 查找路径、命中情况、增量长度
- 推送目标 UMO、发送返回结果
- HTTP 异常摘要和完整异常栈

## 指令

- `/ghwatchumo`
  查看当前会话 UMO

- `/ghwatchstatus`
  查看插件当前配置概况与后台任务状态

- `/ghwatchtest`
  向当前会话发送测试通知

- `/ghwatchcheck`
  立即执行一次检查。成功时不回会话消息；如有异常会返回错误，详细日志写后台

- `/ghwatchsub owner/repo`
  将当前会话订阅到一个仓库，并自动补进默认推送会话列表

- `/ghwatchunsub owner/repo`
  取消当前会话对一个仓库的订阅

- `/ghwatchsubs`
  查看当前会话已订阅的仓库

## CHANGELOG 检测逻辑

插件会按仓库配置的 `changelog_paths` 顺序查找文件，例如：

- `CHANGELOG.md`
- `CHANGELOG`
- `docs/CHANGELOG.md`

检测到文件后，会保存上一次内容快照，并在后续检查中提取新增片段附在通知中。

## 注意事项

- `qq_official` 平台不适合用这个插件的 UMO 主动推送方式
- Linux 服务器部署时建议先安装依赖：`pip install -r requirements.txt`
