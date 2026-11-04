"""Synthetic cases only: no private message data."""

import unittest

from wechat_extract.message_types import classify_message_type


class MessageTypeTests(unittest.TestCase):
    def test_base_types(self):
        expected = {
            1: "text", 3: "image", 34: "voice", 42: "contact", 43: "video",
            47: "emoji", 48: "location", 49: "app", 50: "voip", 62: "video",
            10000: "system", 999: "unknown", 10002: "revoke",
        }
        for raw, category in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(classify_message_type(raw).category, category)

    def test_all_app_subtypes_in_packed_and_xml_forms(self):
        expected = {
            3: "music", 4: "link", 5: "link", 6: "file", 8: "emoji_card", 19: "chat_history",
            33: "mini_program", 36: "mini_program", 44: "mini_program", 51: "finder", 57: "quote",
            62: "pat", 63: "live", 68: "link", 74: "file", 87: "announcement",
            88: "live", 2000: "transfer", 2001: "red_packet", 2003: "red_packet",
        }
        for subtype, category in expected.items():
            with self.subTest(subtype=subtype):
                raw = 49 | (subtype << 32)
                packed = classify_message_type(raw)
                xml = classify_message_type(49, f"<msg><appmsg><type>{subtype}</type></appmsg></msg>")
                self.assertEqual(packed.raw_type, raw)
                self.assertEqual(packed.base_type, 49)
                self.assertEqual(packed.subtype, subtype)
                self.assertEqual(packed.category, category)
                self.assertEqual(xml.category, category)
                self.assertEqual(xml.source, "xml")

    def test_unknown_and_conflicting_values_preserved(self):
        result = classify_message_type(49 | (9999 << 32), "<appmsg><type>5</type></appmsg>")
        self.assertEqual(result.packed_subtype, 9999)
        self.assertEqual(result.xml_subtype, 5)
        self.assertEqual(result.subtype, 9999)
        self.assertEqual(result.category, "unknown")
        self.assertTrue(result.conflict)
        result = classify_message_type(49, "<appmsg><type>9999</type><refermsg/></appmsg>")
        self.assertEqual(result.category, "unknown")
        self.assertEqual(result.subtype, 9999)
        signed = classify_message_type(-1)
        self.assertEqual(signed.raw_type, -1)
        self.assertEqual(signed.base_type, 0xFFFFFFFF)
        self.assertEqual(signed.packed_subtype, 0xFFFFFFFF)
        self.assertEqual(signed.category, "unknown")

    def test_nested_types_and_metadata_do_not_override(self):
        cases = [
            ("<appmsg><weappinfo><type>3</type></weappinfo><type>36</type></appmsg>", "mini_program"),
            ("<appmsg><refermsg><type>1</type></refermsg><type>57</type></appmsg>", "quote"),
            ("<appmsg><recorditem><![CDATA[<refermsg><type>57</type></refermsg>]]></recorditem><type>19</type></appmsg>", "chat_history"),
            ("<appmsg><type>4</type><patMsg><type>62</type></patMsg><url>https://example.org</url></appmsg>", "link"),
            ("<appmsg><recorditem><refermsg/><type>57</type></recorditem></appmsg>", "app"),
        ]
        for xml, category in cases:
            with self.subTest(xml=xml):
                self.assertEqual(classify_message_type(49, xml).category, category)
        self.assertEqual(classify_message_type(1, "<appmsg><type>6</type></appmsg>").category, "text")
        self.assertEqual(classify_message_type(999, "<appmsg><type>6</type></appmsg>").category, "unknown")

    def test_system_and_structural_hints(self):
        cases = [
            (10000, '<sysmsg type="revokemsg"><revokemsg/></sysmsg>', "revoke"),
            (10000, '<sysmsg type="new-unknown-system"/>', "system"),
            (10000, '<sysmsg type="patmsg"/>', "pat"),
            (49, '<appmsg type="patmsg"/>', "pat"),
            (49, '<appmsg type="patmsg"><type>4</type></appmsg>', "pat"),
            (49, '<appmsg type="patmsg"><type>4</type><url>https://example.org</url></appmsg>', "link"),
            (49, "<appmsg><patMsg/></appmsg>", "app"),
            (49, "<appmsg><refermsg/></appmsg>", "quote"),
            (49, "<appmsg><wcpayinfo><paysubtype>1</paysubtype><sendertitle/></wcpayinfo></appmsg>", "transfer"),
            (49, "<appmsg><wcpayinfo><sendertitle>红包</sendertitle></wcpayinfo></appmsg>", "red_packet"),
            (49, "<appmsg><type>2003</type><wcpayinfo><paysubtype>1</paysubtype></wcpayinfo></appmsg>", "red_packet"),
        ]
        for raw, xml, category in cases:
            with self.subTest(raw=raw, xml=xml):
                self.assertEqual(classify_message_type(raw, xml).category, category)
        self.assertEqual(classify_message_type(10000, '<sysmsg type="new"/>').system_type, "new")

    def test_bad_xml_and_untrusted_content(self):
        for xml in ("", "ordinary text", "<appmsg><type>6", "<appmsg><type>abc</type></appmsg>"):
            self.assertEqual(classify_message_type(49, xml).category, "app")
        xml = '<!DOCTYPE appmsg [<!ENTITY type "6">]><appmsg><type>&type;</type></appmsg>'
        self.assertEqual(classify_message_type(49, xml).category, "app")
        self.assertEqual(classify_message_type(49, 'sender:\n<appmsg><type><![CDATA[6]]></type></appmsg>').category, "file")
        self.assertEqual(classify_message_type(49, '<appmsg><type>-123</type></appmsg>').subtype, -123)

    def test_article_refinement_does_not_trust_lookalike_host(self):
        for url, category in (
            ("https://mp.weixin.qq.com/s/example", "official_article"),
            ("https://mp.weixin.qq.com.evil.invalid/s/example", "link"),
        ):
            xml = f"<appmsg><type>5</type><url>{url}</url></appmsg>"
            self.assertEqual(classify_message_type(49, xml).category, category)

    def test_type_validation(self):
        for raw in (True, "49", 49.0, None):
            with self.assertRaises(TypeError):
                classify_message_type(raw)
        for raw in (1 << 64, -(1 << 63) - 1):
            with self.assertRaises(ValueError):
                classify_message_type(raw)
        with self.assertRaises(TypeError):
            classify_message_type(49, b"<appmsg/>")


if __name__ == "__main__":
    unittest.main()
