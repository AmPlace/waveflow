"""频道名归一化 / 合并 / 分类的回归测试。

目标：守护 normalize_channel_name / format_name / Alias.get_primary /
ChannelTemplate.match 的行为，防止减法规则改动引入误伤或 bug 回归。

两类用例：
  - *_baseline：锁定当前正确行为（改减法规则后不应破坏）
  - *_after_fix：当前是 bug，改完减法规则 + alias 后应通过
"""

import unittest

from alias import Alias, format_name
from m3u8_parser import normalize_channel_name
from template import ChannelTemplate


class FormatNameBaselineTest(unittest.TestCase):
    """format_name 的当前正确行为（不应被改动破坏）。"""

    def test_strip_resolution_tags(self):
        self.assertEqual(format_name("湖南卫视HD"), "湖南卫视")
        self.assertEqual(format_name("CCTV-1高清"), "cctv1")
        self.assertEqual(format_name("翡翠台4K"), "翡翠台")

    def test_strip_separators(self):
        self.assertEqual(format_name("CCTV-1"), "cctv1")
        self.assertEqual(format_name("CCTV_1"), "cctv1")

    def test_traditional_to_simplified(self):
        self.assertEqual(format_name("翡翠臺"), "翡翠台")

    def test_plus_replacement(self):
        self.assertEqual(format_name("CCTV5+"), "cctv5+")
        self.assertEqual(format_name("CCTV5PLUS"), "cctv5+")

    def test_strip_provider_in_subpattern(self):
        # _SUB_PATTERN 含 电信/联通/移动/广电/中央/电视台
        self.assertEqual(format_name("CCTV-1电信"), "cctv1")


class NormalizeBaselineTest(unittest.TestCase):
    """normalize_channel_name 当前正确行为（改规则后不应破坏）。"""

    def test_cctv_numbered(self):
        self.assertEqual(normalize_channel_name("CCTV-1"), "cctv1")
        self.assertEqual(normalize_channel_name("CCTV1"), "cctv1")
        self.assertEqual(normalize_channel_name("CCTV-01咪咕"), "cctv1")

    def test_cctv_two_digit_number_not_stripped(self):
        # 防止 fps 规则 \d{2} 误吃两位数编号（曾把 CCTV-13 吃成 cctv）
        self.assertEqual(format_name("CCTV-13"), "cctv13")
        self.assertEqual(format_name("CCTV-15"), "cctv15")
        self.assertEqual(normalize_channel_name("CCTV-13新闻"), "cctv13")
        self.assertEqual(normalize_channel_name("CCTV-15音乐"), "cctv15")

    def test_cctv_hd_variant_merges(self):
        self.assertEqual(normalize_channel_name("CCTV1HD"), "cctv1")
        self.assertEqual(normalize_channel_name("CCTV-1HD"), "cctv1")

    def test_4k_variant_merges(self):
        self.assertEqual(normalize_channel_name("北京卫视4K"), "北京卫视")
        self.assertEqual(normalize_channel_name("广东卫视4K"), "广东卫视")

    def test_brand_prefix_merges(self):
        # TVB 前缀 → 翡翠台/明珠台（alias 归并）
        self.assertEqual(normalize_channel_name("TVB翡翠台"), "翡翠台")
        self.assertEqual(normalize_channel_name("TVB明珠台"), "明珠台")

    def test_cctv4_region_distinct(self):
        # CCTV-4 美洲/欧洲是独立频道，不应被压成 cctv4
        self.assertEqual(normalize_channel_name("CCTV-4美洲"), "cctv4美洲")
        self.assertEqual(normalize_channel_name("CCTV-4欧洲"), "cctv4欧洲")
        self.assertNotEqual(normalize_channel_name("CCTV-4"), "cctv4美洲")


class NormalizeBugFixTest(unittest.TestCase):
    """[48Kk] bug 修复后应通过：编号数字不应被当分辨率删掉。

    根因：_STRIP_RE 的 [48Kk] 把 GDTV8 的 8、HUBEI4 的 4 当分辨率删了，
    导致不同编号的地方台被压成同一个 key。修复后应保留编号。
    """

    def test_gdtv_numbered_distinct(self):
        self.assertEqual(normalize_channel_name("GDTV8"), "gdtv8")
        self.assertEqual(normalize_channel_name("GDTV4"), "gdtv4")
        self.assertNotEqual(
            normalize_channel_name("GDTV8"), normalize_channel_name("GDTV4")
        )

    def test_hubei_numbered_distinct(self):
        self.assertNotEqual(
            normalize_channel_name("HUBEI8"), normalize_channel_name("HUBEI4")
        )

    def test_qtv_numbered_distinct(self):
        self.assertNotEqual(
            normalize_channel_name("QTV4"), normalize_channel_name("QTV")
        )

    def test_sctv_numbered_distinct(self):
        self.assertNotEqual(
            normalize_channel_name("SCTV8"), normalize_channel_name("sctv4")
        )


class NormalizeNoFalsePositiveTest(unittest.TestCase):
    """防误伤：移除 _PROVIDER_RE 的 移动/源 后，真实频道名不被破坏。

    根因：_PROVIDER_RE 含 '移动'→误伤 '深圳移动电视'→'深圳电视'；
    含 '源'→误伤 '沂源生活'（沂源是地名）。修复后这些应保留原名。
    """

    def test_shenzhen_mobile_tv_not_stripped(self):
        # 移动 是频道身份（深圳移动频道），不是运营商后缀
        self.assertNotEqual(normalize_channel_name("深圳移动电视"), "深圳电视")
        self.assertNotEqual(normalize_channel_name("深圳移动电视"), "深圳")

    def test_mobile_opera_not_stripped(self):
        self.assertNotEqual(normalize_channel_name("移动戏曲"), "戏曲")

    def test_yiyuan_place_not_stripped(self):
        # 沂源/济源 是地名，源 不是后缀
        self.assertNotEqual(normalize_channel_name("沂源生活"), "沂生活")

    def test_bare_word_alias_not_overmerge(self):
        # 防止 alias 里"音乐/新闻/少儿"等裸词别名误伤：
        # 咪咕音乐 不应被归并成 CCTV-15（曾因 '音乐' 作 CCTV-15 别名而误判）
        self.assertNotEqual(normalize_channel_name("咪咕音乐"), "cctv15")
        self.assertNotEqual(
            normalize_channel_name("咪咕音乐"),
            normalize_channel_name("CCTV-15"),
        )
        # 同理：音乐频道 / MTV 不应塌进 CCTV-15
        self.assertNotEqual(normalize_channel_name("音乐频道"), "cctv15")
        self.assertNotEqual(normalize_channel_name("MTV"), "cctv15")
        # 但带前缀的完整形态仍应命中（央视音乐 → CCTV-15）
        self.assertEqual(
            normalize_channel_name("央视音乐"), normalize_channel_name("CCTV-15")
        )


class NormalizeStripNoiseTest(unittest.TestCase):
    """通用噪音词剥离（减法规则增强后应通过）。"""

    def test_strip_migu(self):
        # 咪咕是运营商分发标注，应剥
        self.assertEqual(normalize_channel_name("湖南卫视咪咕"), "湖南卫视")
        self.assertEqual(normalize_channel_name("东方卫视咪咕"), "东方卫视")

    def test_strip_high_bitrate(self):
        # 高码是线路质量标注，应剥
        self.assertEqual(normalize_channel_name("CCTV-1高码"), "cctv1")

    def test_strip_iptv_mid(self):
        # IPTV 在中间（非行尾）也应剥：北京IPTV淘剧场 → 北京淘剧场 或 北京
        # 只要剥掉 IPTV 即可，后续靠 alias 兜底
        result = normalize_channel_name("北京IPTV淘剧场")
        self.assertNotIn("iptv", result)

    def test_strip_channel_suffix(self):
        # 行尾"频道"后缀应剥
        self.assertEqual(normalize_channel_name("陕西体育休闲频道"), "陕西体育休闲")
        self.assertEqual(normalize_channel_name("内蒙古农牧频道"), "内蒙古农牧")

    def test_strip_bureau_prefix_and_traditional(self):
        # "广播电视总台" 机构前缀应剥
        self.assertEqual(
            normalize_channel_name("海南广播电视总台少儿频道"), "海南少儿"
        )
        # 繁体"頻道"应转简体后被 频道$ 剥除
        self.assertEqual(normalize_channel_name("東森新聞51頻道"), "东森新闻51")

    def test_strip_source_tag_in_paren(self):
        # (备用)/(测试) 行尾源标注剥，(国内电影) 内容分类保留
        self.assertEqual(normalize_channel_name("德州图文(备用)"), "德州图文")
        self.assertIn("国内电影", normalize_channel_name("1905电影网(国内电影)"))


class AliasMergeTest(unittest.TestCase):
    """alias.txt 语义合并（补全后应通过）。"""

    def setUp(self):
        # 用项目全局 alias 实例
        from m3u8_parser import _channel_alias
        self.alias = _channel_alias

    def test_cctv_chinese_name(self):
        # 央视一套 / 一套 → CCTV-1（CCTV 正则只认 CCTV 开头，需显式别名）
        self.assertEqual(self.alias.get_primary("央视一套"), "CCTV-1")
        self.assertEqual(self.alias.get_primary("央视新闻"), "CCTV-13")

    def test_s卫视_abbreviation(self):
        # 芒果台 → 湖南卫视
        self.assertEqual(self.alias.get_primary("芒果台"), "湖南卫视")

    def test_pinyin_alias(self):
        # 拼音 → 中文主名
        self.assertEqual(self.alias.get_primary("hunan"), "湖南卫视")

    def test_international_channel_split_words(self):
        # Discovery Channel → DiscoveryHD
        self.assertEqual(self.alias.get_primary("Discovery Channel"), "DiscoveryHD")
        self.assertEqual(self.alias.get_primary("BBC World News"), "BBCWORLDNEWSASIA")

    def test_local_channel_abbreviation_to_chinese(self):
        # SCTV/CDTV 英文缩写 → 中文主名（用户看中文更直观）
        self.assertEqual(self.alias.get_primary("SCTV2"), "四川经济")
        self.assertEqual(self.alias.get_primary("SCTV-5"), "四川影视文艺")
        self.assertEqual(self.alias.get_primary("CDTV-2"), "成都经济")
        self.assertEqual(self.alias.get_primary("cdtv4"), "成都影视")

    def test_local_channel_variant_merge(self):
        # 简写变体合并：蒙语/蒙古语同义、呼市=呼和浩特
        self.assertEqual(
            normalize_channel_name("内蒙古蒙古语卫视"),
            normalize_channel_name("内蒙古蒙语卫视"),
        )
        self.assertEqual(
            normalize_channel_name("呼和浩特新闻综合"),
            normalize_channel_name("呼市新闻综合"),
        )


class ChannelTemplateMatchTest(unittest.TestCase):
    """分类匹配：alias 主名 → template 分类。"""

    def setUp(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "config", "template.txt"
        )
        self.ct = ChannelTemplate(path)

    def test_cctv_category(self):
        self.assertEqual(self.ct.match("CCTV-1"), "央视")
        self.assertEqual(self.ct.match("CCTV-5"), "央视")

    def test_s_卫视_category(self):
        self.assertEqual(self.ct.match("湖南卫视"), "卫视")
        self.assertEqual(self.ct.match("北京卫视"), "卫视")

    def test_hk_tw_mo_separated(self):
        # 港澳台应拆成三个独立分类（香港/台湾/澳门），不再合并
        cats = self.ct.get_all_categories()
        self.assertIn("香港", cats)
        self.assertIn("台湾", cats)
        self.assertIn("澳门", cats)
        self.assertNotIn("港澳台", cats)

    def test_kids_category_via_alias_primary(self):
        # 湖南金鹰卡通 → alias 归一到 金鹰卡通 → 匹配少儿
        from m3u8_parser import _channel_alias
        primary = _channel_alias.get_primary("湖南金鹰卡通")
        self.assertEqual(primary, "金鹰卡通")
        self.assertEqual(self.ct.match(primary), "少儿")


class ClassifyPriorityTest(unittest.TestCase):
    """分类优先级：template → 源group有效省份 → 频道名识别省份 → 源group。"""

    def test_template_takes_priority(self):
        # CCTV-1 即使 group=其他，也命中 template 分到央视
        from template import channel_template, normalize_group_name, detect_province
        from m3u8_parser import _channel_alias
        name, grp = "CCTV-1", "其他"
        primary = _channel_alias.get_primary(name)
        tmpl_cat = channel_template.match(primary)
        self.assertEqual(tmpl_cat, "央视")

    def test_source_group_used_when_no_template(self):
        # 频道不在 template，但源 group 是有效省份 → 用源 group
        from template import normalize_group_name
        norm_grp = normalize_group_name("河南地区")
        self.assertEqual(norm_grp, "河南")
        self.assertNotIn(norm_grp, ("地方", "其他"))

    def test_detect_province_for_local_group(self):
        # 源 group=地方 时，从频道名识别省份
        from template import detect_province
        self.assertEqual(detect_province("南京新闻综合"), "江苏")
        self.assertEqual(detect_province("临沂综合"), "山东")
        self.assertEqual(detect_province("内蒙古卫视"), "内蒙古")
        # 全国频道识别不出（正确，不该归省份）
        self.assertEqual(detect_province("优漫卡通"), "")

    def test_detect_province_no_false_positive(self):
        from template import detect_province
        # 东方卫视 不该误匹配海南东方市
        self.assertEqual(detect_province("东方卫视"), "")
        self.assertEqual(detect_province("上视东方影视"), "")


class NormalizeStabilityTest(unittest.TestCase):
    """稳定性：常见频道名归一后稳定、可预测。"""

    def test_stable_for_common_channels(self):
        cases = {
            "CCTV-1": "cctv1",
            "CCTV1HD": "cctv1",
            "湖南卫视": "湖南卫视",
            "湖南卫视HD": "湖南卫视",
            "翡翠台": "翡翠台",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(normalize_channel_name(name), expected)


class FujianChannelAliasTest(unittest.TestCase):
    """福建频道别名归一化：其他源的变体写法应落到与主名相同的 canonical_key。

    策略：新闻类归并（XX新闻综合→XX新闻），纯综合类保留独立（三明综合等）。
    """

    def test_news_variants_merge_to_primary(self):
        cases = {
            "福建文旅体育": "福建文体",
            "福建体育": "福建文体",
            "福州新闻综合": "福州新闻",
            "福州一套": "福州新闻",
            "泉州新闻综合": "泉州新闻",
            "泉州一套": "泉州新闻",
            "漳州新闻综合": "漳州新闻",
            "莆田新闻综合": "莆田新闻",
            "南平综合": "南平新闻",
            "龙岩综合": "龙岩新闻",
            "宁德新闻综合": "宁德新闻",
        }
        for variant, primary in cases.items():
            with self.subTest(name=variant):
                self.assertEqual(
                    normalize_channel_name(variant),
                    normalize_channel_name(primary),
                )

    def test_xiamen_series_primary_is_chinese_quantifier(self):
        cases = {
            "厦门1": "厦门一套",
            "厦门二套": "厦门二套",
            "厦门3": "厦门移动电视",
            "厦门电视台移动电视": "厦门移动电视",
        }
        for variant, primary in cases.items():
            with self.subTest(name=variant):
                self.assertEqual(
                    normalize_channel_name(variant),
                    normalize_channel_name(primary),
                )

    def test_min_dialect_and_public_primary(self):
        cases = {
            "泉州四套": "泉州闽南语",
            "莆田2套": "莆田公共",
            "莆田二套": "莆田公共",
        }
        for variant, primary in cases.items():
            with self.subTest(name=variant):
                self.assertEqual(
                    normalize_channel_name(variant),
                    normalize_channel_name(primary),
                )

    def test_pure_zonghe_kept_independent(self):
        # 纯"综合"类不归并，保持各自独立主名
        for name in ("三明综合", "晋江综合", "石狮新闻综合", "平潭综合"):
            with self.subTest(name=name):
                self.assertEqual(normalize_channel_name(name), normalize_channel_name(name))


if __name__ == "__main__":
    unittest.main()