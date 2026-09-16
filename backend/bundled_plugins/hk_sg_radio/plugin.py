"""Explicit catalog extracted from WaveFlow's legacy HK/SG stations."""
import hashlib
import json

from waveflow_plugin_sdk import InvalidResource, PluginApplication, RadioProvider, StreamDescriptor
from waveflow_plugin_sdk.resources import load_resource_text


def _revision(station):
    return hashlib.sha256(json.dumps(station["stream"], sort_keys=True).encode()).hexdigest()


class Provider(RadioProvider):
    def __init__(self):
        data = json.loads(load_resource_text("stations.json", anchor=__file__))
        self.stations = {station["id"]: station for station in data["stations"]}

    def catalog(self, payload, context):
        return {"stations": [{
            "station_ref": {"provider_key": "hksgradio", "provider_station_id": station["id"]},
            "name": station["name"], "logo_url": station["logoUrl"],
            "country": station["tags"][0],
            "metadata": {"tag": station["tags"][1], "subtitle": station["subtitle"], "legacy_station_id": station["id"]},
            "playback_config": {"stream_revision": _revision(station)},
            "ttl_seconds": 7 * 24 * 60 * 60,
        } for station in self.stations.values()]}

    def resolve_stream(self, reference, context):
        station = self.stations.get(reference.provider_station_id)
        if reference.provider_key != "hksgradio" or station is None:
            raise InvalidResource("Unknown HK/SG station")
        revision = reference.playback_config.get("stream_revision")
        if revision and revision != _revision(station):
            raise InvalidResource("Station stream changed; refresh the catalog")
        stream = station["stream"]
        return StreamDescriptor(
            url=stream["url"], transport=stream["transport"], headers=dict(stream["headers"]),
            ttl_seconds=3600, volatile_url=False, requires_proxy=True,
        )


def main():
    identity, version = PluginApplication.identity_args("org.waveflow/hk-sg-radio")
    PluginApplication(identity=identity, version=version).register_radio("hksgradio", Provider()).run()


if __name__ == "__main__":
    main()
