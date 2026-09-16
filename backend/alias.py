import os
import re

# 繁简转换：用 zhconv 库（完整准确，替代手维护字表）
try:
    from zhconv import convert as _zh_convert
except ImportError:
    _zh_convert = None


_SUB_PATTERN = re.compile(
    r'[\-\_]|'
    r'\(.*?\)|（.*?）|\[.*?\]|「.*?」|'
    r'\s+|\|.*?\|｜|'
    r'频道|高清|标清|超清|超高清|4K|8K|HD|SD|FHD|UHD|HEVC|H\.?265|H\.?264|'
    r'\d{2,3}\s*(?:fps|FPS)|'
    r'咪咕|高码|(?<=[\u4e00-\u9fff])IPTV|总台|'
    r'中央|电视台|电信|联通|移动|广电'
)

_REPLACE_DICT = {'plus': '+', 'PLUS': '+', '＋': '+'}


def format_name(name: str) -> str:
    s = _zh_convert(name, 'zh-cn') if _zh_convert else name
    s = _SUB_PATTERN.sub('', s)
    for old, new in _REPLACE_DICT.items():
        s = s.replace(old, new)
    return s.lower()


class Alias:
    def __init__(self, alias_path: str = None):
        self.primary_to_aliases: dict[str, set[str]] = {}
        self.alias_to_primary: dict[str, str] = {}
        self.pattern_to_primary: list[tuple[re.Pattern, str]] = []

        if alias_path and os.path.exists(alias_path):
            with open(alias_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or ',' not in line:
                        continue
                    parts = [p.strip() for p in line.split(',')]
                    primary = parts[0]
                    aliases = set(parts[1:])
                    aliases.add(format_name(primary))
                    self.primary_to_aliases[primary] = aliases
                    for alias in aliases:
                        self.alias_to_primary[alias] = primary
                        if alias.startswith('re:'):
                            try:
                                pattern = re.compile(alias[3:])
                                self.pattern_to_primary.append((pattern, primary))
                            except re.error:
                                pass
                    self.alias_to_primary[primary] = primary

    def get_primary(self, name: str) -> str:
        primary = self.alias_to_primary.get(name)
        if primary:
            return primary
        for pattern, p in self.pattern_to_primary:
            if pattern.search(name):
                return p
        fmt = format_name(name)
        return self.alias_to_primary.get(fmt, name)
