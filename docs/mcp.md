# MCP 服务

运行 `uv sync --group dev` 后可用 `uv run wechat-mcp --config data/mcp-config.json` 启动 stdio 服务。沿用 MCP SDK v1 的传输协议；依赖固定在 `mcp>=1.20,<2`。配置文件路径也可用环境变量 `WECHAT_MCP_CONFIG` 指定。

## 传输

默认 stdio：stdout 只承载 JSON-RPC，底层日志不写 stdout。也可改用 Streamable HTTP：

```powershell
$env:WECHAT_MCP_TOKEN = "<至少 16 个字符的随机串>"
uv run wechat-mcp --config data/mcp-config.json --transport http --port <端口>
```

HTTP 模式必须显式给出 `--port`。默认只绑定 `127.0.0.1`（`--host` 可改）；每个请求都要带 `Authorization: Bearer <token>`，token 从 `--token-env` 指定的环境变量读取（默认 `WECHAT_MCP_TOKEN`），不足 16 个字符时拒绝启动。Host 头只放行 `127.0.0.1`、`localhost` 与 `host.docker.internal`。

## 配置

```json
{
  "mode": "live",
  "db_dir": "D:/xwechat_files/example/db_storage",
  "source_account_dir": "D:/xwechat_files/example",
  "account_dir": "mcp-cache/account",
  "output_dir": "../output/mcp-media",
  "key_file": "db-key.txt",
  "aes_size_mode": "plaintext"
}
```

所有配置路径相对配置文件所在目录。配置放在被忽略的 `data/` 下。`key_file` 接受 64 位十六进制文本，或现有 `passphrase.json` 的 `passphrase_hex`。也兼容旧 `keys_file` 按相对库路径保存的 `enc_key` 映射，但新建/重建库可能需要新密钥，优先用全局 passphrase。

`live` 使用独立空目录作为明文缓存，按调用范围检查主库、WAL、SHM、journal 与密钥变化，再执行稳定快照和原子发布。跨进程锁避免多客户端同时写缓存；失败不返回旧结果。源库消失时，对应缓存移至缓存目录外隔离，不再参与查询。不会读取进程内存或触发微信下载。

已有明文数据可用 `mode: snapshot`，无需密钥，也不会刷新；仍须显式配置账号目录、原附件目录和输出目录。该模式不能声称数据实时。

## 工具

共 23 个工具，都不写入微信数据：live 模式的刷新只写明文缓存目录，导出类工具只写输出目录。

| 类别 | 工具 |
| --- | --- |
| 会话与联系人 | `get_recent_sessions`、`get_contacts`、`get_contact_tags`、`get_tag_members`、`get_group_members` |
| 消息 | `get_chat_history`、`search_messages`、`get_message_detail`、`get_chat_images`、`get_voice_messages` |
| 增量与事件 | `get_new_messages`、`get_new_events`、`get_recent_events`、`get_change_marker` |
| 结构化解析 | `decode_transfer`、`decode_refer`、`decode_location`、`decode_record_item` |
| 媒体 | `decode_image`、`decode_voice`、`decode_file_message`、`export_media` |
| 状态 | `get_status` |

其中 16 个沿用旧 WeChat MCP 的工具名及原参数。媒体及单消息详情工具新增可选 `shard`，优先从历史返回值原样传入；同会话 local_id 跨片重复时拒绝猜测。保留 `create_time` 消歧能力。识图、语音转文字、网页抓取和 Office 文档读取不属于本服务，由调用方负责。

工具返回 MCP 文本块，内容统一为 JSON 文本，不逐字复刻旧的人类可读排版。依赖旧文本正则的调用方需要适配。查询返回消息身份、发送者和类型；失败返回明确 status/reason，不回显底层异常中的密钥或正文。会话/标签允许唯一模糊匹配，多结果拒绝取首条。

历史和搜索分页上限 500，offset 上限 10000；扫描预算超过 100000 条时须缩小范围。按过滤后的结果跨分片合并，普通最新消息查询不会为取一条而解压整个群历史。`msg_types=file` 现在精准匹配文件；需要所有应用消息时用 `app`。

`get_new_events(include_self=false)` 与 `get_recent_events(chat_name, limit, since, include_self)` 只返回与“我”相关的互动事件（`src/wechat_extract/events.py`）：

- `pat_me` / `pat`：49/62 消息，判断依据是 `patinfo.pattedusername`。
- `mention_me` / `mention_all`：判断依据是 `source` 列的 `atuserlist`。读消息时现在会额外输出 `source_text`。
- `quote_me`：49/57 消息。群聊中 `refermsg.fromusr` 是群 ID，所以按 `svrid` 回查被引用消息的发送者。

“我”的 wxid 优先取配置 `self_username`，没有配置时从 `source_account_dir` 的目录名推导（去掉 `_数字` 后缀）。事件带 `from_self` 和 `chat_display_name` 字段。`get_new_events` 与 `get_new_messages` 各自维护基线。

`get_new_messages` 首次建立进程内身份基线，随后每次最多返回 100 条新增消息；重启后重新建立基线。它不是可靠持久化消息队列，与旧版“会话时间变化/未读摘要”语义不同。`get_change_marker` 只读取原始库与 WAL 的大小和修改时间，不解密、不刷新缓存，可作为调用 `get_new_messages` 前的廉价预检。

图片走已验证导出链；`decode_voice` 使用 silk-python 本地解码为 WAV。转账、引用、位置返回所选消息的 XML 字段；转发条目保留 item_index，文件条目尝试定位本地附件。

动画表情（47）是唯一会联网的媒体（`src/wechat_extract/emoji.py`）。本地 `cache/<月>/Emoticon` 副本用未知密钥加密，所以不读它，改为按消息 XML 下载：先取 `cdnurl` 的原件；失败或 md5 不符时，再取 `encrypturl`，用 AES-128-CBC 解密（key = iv = `aeskey`）。只接受 http/https 和白名单主机，默认 `*.qq.com`、`*.qpic.cn`，可用配置 `emoji_cdn_hosts` 覆盖；重定向目标也要在白名单内。超时 30s，上限 20MB。下载结果的 md5（以及 len）必须与 XML 一致，再完整解码，只接受 gif/png/jpg/webp。原件按 md5 缓存在 `<output_dir>/.emoji-cache/`，命中就不再下载。

- `export_media(kind=auto)` 把 47 映射为 `emoji`，原件写到该消息目录的 `emoji.<ext>`。返回 `kind=emoji`、`preview_path`、`animated`、`frames`、`preview_frames`、`duration_ms`（仅动图）、`md5`、`source`（cache/cdn/encrypted_cdn）。
- 动图另生成 `preview.png`：均匀抽最多 6 帧，按时间顺序排成最多 3 列的白底网格。静图的 `preview_path` 就是原件。配置了 `agent_path_map` 时，同时给出 `agent_path` 和 `preview_agent_path`。
- 对表情消息，`decode_image` 自动走表情导出。
- 失败用 `download_failed`/`decrypt_failed`/`incomplete`/`unsupported` 等 status 表示，reason 不含 aeskey、URL 或正文。

## 验证

合成测试覆盖查询、跨片身份、缓存失效和隔离、工具注册、本地 WAV 解码及真实 stdio 握手。特别验证 live 刷新日志不污染 stdout JSON-RPC。
