# 来源与许可

历史来源记录始于 2026-10-20；2026-11-04 重新核对固定源码树、仓库可见性及许可证。保留来源实现，只做独立运行、类型定义和接入本包所需的修改。发布结论与文件级问题清单见 [公开发布评估](publication-readiness.md)。

| 项目 | 许可 | 固定版本 | 用途 |
| --- | --- | --- | --- |
| [wzj042/wechat-db-decrypt](https://github.com/wzj042/wechat-db-decrypt)（私有） | 固定树无项目级许可证 | `854594ae37189e4d0dfaee8681b1b23e6f9ab0e4` | 独立数据库解密、监听、CLI 与基础测试的直接上游（由 WCDA 抽取）；本次经授权连接可读取，匿名 404 不代表仓库已删除 |
| [LifeArchiveProject/WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis)（WCDA） | 无许可证文件 | `a0390ad330f98f07c6403e9b33fb2bd2f1839835` | 数据库解密的源头；WAL 修复、数据库诊断、日志脱敏、消息类型识别分支 |
| [328336690/wechat-decrypt](https://github.com/328336690/wechat-decrypt) | MIT | 无对应 commit | 旧 17 工具 MCP 的接口与查询语义参考；`tools/extract_windows_key.py` 由 `find_all_keys_v4.py`、`get_passphrase.py` 的未发布修改版重整；这两份脚本基于该项目的 `find_all_keys` 系列，公开仓库中没有同名文件，未固定版本 |
| [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) | Apache-2.0 | `d322c4a9b1124228820bab363e5676016f188fc6` | 路径、多分片和图片格式（issues #20/#32/#34），独立实现 |
| [hicccc77/WeFlow](https://github.com/hicccc77/WeFlow) | CC BY-NC-SA 4.0 | 未记录 | `wxdat` 的图片密钥推导、V2 解密与 WXGF 转换（取自未纳入版本管理的后续实现） |
| [freanocank/wechat-decrypt](https://github.com/freanocank/wechat-decrypt) | MIT | 未记录 | 设计参考：CLI 为核心、薄 MCP、`doctor`、全程 `--json`、`SKILL.md` 加 `references/` 控制 agent 上下文；系统事件分类表 |
| [zhuobichen/weflow-cli](https://github.com/zhuobichen/weflow-cli) | MIT | 未记录 | 设计参考：`init` 先验证已有密钥再决定是否重新提取；`mcp-config` 生成客户端配置；`capabilities --json` 声明安全边界 |

数据库上游本身由 WCDA 抽取。本仓库保留独立 CLI 的范围，并同步当前 WCDA 所需的 WAL 修复；没有搬入 WCDA Web 服务、界面、原生运行时或账号配置。

合成回归发现快速 `quick_check` 无法覆盖索引条目缺失，因此最终输出使用完整 `integrity_check` 决定是否需要修复索引；修复只针对生成的明文副本。监听快照同时核对主库、WAL、SHM 的文件元信息，避免仅主库未变就接受复制期间变化的旁路文件。

Windows 密钥工具重整为独立入口，补充 WinAPI 类型、分块扫描、输出保护与合成测试。图片实现移除个人路径示例，修正无效导出符号，并接入本包命令行。

消息类型模块根据 WCDA 的识别分支整理，具体来源位置在 `message-types.md`。定义与消息展示分离，未知值不套用 WCDA 展示层的通用文本兜底。

UI 自动化、自动回复等调用方功能，以及密钥文件、真实消息、缓存和试错产物，均不在本仓库范围内。

## 许可

WCDA 与 WeFlow 的许可记录见上表。私有中间仓库的 README 明确列出从 WCDA 拷贝的模块，其固定源码树同样未发现项目级许可证。独立抽离未解决这些来源的授权问题，也未确立整个项目可统一采用的开源许可证；后续发布前仍需逐项核对来源与授权。

2026-11-04 的许可证核查固定引用为 WeFlow `91adcfdbb24e8e147d8332440b8c6ac899ad2abf` 与 wechat-decrypt `44427c45786feba4e5fc21625f7934528a83f624`。它们仅固定本次核查证据，不补写上表尚缺失的原始导入版本。对应未修改的许可文本与归属说明见 [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md)。WCDA 中针对其他第三方组件的 notice 不构成其自身代码的许可证。

这些来源的授权说明仍以各来源为准；本仓库未自行给第三方代码授予新许可证。

路径和分片补充的证据、与参考仓库的行为差异、未覆盖部分见 [存储路径与分片身份](storage-and-shards.md)。
