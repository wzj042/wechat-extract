# 微信消息类型

`wechat_extract.message_types.classify_message_type(local_type, content=None)`
接收数据库的整数 `local_type` 和可选的已解压、已解码 XML 文本，返回冻结的
`MessageType`。它用于类型识别，不负责读取数据库、解压、渲染或提取真实聊天内容。

## 数据与优先级

- `raw_type` 原样保留输入整数；支持 SQLite 有符号 64 位和无符号 64 位整数。
- `base_type` 是低 32 位，`packed_subtype` 是高 32 位。`49 | (57 << 32)` 为引用消息；
  基础类型 `62` 是视频，而应用子类型 `62` 是拍一拍。
- 对基础类型 49：非零高位优先，其次 `<appmsg>` **直系** `<type>`；
  `subtype` 为采用的值，`xml_subtype` 保留 XML 值，`conflict` 标记两者不一致。
- 没有有效数值子类型时才使用直系 `refermsg`、`type="patmsg"`、`wcpayinfo`
  结构补充判定。引用先于拍一拍，支付提示中转账先于红包。未知非零子类型不被提示覆盖。
  一个明确例外：4/5/68 链接容器无 URL、无直系引用、无数值冲突而
  `appmsg type="patmsg"` 时归为拍一拍，保留原子类型；有 URL 仍按链接判定。
- `source` 是 `base`、`packed`、`xml` 或 `xml_hint`；它描述数值/提示来源，
  文章和系统消息的细化仍可使用 XML。`system_type` 保留顶层 sysmsg 的 type 属性。
- `label` 为中文标签，`category` 为稳定英文分类。未知主类型/未知应用子类型
  使用 `unknown`，不默认为文本；已知 49 且无可识别子类型保留 `app`。
- XML 畸形时保持数值分类，拒绝 DTD/实体声明；CDATA 中的嵌套消息不再次解析。
  调用方须先解压并解码 `compress_content`，不能直接传入压缩字节。

这是一项保守的类型定义策略，并非完整复制 WCDA 的渲染兜底：
WCDA 的部分路径用 XML 渲染覆盖旧分类，且最后默认 text；本模块保留数据库冲突与未知值，
避免把未来消息类型或聊天记录中的嵌套消息误报为文本/引用。

## 映射

| 主类型 | 标签 / category |
| --- | --- |
| 1 | 文本 / text |
| 3 | 图片 / image |
| 34 | 语音 / voice |
| 42 | 名片 / contact |
| 43、62 | 视频 / video |
| 47 | 动画表情 / emoji |
| 48 | 位置 / location |
| 49 | 应用消息 / app |
| 50 | 通话 / voip |
| 10000 | 系统消息 / system |
| 10002 | 撤回消息 / revoke（继承旧实现） |

| 49 子类型 | 标签 / category |
| --- | --- |
| 3 | 音乐 / music |
| 4、5、68 | 链接 / link |
| 6、74 | 文件 / file |
| 8 | 动画表情卡 / emoji_card（继承旧实现的嵌套 appmsg 定义） |
| 19 | 聊天记录 / chat_history |
| 33、36、44 | 小程序 / mini_program（44 继承旧实现） |
| 51 | 视频号 / finder |
| 57 | 引用消息 / quote |
| 62 | 拍一拍 / pat |
| 63、88 | 直播 / live |
| 87 | 群公告 / announcement |
| 2000 | 转账 / transfer |
| 2001、2003 | 红包 / red_packet |

5/68 有 `mp.weixin.qq.com` 链接或直系 `sourceusername` 以 `gh_` 开头时细化为
公众号文章 / `official_article`。只有数值 5 不足以断言文章。
10000 的 `sysmsg` 带 `revokemsg` 类型/直系子块时细化为撤回消息 / `revoke`，
`type="patmsg"` 时细化为拍一拍 / `pat`；其他系统类型保留 `system`。
10002、8、44 取自旧实现，不声称它们由本次 WCDA 参考确认。

## 源码依据

参考版本为 WCDA commit `a0390ad`，路径相对于 WCDA 仓库：

| 文件与行号 | 依据 |
| --- | --- |
| `src/wechat_decrypt_tool/routers/general.py:999–1001` | 低 32 位主类型掩码 |
| `src/wechat_decrypt_tool/chat_helpers.py:192–232` | 主类型、49 子类型概览（含 63/88 直播、87 群公告） |
| 同上 `235–278` | 复合整数实例；可按高 32 位核算各子类型 |
| 同上 `845–866` | 5/68 公众号文章的来源与 URL 判定 |
| 同上 `1118–1161` | 系统消息与撤回处理 |
| 同上 `1354–1388` | appmsg 直系 type，防嵌套 type 干扰 |
| 同上 `1409–1448` | 音乐、聊天记录及 4/5/68 链接 |
| 同上 `1469–1544` | 51 视频号、33/36 小程序 |
| 同上 `1583–1616` | 6/74 文件；剔除 recorditem 中的引用标记 |
| 同上 `1711–1754` | 拍一拍属性、2000 转账、2001/2003 红包及提示优先顺序 |
| 同上 `2871–2900` | 主类型 62 视频、50 通话 |

当前映射为这些源码识别能力的并集，不是微信官方协议的完整枚举。
合成回归位于 `tests/test_message_types.py`；没有使用个人聊天记录。

补充来源（旧版 17 工具 MCP 的 `mcp_server.py`，未发布）：
`476–482` 的主类型映射给出 10002 撤回；`914–915` 给出 33/36/44 小程序；
`1057–1062` 的嵌套 appmsg 映射给出 8 动画表情卡。44 没有标为音乐。
上述文件是未发布工作副本中的证据，WCDA 的 commit 不适用于它。
