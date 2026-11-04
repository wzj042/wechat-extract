# 存储路径与分片身份

## 已确认的依据

- 2026-10-17 实测记录使用 `%USERPROFILE%/xwechat_files/<账号>/msg/attach/<会话md5>/<YYYY-MM>/`，旧 `Documents/WeChat Files/.../FileStorage/MsgAttach` 配置指向空目录。本轮依据历史记录和现存源码补充实现，未重新扫描真实账号。
- 转发记录文件位于 `Rec/<16hex资源ID>/F/<data_index>/<原文件名>`；资源 ID 来自该消息 `packed_info_data` 的 protobuf field 9 → field 1；`data_index` 从 0 开始。
- [wechatauto-replica #20](https://github.com/fanyuantaier/wechatauto-replica/issues/20)：同会话跨分片，`local_id` 可重复，媒体回查必须传递分片。讨论早期“尚未下载”的诊断并不能替代之后的分片修复。
- [#34](https://github.com/fanyuantaier/wechatauto-replica/issues/34)：`real_sender_id` 属于当前消息分片的 `Name2Id`，不能查资源库的另一张映射表，也不能将 2 固定视为自己。
- [#32](https://github.com/fanyuantaier/wechatauto-replica/issues/32)：不得去掉 `D:\` 的尾斜线使其变成依赖当前目录的 `D:`。

参考源码固定在 `d322c4a9b1124228820bab363e5676016f188fc6`（2026-10-17），主要核对 `wechatauto/db.py`、`wechatauto/media.py` 以及对应的分片、发送者、盘根测试。没有运行上游的界面操作或进程补丁。

## 身份和排序

`MessageReader(decrypted_account_dir)` 接受**单个已解密账号目录**。每条记录保留原始列，并附加：

| 字段 | 含义 |
| --- | --- |
| `account` | 明文账号目录绝对路径，标识这份数据源 |
| `shard` | 数据库相对于账号目录的路径，不能只存文件名 |
| `conversation` / `table` | 会话 username / 实际 `Msg_<md5>` 表 |
| `local_id` | 该分片会话表中的编号 |
| `sender_username` | 当前分片 `Name2Id` 中解析出的身份，未知为 `None` |
| `content_text` / `message_type` | 解压后的正文及类型定义 |

稳定身份为 `(account, shard, table, local_id)`。按 `sort_seq` 排序；相同时以分片路径和 local_id 稳定排序，不推断相同时间内的实际发送先后，也不把同号消息去重。每片只取分页所需数量，再合并分页。

任一参与查询的分片损坏都会报 `ShardReadError`。明确指定分片时只读取该片；未指定分片且多条命中时抛 `AmbiguousMessageError`。这里比上游兼容旧调用时“警告后选最新”的行为严格。

## 媒体接口

```python
from wechat_extract.messages import MessageReader
from wechat_extract.media import resolve_image, resolve_record_file

reader = MessageReader("output/databases/account")
row = reader.get_message("example@chatroom", 7, shard="message_1.db")
image = resolve_image(
    reader.account_dir, "D:/xwechat_files/account",
    row["conversation"], row["local_id"], shard=row["shard"], tier="best",
)
record_file = resolve_record_file(
    reader.account_dir, "D:/xwechat_files/account",
    "example@chatroom", 12, shard="message_2.db",
    data_index=0, filename="report.pdf",
)
```

返回值包含 `status`、`path`、`reason`、`identity`、`output_relative_path`，图片另有 `tier`。`output_relative_path` 按账号源、分片、表、local_id 隔离，可用于调用者保存结果；定位函数自身不复制或解密。

图片候选来自**所选消息自身**的直接 XML 图像字段或明确 protobuf 资源字段，仅在该会话的月份目录内匹配。考虑延迟下载，月份不作为硬性过滤；不同月份存在多份候选时抛 `AmbiguousMediaError`。`original/mid/thumb` 表示文件命名档位，不保证高清文件已下载完整。

资源索引关联使用 `ChatName2Id` 与 `MessageResourceInfo`，同时核对会话、local_id、类型及可用的 server_id/create_time。没有 server_id 时，跨消息分片重复的 local_id 不允许用于资源兜底。多资源候选报歧义，不按 local_id 单独取首条。真实样本中 XML MD5 与资源库 file-id 不同，资源库 `packed_info` 的 field2 → field1 可给出实际文件 ID。

`hardlink.db` 仅按明确 MD5 查找，结合其目录映射和大小约束。`read_voice` 遍历 media 分片，以各分片自己的 Name2Id 和消息服务端编号读取 VoiceInfo；多个候选拒绝选择。`resolve_video`、`resolve_file` 定位已缓存附件，不触发下载。没有明确资源 ID、未知布局或媒体未缓存时返回缺失，不能据此断言消息不存在。

`export-media` 接通定位、解密与导出，输出保留完整消息身份。图片使用 Pillow verify 加实际帧解码，DAT 的 JPEG/PNG 可裁去不超过 32 字节的容器尾部后重新校验。语音仅 SILK 头校验，普通文件与视频仅稳定复制；这些成功状态不等于媒体内容已经完整解码验证。所有输出经临时文件原子替换，失败不覆盖已有成果。

## 路径和图片格式边界

`paths.resolve_account_dir()` 可接收账号目录、`db_storage`、账号容器或存储根，有限层级发现，多个账号必须指定。`--account-dir` 不自动读取配置/注册表；自定义位置由调用者明确传入。支持识别包含 4.x `db_storage` 或 `msg/attach` 的目录，不声明支持旧 3.x 数据解密。

旧实现的实测记录表明头部 `aes_size` 为密文长度；参考项目按填充前明文长度解释。整倍数时两者无法仅凭长度消歧，因此默认 `ciphertext` 保持原实现，显式 `plaintext` 采用 `floor(aes_size/16)*16+16` 密文长度并检查 PKCS7 和解填充长度。两者均拼接 AES 解密段、中间明文段、尾部 XOR 段。

原先真实媒体测试中的单片读取、local_id 字典和全账号文件名搜索已移除。现在仅在 `WECHAT_RUN_MEDIA_TESTS=1` 且显式指定 `WECHAT_TEST_ACCOUNT_DIR`、`WECHAT_TEST_SOURCE_ACCOUNT_DIR`、`WECHAT_TEST_CHAT` 时验证所选会话；默认测试只用合成数据。
