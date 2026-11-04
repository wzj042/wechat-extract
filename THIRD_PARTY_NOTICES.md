# Third-party provenance and notices

This project has **no project-wide open-source license yet**. These notices
preserve known attribution and license texts; they do not resolve missing
permissions or relicense upstream code. See `docs/publication-readiness.md`
and `docs/release-audit.json` for unresolved items.

## WeChatDataAnalysis and the intermediate extraction

The database implementation originates from
[LifeArchiveProject/WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis)
through `wzj042/wechat-db-decrypt` (a private intermediate repository).
The intermediate README at `854594ae37189e4d0dfaee8681b1b23e6f9ab0e4`
identifies copied modules: `wechat_decrypt.py`, `app_paths.py`,
`database_filters.py`, `sqlite_diagnostics.py`, `logging_config.py`,
and a reduced `request_logging.py`. Later work also used WAL and message-type
implementations from WCDA. No project-wide license was found in the inspected
WCDA tree at `a0390ad330f98f07c6403e9b33fb2bd2f1839835` or the intermediate tree.
The third-party notices in WCDA are not a license for WCDA's own code.

Local changes include WAL handling, validation and diagnostics, CLI/MCP
integration, package renaming and removal of unrelated application features.
Precise per-file derivation still needs review before redistribution.

## WeFlow

`src/wechat_extract/wxdat.py` records derivation from
[hicccc77/WeFlow](https://github.com/hicccc77/WeFlow): image-key derivation,
V2 DAT decryption and WXGF conversion. The implementation is adapted to Python
with local path handling, explicit AES-size semantics and image validation.

The license inspected at `91adcfdbb24e8e147d8332440b8c6ac899ad2abf`
is **CC BY-NC-SA 4.0**. Its unmodified text is in
`docs/licenses/WeFlow-CC-BY-NC-SA-4.0.txt`.
That commit records this audit's evidence, **not** the original import version,
which remains unknown. This notice does not assert that all required attribution
or historical provenance has been recovered.

## wechat-decrypt

[328336690/wechat-decrypt](https://github.com/328336690/wechat-decrypt)
is credited for earlier MCP/query behavior, V2 header identification, and the
base of the unpublished Windows key-extraction modifications.
The Windows helper adds WinAPI declarations, chunk scanning, output protections
and synthetic tests. Its unpublished intermediate version also refers to WCDA;
the upstream MIT license does not establish rights in those additional changes.

Copyright (c) 2026 328336690.
The unmodified MIT text inspected at
`44427c45786feba4e5fc21625f7934528a83f624`
is in `docs/licenses/wechat-decrypt-MIT.txt`. This is an audit reference,
not a claim that the unpublished modifications came from that exact commit.

## Behavioral and design references

- `fanyuantaier/wechatauto-replica` at
  `d322c4a9b1124228820bab363e5676016f188fc6`: Apache-2.0;
  path, shard-identity and media behavior references, recorded as independently
  implemented locally. Verify any copied fragments separately.
- `freanocank/wechat-decrypt` and `zhuobichen/weflow-cli`: MIT in current
  repository metadata; CLI/MCP design references, original reference versions
  not recorded. No blanket license for this project follows from these references.

Python dependencies are installed separately under their own licenses.
The wheel does not bundle ffmpeg, WeChat native binaries, models or account data.
If distributing a standalone executable or third-party binaries later, audit
their bundled licenses and notices separately.

In an installed wheel this notice, the source register and license texts reside
under `wechat_extract/_notices/`; relative paths above refer to the source tree.
