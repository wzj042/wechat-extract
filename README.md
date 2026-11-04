# wechat-extract

Windows 优先的微信 4.x 本地数据提取工具：提取数据库密钥、解密数据库、跨分片读取消息、按消息身份导出本地媒体，并以 MCP 服务提供同一套读取能力。

首次使用先看 [工作流：从微信本地数据到消息与图片](docs/workflow.md)。它按“提取数据库密钥 → 解密数据库 → 读取消息 → 定位并解密图片 → MCP 接入”的顺序，说明各步骤的作用、输入、输出和命令，并区分数据库密钥与图片密钥。

需要 Python 3.11 或更新版本。数据库解密与读取不依赖平台；进程密钥提取只支持 Windows。只读取已同步到本机的数据，不触发微信下载，也不写入微信。

当前版本完成独立项目抽离，保留现有读取契约；接口统一、调用方游标与代际缓存仍属后续重构，尚未实现。抽离范围、验证结果和后续边界见 [抽离状态](docs/extraction-status.md)。

当前目标是私有仓库托管和有限共享，提交前重点检查敏感数据，见 [私有共享检查](docs/private-sharing.md)。第三方来源说明仍保留，当前没有覆盖整个项目的开源许可证；此前的 [公开发布评估](docs/publication-readiness.md) 仅作为将来公开发布时的参考。

## 安装与验证

```powershell
uv sync --group dev
uv run pytest -q
uv run wechat-extract --help
uv run wechat-image-decrypt --help
uv run wechat-mcp --help
```

默认测试只用合成数据，不访问微信进程或真实聊天；临时目录固定在仓库内的 `.test-tmp/`（由 `tests/conftest.py` 设置，显式传 `--basetemp` 时以参数为准）。真实媒体测试默认跳过，须显式设置 `WECHAT_RUN_MEDIA_TESTS=1` 并提供数据，变量说明见 `tests/test_extract_media.py`。

下文的 `<账号目录>` 指微信 4.x 的账号数据目录（形如 `.../xwechat_files/<账号>`，含 `db_storage` 与 `msg/attach`）；`<明文账号目录>` 指解密输出中的单个账号目录（`output/databases/<账号>`）。

## 数据库解密与监听

已有密钥时，将 64 位十六进制内容保存到 `data/db-key.txt`，然后运行：

```powershell
uv run wechat-extract decrypt --key-file data/db-key.txt --input "<账号目录>/db_storage" --output output
uv run wechat-extract watch --key-file data/db-key.txt --input "<账号目录>/db_storage" --output output --interval 2
```

`decrypt` 执行一次；`watch` 持续检查变化，`--once` 只跑一轮。解密会处理已提交的 WAL，以读取仍在 WAL 中的新消息。密钥也可用 `--key` 或环境变量 `WECHAT_DB_KEY` 提供。

命令行将空输入、部分失败作为失败退出。监听发布新明文库前检查旧输出的 WAL、SHM、journal，存在冲突时保留旧输出并报告失败；请关闭占用输出库的写入程序后重试。

数据库 passphrase 可结合各库 salt 派生密钥；某一数据库的原始加密密钥则不能直接用于所有数据库。解密器校验第一页以识别两种输入。

Windows 尚无密钥时，从运行中的微信进程提取：

```powershell
uv run python tools/extract_windows_key.py --help
uv run python tools/extract_windows_key.py --db-dir "<账号目录>/db_storage" --out data/db-key.txt
```

结果写入指定文件，可直接用作上面的 `--key-file`。

## 图片解密

```powershell
uv run wechat-image-decrypt --account-dir "<账号目录>" --chat example@chatroom --out output/images
uv run wechat-image-decrypt --wxid-dir "<账号目录名>" --chat example@chatroom --out output/images
```

复用本机 `kvcomm` 信息推导图片密钥，处理 V2 `.dat`。`--account-dir` 指定真实存储位置，`--userprofile` 指定 `kvcomm` 所在的用户主目录；显式目录无效时不会回退到其他账号。WXGF 转为 JPEG 需要可用的 `ffmpeg`。`--keys` 会显式显示图片密钥，仅在确实需要查看时使用。

输出按账号来源、会话、月份隔离；文件修改时间不是消息时间。默认按头部 AES 长度为密文长度解释（`ciphertext`），`--aes-size-mode plaintext` 显式使用明文长度变体，两种模式均保留中间未加密数据段。

## 跨分片读取与图片定位

```powershell
uv run wechat-extract messages --account-dir "<明文账号目录>" --chat example@chatroom --limit 100
uv run wechat-extract messages --account-dir "<明文账号目录>" --chat example@chatroom --local-id 7 --shard message_1.db
uv run wechat-extract image-path --account-dir "<明文账号目录>" --source-account-dir "<账号目录>" --chat example@chatroom --local-id 7 --shard message_1.db
```

请从读取结果原样传回 `shard`，它可能是 `message/message_1.db` 等相对路径。列表按 `sort_seq` 合并分页，保留相同时间/编号的不同分片消息；单条查询遇到重复 `local_id` 且没有分片时报告歧义。发送者只查该分片的 `Name2Id`。

图片定位限制在所选消息的账号与会话目录，返回实际文件、档位及完整消息身份；多候选拒绝猜测，未缓存明确报告缺失。此命令只定位文件，不触发微信下载。路径依据与限制见 [存储路径与分片身份](docs/storage-and-shards.md)。

## 按消息导出媒体

```powershell
uv run wechat-extract export-media --account-dir "<明文账号目录>" --source-account-dir "<账号目录>" --chat example@chatroom --local-id 7 --shard message_1.db --output output/media
```

`--kind auto` 默认识别图片、语音、视频和文件消息。图片可用 `--tier original|mid|thumb|best` 选择档位；明确档位不会降级，`best` 选中较高档位后校验失败也不会悄悄改用缩略图。合并转发中的单个文件需显式指定 `--kind record-file --data-index 0 --filename "示例.pdf"`。

输出按账号来源、分片、消息表和消息编号隔离；合并转发另带 `data_index`，避免相同编号、相同文件名覆盖。图片先稳定读取、解密，再通过 Pillow 完整解码校验，最后原子写出；WXGF 转换需要 `ffmpeg`。语音保留原始 SILK，只校验头部；视频和普通文件仅验证读取稳定性。

图片默认从本机 `kvcomm` 推导密钥；也可传 `--image-key-file data/image-keys.json`，JSON 字段为 `aes_key`（16 位 ASCII 或 32 位 hex）与 `xor_key`（0–255）。DAT 长度语义通过 `--aes-size-mode` 显式选择。多候选、未缓存、损坏和解密失败分别返回状态；成功退出码为 0，其余为非零。有限真实样本的结果见 [媒体验证记录](docs/media-validation.md)。

## MCP 服务

```powershell
uv run wechat-mcp --config data/mcp-config.json
```

可将 `examples/mcp-live.json` 或 `examples/mcp-snapshot.json` 复制到 `data/mcp-config.json`，再填写自己的路径；配置内相对路径相对于配置文件所在目录。示例不包含密钥，不会自动发现真实账号。

默认 stdio；`--transport http --port <端口>` 改用 Streamable HTTP，默认绑定本机回环地址（`--host` 可修改），需要 Bearer token。共 23 个工具，配置、传输与工具清单见 [MCP 服务](docs/mcp.md)。

## 文档

- [工作流与作用](docs/workflow.md)：从取密钥到消息读取、图片解密及 MCP 接入
- [目标设计](docs/design.md)：定位、范围与后续改造方向（尚未全部实现）
- [抽离状态](docs/extraction-status.md)：当前交付范围与验证
- [公开发布评估](docs/publication-readiness.md)：来源授权、隐私检查与待处理项
- [私有共享检查](docs/private-sharing.md)：当前共享范围、敏感内容与历史处理
- [MCP 服务](docs/mcp.md)：配置、传输与工具
- [存储路径与分片身份](docs/storage-and-shards.md)
- [消息类型定义](docs/message-types.md)
- [媒体验证记录](docs/media-validation.md)
- [来源与许可](docs/sources.md)

## 本地数据与隐私

密钥、数据库、解密输出和导出的媒体都不得提交到 Git。`.gitignore` 已忽略 `data/`、`output/`、`*.db`、`db-key.txt`、`*keys*.json`、`*passphrase*`、`.env` 等；真实数据只放在 `data/` 或 `output/`，不要把个人配置或聊天样本加入 Git。媒体导出与 MCP 失败时不回显密钥或消息正文。
