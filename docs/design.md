# 目标设计

> 这是目标设计，尚未全部实现；现状以 [README](../README.md) 与 [MCP 服务](mcp.md) 为准。

## 定位

Windows 优先的微信 4.x 本地数据提取工具：提取数据库密钥、解密数据库、按消息身份定位和导出媒体，并把消息规整为稳定的 JSON 结构供 AI 读取。CLI 是核心，所有命令支持 `--json`；MCP 是同一套能力的薄封装，默认 stdio，HTTP 可选。目标是 agent 拉取仓库后按文档即可安装、自检和运行。

判断一项能力是否属于本项目，只看它是不是在把微信本地存储的数据读出来、解释清楚。读出之后的再加工（识图、语音转文字、抓网页、读 Office 文档、报告调用方的运行参数）由调用方负责。

## 范围

| 范围内 | 范围外 |
| --- | --- |
| 从运行中的 Windows 微信进程提取数据库密钥并验证 | UI 自动化、发送消息及任何写入微信的操作 |
| SQLCipher 4 / WCDB 解密，含已提交 WAL；一次性解密与增量监听 | 调用 LLM 或视觉模型 |
| 跨分片读取会话、联系人、标签、群成员和消息 | 抓取网页正文、提取 Office 文档内容 |
| 会话与联系人的精确解析（username 与显示名互查） | 读取或报告调用方的运行参数 |
| 规整消息类型、appmsg 卡片、互动与系统事件 | 为某个调用方定制的文本渲染格式 |
| 按消息身份定位、导出本地媒体：图片 dat、语音 SILK→WAV、视频、文件、合并转发条目 | 触发微信下载 |
| 动画表情按消息 XML 从 CDN 取回原件（调用方显式开启网络） | |

密钥提取只做 Windows；解密与读取本身不依赖平台，但首期只在 Windows 上验证。

## 设计原则

调用方往往已把现有字段名、`shard:local_id:create_time` 身份串和导出目录布局写进持久化存储或判断逻辑。工具同名而形状变化时，它们大多不会报错，只会静默失效。因此：

1. **契约先于整洁。** 保留的工具沿用现名和参数名，消息行沿用现有平铺字段名。有意的不兼容变化只有四处：列表类工具改为返回外壳对象；原始 XML 改为按需返回；删除 `account` 字段；删除按备注取名的 `sender` 字段。其余都是新增字段。
2. **版本可校验。** 所有工具返回单个 JSON 对象，成功与失败共用一个外壳，并带 `schema_version`，让调用方能发现形状变化。工具参数类型放宽、在内部校验，避免 FastMCP 的参数校验错误绕过外壳。
3. **身份格式冻结。** `shard` 是解密目录内的相对 posix 路径（如 `message/message_0.db`），`create_time` 是整数秒，两者逐字不变；导出目录布局同样冻结。
4. **结构化字段为主。** 链接、@、引用、卡片、系统事件都给成字段，默认输出可以直接交给模型阅读；原始 XML 只在 `include_raw=true` 时返回。
5. **增量由调用方持有游标。** 服务端不保存消费进度；调用方处理完一批再保存游标，出错可以重读。
6. **读请求不排队等刷新。** 刷新按代际发布明文缓存，查询在当前代际上执行；网络下载与大文件拷贝不占全局锁。工具改为异步并在线程中执行：FastMCP 在事件循环里直接调用同步工具，一个慢请求会卡住所有请求。见“缓存与并发”。
7. **网络与路径显式。** 只有调用方明确开启时才联网；路径映射是通用配置，不绑定某种部署。

## 消息结构（v1）

列表、详情与增量共用同一种消息行：

| 字段 | 说明 | 相对现状 |
| --- | --- | --- |
| `conversation`、`shard`、`table`、`local_id`、`server_id`、`create_time`、`sort_seq` | 消息身份与排序；`shard`、`create_time` 格式冻结 | 不变 |
| `ref` | 紧凑消息引用；按消息操作的工具都接受它，可代替四个身份参数 | 新增 |
| `sender_username`、`sender_display` | 显示名取群名片 > 昵称 > username，不用备注；同群重名时 `sender_display_duplicate=true` | 新增重名标记 |
| `sender` | 按备注 > 昵称取的显示名 | 删除，与 `sender_display` 的规则冲突 |
| `local_type` | 数据库原始类型值 | 不变 |
| `message_type` | `raw_type`、`base_type`、`subtype`、`label`、`category` 等 | 不变 |
| `summary` | 单行摘要，格式对齐微信界面：文本消息为正文，其他为“[类型] 标题”；不截断，调用方会用全等或子串比对来识别自己发出的消息 | 改为固定单行，附加说明移到 `links`、`card` |
| `text` | 文本消息去掉群前缀后的正文；引用消息的回复正文 | 新增 |
| `card` | 结构化卡片：标题、描述、url；视频号作者、文案、封面 url；引用的回复正文与被引消息身份；小程序真实链接等 | 补全字段，修正视频号与小程序取错的值 |
| `mentions` | 被 @ 的 username，`@所有人` 单独标记 | 新增，取代读 `source_text` |
| `links` | 卡片与被引用卡片里的链接 | 新增 |
| `events` | 内嵌事件：`kind`、`targets_me`、`actor`、`target`、`from_self` | 新增 |
| `media` | 可导出的媒体类型、本机缓存状态、md5、时长 | 新增 |
| `content_text`、`source_text` | 原始 XML | 改为 `include_raw=true` 才返回 |
| `account` | 明文缓存目录的绝对路径 | 删除 |

表中未列出的其他数据库原始列不再输出，`include_raw=true` 时随原始 XML 一并返回。

`ref` 格式（初步）：`<md5(会话)[:8]>.<分片码>.<local_id>.<create_time>`，分片码如 `m0`（`message/message_0.db`）、`b0`，其他分片用 `~` 加 base64。`server_id` 可能被补写，因此不放进 `ref`。调用时同时给了 `chat_name` 的，服务端核对两者一致；前缀碰撞时返回 `ambiguous`。

响应外壳：`{"status": "ok", "schema_version": 1, "items": [...], "next_cursor": "...", "has_more": false}`。失败时 `status` 为 `missing`、`ambiguous`、`network_disabled` 等，并在 `reason` 里给出具体原因，不再用笼统的 `invalid_or_ambiguous_request`。

## MCP 工具

| 工具 | 变化 |
| --- | --- |
| `get_chat_history` | 时间窗改为半开区间 [start, end)，接受 ISO 或 epoch 秒，只给日期的 `end` 解释为次日零点；时区可配置（现状固定东八区）；支持 `oldest_first`；用 `cursor` 做键集分页，按 (create_time, sort_seq, shard, table, local_id) 全序，游标绑定过滤条件，`offset` 仅为兼容保留；`msg_types` 吸收 `get_chat_images`、`get_voice_messages` |
| `search_messages` | 在反转义后的正文和 `card.title`、`card.description` 上匹配，不再命中 XML 内部字段；支持时间窗 |
| `resolve_chats`（新增） | 批量精确解析会话。返回 `status`（ok/not_found/ambiguous）、`username`、类型、`display_name`、`remark`、`nick_name`、候选和显示名冲突；重名按全量数据计算；按 contact.db 签名缓存。它是全局查询，只应开放给可信调用方 |
| `get_group_members` | 支持 `member=` 精确查询；返回群名片、昵称、显示名、重名标记，以及不含备注的群名；成员列表仍放在 `members` 键下 |
| `get_message_detail` | 吸收 `decode_transfer`、`decode_refer`、`decode_location`，在 `detail` 下给出转账、引用、位置、合并转发条目；引用附被引消息的完整行；保留现有顶层的 `identity`、`type`、`create_time`、`card`、`text` |
| `export_media` | 吸收 `decode_image`、`decode_voice`、`decode_file_message`、`decode_record_item`；见“媒体导出” |
| `get_updates`（新增） | 取代 `get_new_messages`、`get_new_events`；见“增量” |
| `get_status` | 增加刷新就绪位、微信版本、各工具作用域（会话级/全局）、网络与路径映射开关 |
| `get_recent_sessions`、`get_contacts`、`get_contact_tags`、`get_tag_members`、`get_recent_events`、`get_change_marker` | 沿用 |
| 移出 | `describe_image`、`describe_finder_cover`、`fetch_url`、`read_document`、`transcribe_voice`、`get_runtime_info` |

`chat_name` 只接受精确的 username 或唯一的精确显示名，需要模糊查找时改用 `resolve_chats`。现状在精确匹配失败后会退回唯一子串匹配，调用方因此得回头核对返回的会话是不是自己要的。

## 增量

- 不传游标：只返回当前游标，作为显式基线，不回放之前的消息。
- 传游标：返回之后的新消息（内嵌事件与会话显示名）、`next_cursor` 和 `has_more`；支持 `chats` 过滤与单批上限。调用方处理完再保存游标。
- 游标记录每个“分片 + 消息表”已见的最大 `local_id`，压缩编码，并带账号指纹与版本号；用 `chats` 过滤时，其他表的水位原样带回；`create_time` 早于本批的迟到行标记 `late`。
- 原地更新：撤回如果是插入一条新的系统消息，水位游标天然覆盖；如果是改写原行，就对近期有新行的表，在窗口内比较 (server_id, local_type, 正文哈希) 摘要。补写 `server_id` 价值低，放到后续。需用真实库验证：`local_id` 是否只增不复用（看 sqlite_sequence），以及撤回的实际形态。
- 返回跨会话数据，不放进模型工具表。

现状的问题：消息与事件各有一份进程内游标，同一批消息的正文和事件可能落在不同轮次，“引用我”“拍我”因此丢失；服务重启后基线重建，重启前后到达的消息不再返回；游标在调用时就前进，调用方之后出错，这批消息也随之丢失。

## 媒体导出

- 覆盖类型：
  - 图片：返回实际使用的档位，`tier=original` 时不降级；
  - 表情：原件、抽帧拼图、`animated`、`frames`、`md5`；
  - 语音：WAV，注明采样率；
  - 视频、文件与合并转发条目。
- 导出目录布局冻结为 `<导出根>/<账号键>/<shard>/<table>/<local_id>/<文件>`，调用方据此按会话隔离、校验参考图归属。冻结的是路径段数和 `<shard>/<table>/<local_id>` 这几段；账号键改为 `sha256("wechat-extract:" + 源账号目录名小写)` 的前 16 位，可用配置覆盖，不再随明文缓存目录变化（现状取缓存目录绝对路径的 sha256 前 16 位）。
- 返回宿主机绝对路径和相对导出根的路径；配置通用的 `path_map`（最长前缀匹配，不区分大小写）后，另给映射后的路径 `mapped_path`，取代 `agent_path_map` 与 `agent_path`。
- 表情：本地已缓存的原件直接返回；需要下载时，服务端总开关、主机白名单与每次调用的 `allow_network` 三者都满足才联网，否则返回 `network_disabled`，不静默降级。
- 内容相同时不重写文件：消费方打开着文件时，Windows 上无法替换。
- 下载与拷贝在锁外进行。

## 缓存与并发

- 刷新单飞；每一代是一个新目录，未变化的库用 NTFS 硬链接放进去，最后写一份不可变清单。
- 每次工具调用取一份租约，整次调用固定在同一代上；保留当前代和上一代，回收时遇到被占用的文件留到下一轮。
- 读请求最多等刷新一段时间，超时就在当前代上执行，并在外壳里标 `stale`；`get_updates` 默认等刷新完成；还没有任何一代时返回 `not_ready`。
- 约束：SQLite 在 Windows 上打开库文件时不带 FILE_SHARE_DELETE，文件被打开期间不能替换或删除。非 NTFS 卷由 `doctor` 拒绝。实现前先用合成测试确认：库文件被 SQLite 打开时，`os.link` 仍能成功。

## 目标结构

```text
src/wechat_extract/
  keys/    Windows 取密钥、验证密钥
  crypto/  SQLCipher 解密、WAL 快照、诊断、增量监听
  store/   账号发现、分片、联系人与会话解析、群成员、代际缓存
  model/   消息类型、appmsg 卡片、事件 → 消息行 v1
  media/   定位、图片 dat、语音 SILK、文件与视频、表情（按调用开网）
  cli/     全部命令支持 --json
  mcp/     工具与 CLI 命令一一对应；stdio 默认，HTTP 可选
```

CLI 与 MCP 对照（命令名为草案）：

| CLI | MCP | 说明 |
| --- | --- | --- |
| `doctor` | `get_status` | 自检环境、密钥、数据库、刷新状态与微信版本，不回显密钥 |
| `capabilities` | `get_status` 的一部分 | 工具清单、作用域、开关；可用来生成调用方的工具白名单 |
| `key extract` / `key verify` | — | 仅 CLI，需要访问微信进程 |
| `decrypt` / `watch` | — | 仅 CLI |
| `chats` | `get_recent_sessions` | |
| `resolve <名字…>` | `resolve_chats` | |
| `contacts` / `tags` | `get_contacts`、`get_contact_tags`、`get_tag_members` | |
| `members <chat>` | `get_group_members` | |
| `read <chat>` | `get_chat_history` | |
| `search <keyword>` | `search_messages` | |
| `message <ref>` | `get_message_detail` | |
| `events <chat>` | `get_recent_events` | |
| `updates [--cursor]` | `get_updates` | |
| `changes` | `get_change_marker` | 只读库文件大小与修改时间，不解密 |
| `media export <ref>` | `export_media` | |
| `mcp serve` / `mcp config` | — | 启动服务；生成客户端配置 |
