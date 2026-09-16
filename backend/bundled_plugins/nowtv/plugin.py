#!/usr/bin/env python3
"""NOW TV Provider implemented with the public WaveFlow Plugin SDK."""
from waveflow_plugin_sdk import PluginApplication, ResolveContext, StreamDescriptor, TVProvider, TVReference, TemporaryFailure

API_URL = "https://webtvapi.now.com/10/7/getLiveURL"
USER_AGENT = "NNC/6.3.0 (com.now.news; build:2309121224; iOS 17.1.0) Alamofire/5.2.2"
CHANNELS = {"NEWS": ("331", "NOW 新闻台"), "FINANCE": ("332", "NOW 财经台"), "LIVE": ("333", "NOW 直播新闻台")}


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        resource = reference.resource_id.strip("/").upper()
        channel = CHANNELS.get(resource)
        if channel is None:
            if resource.isdigit():
                channel = (resource, f"NOW CH{resource}")
            else:
                from waveflow_plugin_sdk import InvalidResource
                raise InvalidResource("NOW TV channel is not supported")
        body = {"deviceType": "IOS_PHONE", "contentId": channel[0], "audioCode": "A",
                "deviceId": "8269809F-7702-45CE-9378-D7157A2E6819", "mode": "prod",
                "callerReferenceNo": "20140702122500", "contentType": "Channel"}
        try:
            response = context.capabilities.managed_http(API_URL, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                json_body=body, response_mode="json", timeout=10)
        except TemporaryFailure:
            raise
        except Exception as exc:
            from waveflow_plugin_sdk import PluginError
            if isinstance(exc, PluginError) and exc.code in {
                "CAPABILITY_DENIED", "PLUGIN_TIMEOUT", "AUTH_FAILED", "RATE_LIMITED",
                "TEMPORARY_UPSTREAM_FAILURE",
            }:
                raise
            raise TemporaryFailure("NOW TV upstream request failed") from None
        data = response.body
        if not isinstance(data, dict) or data.get("responseCode") != "SUCCESS":
            raise TemporaryFailure(f"NOW TV {channel[1]} returned no success")
        assets = data.get("asset")
        if not isinstance(assets, list) or not assets or not isinstance(assets[0], str) or not assets[0].startswith(("http://", "https://")):
            raise TemporaryFailure(f"NOW TV {channel[1]} returned no playable asset")
        return StreamDescriptor.hls(assets[0], ttl_seconds=300, volatile_url=True, requires_proxy=True)


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/nowtv")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "nowtv", Provider()
    ).run()


if __name__ == "__main__":
    main()
