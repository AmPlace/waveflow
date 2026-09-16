from typing import Any

import httpx

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError

# [orgid, channel_id] 映射
SDLY_CHANNELS: dict[str, list[int]] = {
    # 济南
    "jncqxw": [171, 2], "jncqsh": [171, 20], "jnjrtv": [303, 1], "jnjyzh": [85, 1],
    "jnjyys": [85, 2], "jnlcxw": [261, 1], "jnpyzh": [257, 1], "jnpyxc": [257, 3],
    "jnshzh": [97, 1], "jnshys": [97, 2], "jnzqzh": [195, 1], "jnzqgg": [195, 2],
    # 东营
    "dyxwzh": [537, 1], "dygg": [537, 3], "dygg2": [29, 90], "dykj": [537, 7],
    "dydyqxw": [163, 5], "dydyqkj": [163, 7], "dygrzh": [237, 1], "dygrkj": [237, 5],
    "dyklxw": [269, 3], "dyljzh": [153, 1], "dyljwh": [153, 3],
    # 青岛
    "qdcyzh": [403, 5], "qdhdzh": [227, 1], "qdhdsh": [227, 3], "qdjmzh": [221, 2],
    "qdjmsh": [221, 3], "qdjzzh": [305, 1], "qdjzsh": [305, 3], "qdlc": [173, 1],
    "qdls": [295, 1], "qdlxzh": [253, 1], "qdlxsh": [253, 3], "qdpdxw": [45, 4],
    "qdpdsh": [45, 5],
    # 潍坊
    "wfxwzh": [635, 1], "wfsh": [635, 5], "wfyswy": [635, 7], "wfkjwl": [635, 9],
    "wfgxq": [421, 14], "wfaqzh": [137, 3], "wfaqms": [137, 4], "wfbhxw": [199, 1],
    "wfclzh": [1, 3], "wfcyzh": [47, 1], "wfcyjj": [47, 2], "wffzxw": [285, 1],
    "wfgmzh": [71, 24], "wfgmdj": [71, 38], "wfhtxw": [133, 1], "wfkwtv": [127, 17],
    "wflqxw": [205, 39], "wfqzzh": [125, 2], "wfqzwh": [125, 3], "wfwc": [15, 3],
    "wfzcxw": [115, 23], "wfzcsh": [115, 25],
    # 烟台
    "ytcd": [175, 1], "ytfszh": [189, 4], "ytfssh": [189, 5], "ythyzh": [255, 1],
    "ytlkzh": [57, 1], "ytlksh": [57, 2], "ytlszh": [245, 4], "ytlsys": [245, 6],
    "ytlyzh": [241, 4], "ytlyms": [241, 7], "ytlzzh": [239, 1], "ytmpzh": [281, 1],
    "ytplzh": [109, 1], "ytplzy": [109, 2], "ytqxzh": [165, 12], "ytqxpg": [165, 14],
    "ytzyzh": [55, 2], "ytzyzy": [55, 4],
    # 淄博
    "zbbsxw": [17, 8], "zbbstw": [17, 9], "zbgqzh": [61, 1], "zbgqys": [61, 2],
    "zbht1": [23, 15], "zbht2": [23, 16], "zblzxw": [151, 6], "zblzsh": [151, 7],
    "zbyyzh": [203, 6], "zbyysh": [203, 7], "zbzcxw": [75, 1], "zbzcsh": [75, 2],
    "zbzd1": [101, 1], "zbzd2": [101, 6], "zbzctv1": [259, 1], "zbzctv2": [259, 3],
    # 枣庄
    "zzstzh": [243, 1], "zzszzh": [233, 1], "zztezxw": [185, 2], "zztzzh": [103, 2],
    "zztzms": [103, 3], "zzxcxw": [37, 8], "zzyczh": [209, 1],
    # 滨州
    "bzbctv": [249, 35], "bzbxzh": [207, 3], "bzbxsh": [207, 4], "bzhmzh": [211, 2],
    "bzhmys": [211, 3], "bzwdzh": [169, 1], "bzwdzy": [169, 21], "bzyxxw": [217, 1],
    "bzzhzh": [277, 1], "bzzhzy": [277, 9], "bzzpzh": [11, 15], "bzzpms": [11, 16],
    # 德州
    "dzxwzh": [179, 1], "dzjjsh": [179, 2], "dztw": [179, 9], "dzlczh": [215, 6],
    "dzllxw": [267, 1], "dzllcs": [267, 5], "dzly1": [49, 3], "dzly2": [49, 4],
    "dznjzh": [193, 1], "dzpyzh": [19, 2], "dzqhzh": [251, 8], "dzqyzh": [5, 9],
    "dzqysh": [5, 7], "dzwczh": [33, 4], "dzwczy": [33, 6], "dzxjzh": [223, 1],
    "dzxjgg": [223, 2], "dzyczh": [235, 1], "dzyczy": [235, 3],
    # 菏泽
    "hzcwzh": [131, 1], "hzcwzy": [131, 2], "hzcxzh": [87, 2], "hzdmxw": [111, 2],
    "hzdt1": [27, 7], "hzdt2": [27, 8], "hzjczh": [141, 186], "hzjyxw": [139, 1],
    "hzmdxw": [219, 6], "hzmdzy": [219, 17], "hzsxzh": [155, 2], "hzycxw": [135, 3],
    "hzyczy": [135, 2],
    # 济宁
    "jijiazh": [273, 1], "jijiash": [273, 3], "jijxzh": [129, 2], "jijxsh": [129, 4],
    "jilszh": [89, 1], "jiqfxw": [13, 1], "jircxw": [73, 8], "jircys": [73, 9],
    "jissxw": [117, 5], "jisswh": [117, 6], "jiws1": [53, 4], "jiws2": [53, 5],
    "jiwszh": [301, 1], "jiytxw": [63, 5], "jiytsh": [63, 15], "jiyzxw": [231, 1],
    "jiyzsh": [231, 3], "jizczh": [181, 1], "jizcwh": [181, 4],
    # 聊城
    "lccpzh": [31, 6], "lccpsh": [31, 8], "lcdczh": [265, 1], "lcdezh": [95, 22],
    "lcdezy": [95, 29], "lcgtzh": [43, 1], "lcgtzy": [43, 5], "lcgxzh": [79, 1],
    "lclqzh": [65, 2], "lclqjj": [65, 5], "lcsxzh": [183, 1], "lcsxsh": [183, 5],
    "lcygzh": [81, 1], "lcygys": [81, 10],
    # 临沂
    "lyfxzh": [41, 119], "lyfxsh": [41, 117], "lyhdys": [191, 1], "lyhdzh": [191, 2],
    "lyjnzh": [105, 4], "lyjnys": [105, 5], "lyllzh": [113, 131], "lyllgg": [113, 133],
    "lylszh": [201, 1], "lyls1": [167, 3], "lyls2": [167, 4], "lylzzh": [147, 1],
    "lylzys": [147, 17], "lymy1": [161, 13], "lymy2": [161, 15], "lypyzh": [345, 4],
    "lypysh": [345, 14], "lytc1": [83, 1], "lytc2": [83, 2], "lyynzh": [177, 6],
    "lyynys": [177, 7], "lyys1": [145, 1], "lyys2": [145, 2],
    # 日照
    "rzjx1": [159, 23], "rzjx2": [159, 27], "rzls": [289, 1], "rzwlzh": [299, 10],
    "rzwlwh": [299, 12],
    # 泰安
    "tadpzh": [187, 9], "tadpms": [187, 11], "tady": [293, 1], "tafczh": [51, 18],
    "tafcsh": [51, 6], "tany1": [123, 1], "tany2": [123, 7], "tats": [263, 1],
    "taxtzh": [59, 2], "taxtxc": [59, 3],
    # 威海
    "whxwzh": [157, 1], "whdssh": [157, 3], "whhy": [157, 12], "whhczh": [213, 5],
    "whrczh": [77, 10], "whrcsh": [77, 11], "whrszh": [143, 8], "whrssh": [143, 9],
    "whwd1": [91, 7], "whwd2": [91, 8],
    # 新增
    "sgzh": [279, 1], "sgsc": [279, 3],  # 寿光综合、寿光蔬菜
    "jzxw": [423, 1], "jzzh": [423, 3], "jzyl": [423, 5],  # 胶州新闻/综合/娱乐
    "dzxwzh2": [519, 18], "dzjjsh2": [519, 20], "dztw2": [519, 22],  # 德州(备用源)
    "hdsh": [672, 1], "hdzh": [672, 3],  # 黄岛生活/综合
}

SDLY_API_URL = "https://app.litenews.cn/v1/app/play/tv/live"
SDLY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://v.iqilu.com/",
    "Origin": "https://v.iqilu.com",
}

# orgid 到频道列表的缓存
_org_cache: dict[int, dict[str, Any]] = {}


async def resolve_sdly(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    mapping = SDLY_CHANNELS.get(channel_key)
    if not mapping:
        # 支持 orgid:ch_id 格式
        if ":" in channel_key:
            parts = channel_key.split(":", 1)
            if parts[0].isdigit() and parts[1].isdigit():
                orgid = int(parts[0])
                ch_id = int(parts[1])
            else:
                raise AdapterResolveError("invalid_sdly_channel_id", f"无效格式: {channel_key}")
        elif channel_key.isdigit():
            orgid = int(channel_key)
            ch_id = 0  # 0 表示取第一个频道
        else:
            supported = ", ".join(list(SDLY_CHANNELS.keys())[:20])
            raise AdapterResolveError(
                "invalid_sdly_channel_id",
                f"不支持的山东区县频道: {channel_key}",
            )
    else:
        orgid = mapping[0]
        ch_id = mapping[1]

    # 请求频道列表
    try:
        resp = await client.get(
            SDLY_API_URL,
            params={"_orgid_": str(orgid)},
            headers=SDLY_HEADERS,
            follow_redirects=True,
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "sdly_request_failed",
            f"山东区县接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    data = resp.json()
    stream = None
    for v in data.get("data", []):
        if ch_id == 0:
            stream = v.get("stream", "")
            break
        if v.get("id") == ch_id:
            stream = v.get("stream", "")
            break

    if not stream:
        raise AdapterResolveError(
            "sdly_no_stream",
            f"山东区县频道 {channel_key} 未找到播放地址",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "sdly",
        "source_type": "hls",
        "url": stream,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": 30 * 60,
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_key,
        "channel_name": channel_key,
        "volatile_url": True,
    }
