# 独立抽离状态

2026-11-04：完成独立项目抽离。本阶段保留读取行为，接口重构另行进行；[目标设计](design.md) 是后续方案，不是当前 API 承诺。

## 当前交付

- 独立 Python 项目 `wechat-extract`，包名 `wechat_extract`，版本 `0.1.0`；具备自己的依赖锁、测试和文档。
- 三个安装入口：`wechat-extract`、`wechat-image-decrypt`、`wechat-mcp`；Windows 密钥提取脚本保留在 `tools/extract_windows_key.py`。
- 保留数据库解密与 WAL、增量监听、跨分片消息读取、消息类型与卡片解析、联系人和群成员、互动事件及媒体定位导出。
- MCP 保留 23 个工具，支持 stdio 和带 Bearer 认证的 HTTP。HTTP 必须显式指定端口。
- 移出模型识图、语音转文字、网页抓取、Office 文档解析和调用方运行参数探测；清除对应工具、模块、测试与部署专属提示。
- 仓库只包含源码、合成测试、文档和依赖信息，不携带密钥、真实账号配置、数据库、媒体或旧仓库历史。

工具名、握手服务名和导出提示等与原集成版本的差异已写入当前文档。本版本不直接替换现有部署，调用方迁移需要单独验证。

## 验证

- 合成回归：178 passed、3 skipped、49 subtests passed；跳过的是需显式开启并提供数据的真实媒体测试。
- 测试包含真实子进程 stdio 初始化、工具清单与调用，以及 snapshot/live 两种模式的协议验证。
- 扫描待提交文件，未发现本机身份信息、私有群号或常见凭证格式；配置、缓存、输出与虚拟环境均不进入提交。
- 构建 wheel 与源码包，并核对归档文件清单。

Windows 上若默认 `.test-tmp` 或 `.pytest_cache` 由其他运行身份创建，可用独立短路径验证，不必修改原目录权限：

```powershell
New-Item -ItemType Directory -Path output -Force | Out-Null
uv run pytest -q -p no:cacheprovider --basetemp=output/pt
uv build
```

`--basetemp` 应指向专用测试目录，pytest 会清理该目录。上述测试未访问真实微信进程或聊天数据，也未重启生产服务。

## 后续重构

仍未实现：分层模块目录、统一响应外壳与消息结构、`resolve_chats`、调用方持有的 `get_updates` 游标、键集分页、代际缓存、通用 `path_map`、逐次调用的表情联网许可，以及完整 CLI/MCP 能力对应。

当前仍沿用进程内增量基线、原始消息字段、`agent_path_map` 与表情 CDN 下载行为。不能仅根据目标设计假定默认不联网、重启后可无损续读或响应已带 `schema_version`；实际行为见 [MCP 服务](mcp.md)。

第三方来源与许可记录见 [来源与许可](sources.md)。本次抽离没有新增总许可证，也未发布到远程仓库。
