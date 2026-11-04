"""显式选择账号和会话的真实数据验证；默认跳过，不扫描全账号媒体。

WECHAT_RUN_MEDIA_TESTS=1
WECHAT_TEST_ACCOUNT_DIR=已解密单账号目录
WECHAT_TEST_SOURCE_ACCOUNT_DIR=原账号目录（含 msg/attach）
WECHAT_TEST_CHAT=会话 username
图片解密另需 WECHAT_IMAGE_AES_KEY / WECHAT_IMAGE_XOR_KEY。
"""

import os
from pathlib import Path

import pytest

from wechat_extract.messages import MessageReader
from wechat_extract.media import resolve_image
from wechat_extract.wxdat import dat_decrypt

pytestmark = pytest.mark.skipif(
    os.environ.get("WECHAT_RUN_MEDIA_TESTS") != "1",
    reason="真实数据测试须显式开启",
)


def selected_account():
    values = [os.environ.get(key, "") for key in (
        "WECHAT_TEST_ACCOUNT_DIR", "WECHAT_TEST_SOURCE_ACCOUNT_DIR", "WECHAT_TEST_CHAT",
    )]
    if not all(values):
        pytest.skip("需要指定已解密账号、源账号和会话")
    return Path(values[0]), Path(values[1]), values[2]


def selected_image():
    account, source, chat = selected_account()
    for row in MessageReader(account).list_messages(chat, limit=100):
        if row["message_type"].base_type != 3:
            continue
        result = resolve_image(account, source, chat, row["local_id"], shard=row["shard"])
        if result["status"] == "ok":
            return row, result
    pytest.skip("该会话最近100条内没有可明确关联的本地图片")


def test_live_message_identity_roundtrip():
    account, _, chat = selected_account()
    reader = MessageReader(account)
    rows = reader.list_messages(chat, limit=100)
    if not rows:
        pytest.skip("指定会话没有本地消息")
    for row in rows:
        exact = reader.get_message(chat, row["local_id"], shard=row["shard"])
        assert exact == row


def test_live_image_path_keeps_identity():
    row, result = selected_image()
    assert Path(result["path"]).is_file()
    assert result["identity"]["shard"] == row["shard"]
    assert result["identity"]["local_id"] == row["local_id"]


def test_live_image_decrypt():
    aes = os.environ.get("WECHAT_IMAGE_AES_KEY")
    xor = os.environ.get("WECHAT_IMAGE_XOR_KEY")
    if not aes or not xor:
        pytest.skip("需要显式图片AES/XOR密钥")
    _, result = selected_image()
    plain, metadata = dat_decrypt(Path(result["path"]).read_bytes(), int(xor, 0), aes)
    assert plain and (metadata["isWxgf"] or metadata["ext"] in {"jpg", "png", "gif", "webp"})
