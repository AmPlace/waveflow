#!/usr/bin/env python3
"""Independent SDLY Provider using the generic managed HTTP capability."""
from __future__ import annotations

import json
from typing import Any

from waveflow_plugin_sdk import InvalidResource, PluginApplication, PluginError, ResolveContext, StreamDescriptor, TVProvider, TVReference


# [orgid, channel_id], kept as the legacy source/reference map.
SDLY_CHANNELS: dict[str, list[int]] = {
    "jncqxw": [171, 2], "jncqsh": [171, 20], "jnjrtv": [303, 1], "jnjyzh": [85, 1],
    "jnjyys": [85, 2], "jnlcxw": [261, 1], "jnpyzh": [257, 1], "jnpyxc": [257, 3],
    "jnshzh": [97, 1], "jnshys": [97, 2], "jnzqzh": [195, 1], "jnzqgg": [195, 2],
    "dyxwzh": [537, 1], "dygg": [537, 3], "dygg2": [29, 90], "dykj": [537, 7],
    "dydyqxw": [163, 5], "dydyqkj": [163, 7], "dygrzh": [237, 1], "dygrkj": [237, 5],
    "dyklxw": [269, 3], "dyljzh": [153, 1], "dyljwh": [153, 3],
    "qdcyzh": [403, 5], "qdhdzh": [227, 1], "qdhdsh": [227, 3], "qdjmzh": [221, 2],
    "qdjmsh": [221, 3], "qdjzzh": [305, 1], "qdjzsh": [305, 3], "qdlc": [173, 1],
    "qdls": [295, 1], "qdlxzh": [253, 1], "qdlxsh": [253, 3], "qdpdxw": [45, 4],
    "qdpdsh": [45, 5],
    "wfxwzh": [635, 1], "wfsh": [635, 5], "wfyswy": [635, 7], "wfkjwl": [635, 9],
    "wfgxq": [421, 14], "wfaqzh": [137, 3], "wfaqms": [137, 4], "wfbhxw": [199, 1],
    "wfclzh": [1, 3], "wfcyzh": [47, 1], "wfcyjj": [47, 2], "wffzxw": [285, 1],
    "wfgmzh": [71, 24], "wfgmdj": [71, 38], "wfhtxw": [133, 1], "wfkwtv": [127, 17],
    "wflqxw": [205, 39], "wfqzzh": [125, 2], "wfqzwh": [125, 3], "wfwc": [15, 3],
    "wfzcxw": [115, 23], "wfzcsh": [115, 25],
    "ytcd": [175, 1], "ytfszh": [189, 4], "ytfssh": [189, 5], "ythyzh": [255, 1],
    "ytlkzh": [57, 1], "ytlksh": [57, 2], "ytlszh": [245, 4], "ytlsys": [245, 6],
    "ytlyzh": [241, 4], "ytlyms": [241, 7], "ytlzzh": [239, 1], "ytmpzh": [281, 1],
    "ytplzh": [109, 1], "ytplzy": [109, 2], "ytqxzh": [165, 12], "ytqxpg": [165, 14],
    "ytzyzh": [55, 2], "ytzyzy": [55, 4],
    "zbbsxw": [17, 8], "zbbstw": [17, 9], "zbgqzh": [61, 1], "zbgqys": [61, 2],
    "zbht1": [23, 15], "zbht2": [23, 16], "zblzxw": [151, 6], "zblzsh": [151, 7],
    "zbyyzh": [203, 6], "zbyysh": [203, 7], "zbzcxw": [75, 1], "zbzcsh": [75, 2],
    "zbzd1": [101, 1], "zbzd2": [101, 6], "zbzctv1": [259, 1], "zbzctv2": [259, 3],
    "zzstzh": [243, 1], "zzszzh": [233, 1], "zztezxw": [185, 2], "zztzzh": [103, 2],
    "zztzms": [103, 3], "zzxcxw": [37, 8], "zzyczh": [209, 1],
    "bzbctv": [249, 35], "bzbxzh": [207, 3], "bzbxsh": [207, 4], "bzhmzh": [211, 2],
    "bzhmys": [211, 3], "bzwdzh": [169, 1], "bzwdzy": [169, 21], "bzyxxw": [217, 1],
    "bzzhzh": [277, 1], "bzzhzy": [277, 9], "bzzpzh": [11, 15], "bzzpms": [11, 16],
    "dzxwzh": [179, 1], "dzjjsh": [179, 2], "dztw": [179, 9], "dzlczh": [215, 6],
    "dzllxw": [267, 1], "dzllcs": [267, 5], "dzly1": [49, 3], "dzly2": [49, 4],
    "dznjzh": [193, 1], "dzpyzh": [19, 2], "dzqhzh": [251, 8], "dzqyzh": [5, 9],
    "dzqysh": [5, 7], "dzwczh": [33, 4], "dzwczy": [33, 6], "dzxjzh": [223, 1],
    "dzxjgg": [223, 2], "dzyczh": [235, 1], "dzyczy": [235, 3],
    "hzcwzh": [131, 1], "hzcwzy": [131, 2], "hzcxzh": [87, 2], "hzdmxw": [111, 2],
    "hzdt1": [27, 7], "hzdt2": [27, 8], "hzjczh": [141, 186], "hzjyxw": [139, 1],
    "hzmdxw": [219, 6], "hzmdzy": [219, 17], "hzsxzh": [155, 2], "hzycxw": [135, 3],
    "hzyczy": [135, 2],
    "jijiazh": [273, 1], "jijiash": [273, 3], "jijxzh": [129, 2], "jijxsh": [129, 4],
    "jilszh": [89, 1], "jiqfxw": [13, 1], "jircxw": [73, 8], "jircys": [73, 9],
    "jissxw": [117, 5], "jisswh": [117, 6], "jiws1": [53, 4], "jiws2": [53, 5],
    "jiwszh": [301, 1], "jiytxw": [63, 5], "jiytsh": [63, 15], "jiyzxw": [231, 1],
    "jiyzsh": [231, 3], "jizczh": [181, 1], "jizcwh": [181, 4],
    "lccpzh": [31, 6], "lccpsh": [31, 8], "lcdczh": [265, 1], "lcdezh": [95, 22],
    "lcdezy": [95, 29], "lcgtzh": [43, 1], "lcgtzy": [43, 5], "lcgxzh": [79, 1],
    "lclqzh": [65, 2], "lclqjj": [65, 5], "lcsxzh": [183, 1], "lcsxsh": [183, 5],
    "lcygzh": [81, 1], "lcygys": [81, 10],
    "lyfxzh": [41, 119], "lyfxsh": [41, 117], "lyhdys": [191, 1], "lyhdzh": [191, 2],
    "lyjnzh": [105, 4], "lyjnys": [105, 5], "lyllzh": [113, 131], "lyllgg": [113, 133],
    "lylszh": [201, 1], "lyls1": [167, 3], "lyls2": [167, 4], "lylzzh": [147, 1],
    "lylzys": [147, 17], "lymy1": [161, 13], "lymy2": [161, 15], "lypyzh": [345, 4],
    "lypysh": [345, 14], "lytc1": [83, 1], "lytc2": [83, 2], "lyynzh": [177, 6],
    "lyynys": [177, 7], "lyys1": [145, 1], "lyys2": [145, 2],
    "rzjx1": [159, 23], "rzjx2": [159, 27], "rzls": [289, 1], "rzwlzh": [299, 10],
    "rzwlwh": [299, 12],
    "tadpzh": [187, 9], "tadpms": [187, 11], "tady": [293, 1], "tafczh": [51, 18],
    "tafcsh": [51, 6], "tany1": [123, 1], "tany2": [123, 7], "tats": [263, 1],
    "taxtzh": [59, 2], "taxtxc": [59, 3],
    "whxwzh": [157, 1], "whdssh": [157, 3], "whhy": [157, 12], "whhczh": [213, 5],
    "whrczh": [77, 10], "whrcsh": [77, 11], "whrszh": [143, 8], "whrssh": [143, 9],
    "whwd1": [91, 7], "whwd2": [91, 8],
    "sgzh": [279, 1], "sgsc": [279, 3], "jzxw": [423, 1], "jzzh": [423, 3],
    "jzyl": [423, 5], "dzxwzh2": [519, 18], "dzjjsh2": [519, 20], "dztw2": [519, 22],
    "hdsh": [672, 1], "hdzh": [672, 3],
}
SDLY_API_URL = "https://app.litenews.cn/v1/app/play/tv/live"
SDLY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://v.iqilu.com/",
    "Origin": "https://v.iqilu.com",
}


def _failure(code: str, message: str) -> PluginError:
    return PluginError("TEMPORARY_UPSTREAM_FAILURE", message, retryable=True,
                       category="provider", details={"provider_code": code})


def _target(resource: str) -> tuple[str, int, int]:
    key = resource.strip("/").lower()
    mapping = SDLY_CHANNELS.get(key)
    if mapping:
        return key, mapping[0], mapping[1]
    if ":" in key:
        orgid, channel_id = key.split(":", 1)
        if orgid.isdigit() and channel_id.isdigit():
            return key, int(orgid), int(channel_id)
    if key.isdigit():
        return key, int(key), 0
    raise InvalidResource(f"Unsupported SDLY channel: {key}")


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        channel_key, orgid, channel_id = _target(reference.resource_id)
        try:
            response = context.capabilities.managed_http(
                SDLY_API_URL, query={"_orgid_": str(orgid)}, headers=SDLY_HEADERS,
                response_mode="json", timeout=10,
            )
            data = response.body
            if isinstance(data, str):
                data = json.loads(data)
            if not isinstance(data, dict):
                raise ValueError("SDLY response is not an object")
            stream = ""
            for value in data.get("data", []) if isinstance(data.get("data"), list) else []:
                if not isinstance(value, dict):
                    continue
                if channel_id == 0 or value.get("id") == channel_id:
                    stream = str(value.get("stream") or "")
                    break
            if not stream:
                raise _failure("sdly_no_stream", f"SDLY channel {channel_key} returned no stream")
            if not stream.startswith(("http://", "https://")):
                raise _failure("sdly_no_stream", f"SDLY channel {channel_key} returned an invalid stream")
        except PluginError:
            raise
        except Exception:
            raise _failure("sdly_request_failed", "SDLY upstream response failed") from None
        return StreamDescriptor.hls(stream, ttl_seconds=1800, volatile_url=True, requires_proxy=False)


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/sdly")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "sdly", Provider()
    ).run()


if __name__ == "__main__":
    main()
