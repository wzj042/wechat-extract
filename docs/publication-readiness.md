# 公开发布评估（2026-11-04）

后续范围确认：当前目标改为私有仓库托管与有限共享，见 [私有共享检查](private-sharing.md)。下文保留为将来公开发布的评估，不是本次要求完成的公开发行计划。

**结论：独立项目可以继续本地开发，目前不具备整库按标准开源许可证发布的证据。** 功能测试通过与脱敏检查通过，不代表第三方代码的再分发权已解决。当前不应添加覆盖全库的 MIT/Apache 许可证或发布到公共包仓库。

## 来源核查

| 部分 | 本次确认的证据 | 仍需处理 |
| --- | --- | --- |
| 数据库与辅助模块 | 私有中间仓库固定版本的 README 明确列出从 WCDA 拷贝的模块；两个固定源码树均未找到项目级许可证 | 取得覆盖派生代码的许可，或替换/移除；不能用中间仓库所有权代替上游授权 |
| WAL、消息类型 | 现有来源记录指向 WCDA；其固定树有第三方 notice，但该 notice 只说明其他组件 | 逐段追溯，区分格式事实、独立实现与复用表达 |
| 图片密钥/DAT/WXGF | `wxdat.py` 明确写了 WeFlow 复刻来源；本次固定引用的 LICENSE 为 CC BY-NC-SA 4.0 | 追回原参考版本；若目标允许商用，需要另获授权或替换/移除受限实现 |
| Windows 取密钥 | 公共基础项目确认为 MIT，许可全文已保留 | 未发布修改版和 WCDA 参考部分缺完整来源链，不能据 MIT 标签宣称整体已清理 |
| 其他新模块与测试 | 独立仓库已去掉生产配置与历史 | 文件级来源审核仍需完成，不能简单把未列入上表的代码视为原创 |

机器可读的问题清单见 [release-audit.json](release-audit.json)，归属文件清单是当前已知范围，不是穷尽认定。完整 notice 见 [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md)。

核查引用：

- [WCDA 固定源码树](https://github.com/LifeArchiveProject/WeChatDataAnalysis/tree/a0390ad330f98f07c6403e9b33fb2bd2f1839835) 与 [第三方 notice](https://github.com/LifeArchiveProject/WeChatDataAnalysis/blob/a0390ad330f98f07c6403e9b33fb2bd2f1839835/THIRD_PARTY_NOTICES.md)。README 的“开源”称呼、可下载源码或欢迎贡献，并不明确授予所需再分发权。无许可证的通常含义见 [GitHub Choose a License](https://choosealicense.com/no-permission/)。
- [WeFlow 本次核查的 LICENSE](https://github.com/hicccc77/WeFlow/blob/91adcfdbb24e8e147d8332440b8c6ac899ad2abf/LICENSE)。[CC 官方摘要](https://creativecommons.org/licenses/by-nc-sa/4.0/)要求署名、非商业用途及派生作品相同方式共享；[OSI 定义第 6 条](https://opensource.org/osd)要求不得限制商业领域使用。因此按该限制开放源码也不能称为符合 OSI 定义的开源。
- [wechat-decrypt 本次核查的 MIT](https://github.com/328336690/wechat-decrypt/blob/44427c45786feba4e5fc21625f7934528a83f624/LICENSE)。本次核查固定引用不冒充原始导入版本。

## 隐私与发布产物

源码树已排除密钥、账号数据库、媒体、虚拟环境和生产配置。初始提交使用维护者已有 Git 身份，作者/提交者邮箱会随 Git 历史公开；忽略文件或修改 `user.email` 不会改变既有提交。是否保留此元数据应在真正发布前决定，本次不改写历史。

运行可重复的只读检查：

```powershell
uv run python tools/check_release.py --privacy-only --json
uv run python tools/check_release.py --json
```

前者扫描 Git 跟踪的工作文件、暂存区和所有本地 refs 可达的历史 blob；后者还检查授权问题清单与项目许可证。发现敏感格式、私有数据路径、待人工检查的二进制/超大文件、未解决授权或检查失败时返回非零。输出不含匹配到的密钥值。当前完整检查**应因许可问题而失败**；不得为了使它变绿直接改问题状态。

这是有限模式扫描，不识别所有真实姓名、聊天正文、未知 token 或照片；未跟踪文件、远程 refs 中尚未获取的历史和外部构建输入不在检查范围内。公共发布前还要复核实际归档内容。wheel 中现已包含来源记录、notice 与已核实的第三方许可文本；保留文本本身不消除授权缺口。

## 可用性与下一步

当前是 Python 3.13.5 / Windows 下通过验证的本地工具；Python 最低声明为 3.11，尚未完成各版本及非 Windows 的验证矩阵。默认测试只用合成数据。3 项真实媒体测试跳过，不能据此宣称各种微信版本、账号和媒体都已验证。

本轮调整后的回归为 183 passed、3 skipped、49 subtests passed，其中 5 项新增测试覆盖历史中已删除的凭证、暂存区与工作副本不一致、强制跟踪私有数据、未提交改动以及缺失/未完成的授权审核。wheel 与源码包均构建成功，许可文本随包逐字校验，归档不含运行目录。

继续调整时，先完成来源梳理和对受限实现的取舍，再做模块/接口重构，避免在将被替换的代码上重复投入。重写要依据可复用的协议资料、独立需求和合成测试记录实现过程；机械改名、翻译语言或让模型改写既有代码不自动消除派生关系。

技术上仍需单独处理：CLI 与 MCP 能力对应、配置/自检入口、表情联网显式许可、稳定消息契约及游标。当前表情缓存未命中会访问白名单 CDN；当前 `account` 字段包含本机路径，消息可能携带原始 XML，HTTP 可通过参数绑定非回环地址。以上均应按当前文档理解，不能用目标设计代替现状。

本评估是基于仓库证据的发布准备判断，不保证某项实现构成或不构成派生作品；有疑义的权属和许可兼容性需由权利人或专业审查确认。
