"""Synthetic cases only: no private message data."""

import unittest
from urllib.parse import quote

from wechat_extract.cards import parse_card, summarize

ARTICLE = ("https://mp.weixin.qq.com/s?__biz=QUJD%3D%3D&mid=100&idx=1&underline_version=0&sn=abc"
           "&share_id=1&fromsharesource=underline&start=10&end=42&scene=169#wechat_redirect")
CLEAN = "https://mp.weixin.qq.com/s?__biz=QUJD%3D%3D&mid=100&idx=1&sn=abc"


def _row(category, label, content):
    return {"message_type": {"category": category, "label": label}, "content_text": content}


def _image(source_url=None):
    attr = f' imgsourceurl="{quote(source_url, safe="")}"' if source_url else ""
    return f'<msg><img aeskey="k" md5="m"{attr}></img></msg>'


class CardTests(unittest.TestCase):
    def test_plain_image_is_not_a_card(self):
        row = _row("image", "图片", "wxid_a:\n" + _image())
        self.assertIsNone(parse_card(row))
        self.assertEqual(summarize(row), "[图片]")

    def test_underline_share_image_exposes_article(self):
        row = _row("image", "图片", "wxid_a:\n" + _image(ARTICLE))
        card = parse_card(row)
        self.assertEqual(card["url"], CLEAN)
        self.assertEqual(card["image_source"]["kind"], "公众号划线分享")
        self.assertEqual(card["image_source"]["highlight"], {"start": 10, "end": 42})
        self.assertNotIn("title", card)  # 调用方按 title 在界面定位消息，图片不能有
        summary = summarize(row)
        self.assertEqual(summary.split("\n", 1)[0], "[图片]")
        self.assertIn(CLEAN, summary)

    def test_quote_of_underline_image_names_article(self):
        inner = _image(ARTICLE).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = ("<msg><appmsg><title>这篇说了什么</title><type>57</type><refermsg><type>3</type>"
                f"<displayname>甲</displayname><content>{inner}</content></refermsg></appmsg></msg>")
        row = _row("quote", "引用消息", body)
        card = parse_card(row)
        self.assertEqual(card["quoted"]["content"], "[图片]")
        self.assertEqual(card["quoted"]["image_source"]["url"], CLEAN)
        self.assertIn(f"引用 甲: [图片]（公众号划线分享，原文: {CLEAN}）", summarize(row))

    def test_quote_of_card_uses_inner_title(self):
        inner = "<msg><appmsg><title>文章标题</title><type>5</type></appmsg></msg>"
        inner = inner.replace("<", "&lt;").replace(">", "&gt;")
        body = ("<msg><appmsg><title>看看</title><type>57</type><refermsg><type>49</type>"
                f"<content>{inner}</content></refermsg></appmsg></msg>")
        row = _row("quote", "引用消息", body)
        self.assertEqual(parse_card(row)["quoted"]["content"], "文章标题")
        self.assertTrue(summarize(row).endswith("| 引用: 文章标题"))


if __name__ == "__main__":
    unittest.main()
