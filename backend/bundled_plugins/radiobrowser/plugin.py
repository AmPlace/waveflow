"""RadioBrowser provider using explicit upstream filters and UUID identities."""
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from waveflow_plugin_sdk import InvalidResource, PluginApplication, PluginError, RadioProvider, StreamDescriptor


API_BASE = "https://all.api.radio-browser.info/json"
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 250
MAX_QUERY_LENGTH = 160
MAX_CURSOR = 100_000


def _station_id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise InvalidResource("Invalid RadioBrowser station UUID") from None


def _http_url(value):
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password else ""
    except ValueError:
        return ""


def _rows(path, context):
    context.raise_if_cancelled()
    response = context.capabilities.managed_http(
        API_BASE + path, response_mode="json", timeout=15,
        headers={"User-Agent": "WaveFlow-RadioBrowser/1.0", "Accept": "application/json"},
    )
    if response.status != 200 or not isinstance(response.body, list):
        raise PluginError("TEMPORARY_UPSTREAM_FAILURE", "RadioBrowser catalog is unavailable", retryable=True, category="provider")
    return response.body


def _optional_text(payload, key, *, limit=MAX_QUERY_LENGTH, upper=False):
    value = payload.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidResource(f"RadioBrowser {key} must be text")
    value = value.strip()
    if len(value) > limit:
        raise InvalidResource(f"RadioBrowser {key} is too long")
    return value.upper() if upper else value


def _catalog_request(payload):
    if not isinstance(payload, dict):
        raise InvalidResource("RadioBrowser catalog payload must be an object")
    country = _optional_text(payload, "country", limit=2, upper=True)
    if country and (len(country) != 2 or not country.isalpha()):
        raise InvalidResource("RadioBrowser country must be an ISO country code")
    query = _optional_text(payload, "query")
    tag = _optional_text(payload, "tag")
    language = _optional_text(payload, "language")
    cursor = payload.get("cursor", "0")
    try:
        offset = int(str(cursor or "0"), 10)
    except ValueError as error:
        raise InvalidResource("RadioBrowser cursor must be a decimal offset") from error
    if offset < 0 or offset > MAX_CURSOR:
        raise InvalidResource("RadioBrowser cursor is outside the supported range")
    page_size = payload.get("page_size", DEFAULT_PAGE_SIZE)
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise InvalidResource(f"RadioBrowser page_size must be 1..{MAX_PAGE_SIZE}")
    params = {
        "hidebroken": "true",
        "order": "votes",
        "reverse": "true",
        "limit": page_size,
        "offset": offset,
    }
    if country:
        params["countrycode"] = country
    if query:
        params["name"] = query
    if tag:
        params["tag"] = tag
    if language:
        params["language"] = language
    return params, offset, page_size


def _tags(value):
    if not isinstance(value, str):
        return []
    return [tag.strip() for tag in value.split(",") if tag.strip()]


class Provider(RadioProvider):
    def catalog(self, payload, context):
        params, offset, page_size = _catalog_request(payload)
        rows = _rows("/stations/search?" + urlencode(params), context)
        stations, seen = [], set()
        for row in rows[:page_size]:
            context.raise_if_cancelled()
            if not isinstance(row, dict) or not _http_url(row.get("url_resolved")) or not row.get("name"):
                continue
            try:
                station_id = _station_id(row.get("stationuuid"))
            except InvalidResource:
                continue
            if station_id in seen:
                continue
            seen.add(station_id)
            stations.append({
                "station_ref": {"provider_key": "radiobrowser", "provider_station_id": station_id},
                "name": str(row["name"]), "logo_url": _http_url(row.get("favicon")),
                "country": str(row.get("countrycode") or ""), "group_name": str(row.get("state") or ""),
                "language": str(row.get("languagecodes") or row.get("language") or ""),
                "metadata": {
                    "tags": _tags(row.get("tags")),
                    "codec": str(row.get("codec") or ""),
                    "bitrate": row.get("bitrate") if isinstance(row.get("bitrate"), int) else None,
                    "homepage": _http_url(row.get("homepage")),
                },
                "ttl_seconds": 7200,
            })
        if rows and not stations:
            raise PluginError("TEMPORARY_UPSTREAM_FAILURE", "RadioBrowser returned no valid station records", retryable=True, category="provider")
        next_cursor = str(offset + page_size) if len(rows) >= page_size else None
        return {"stations": stations, "next_cursor": next_cursor}

    def resolve_stream(self, reference, context):
        if reference.provider_key != "radiobrowser":
            raise InvalidResource("Unsupported RadioBrowser reference")
        station_id = _station_id(reference.provider_station_id)
        rows = _rows("/stations/byuuid/" + station_id, context)
        row = next((item for item in rows if isinstance(item, dict) and str(item.get("stationuuid")) == station_id), None)
        if row is None or not _http_url(row.get("url_resolved")):
            raise InvalidResource("RadioBrowser station has no playable URL")
        if row.get("hls") not in (0, 1):
            raise InvalidResource("RadioBrowser station has no explicit transport flag")
        return StreamDescriptor(
            url=row["url_resolved"], transport="hls" if row["hls"] == 1 else "audio_http",
            ttl_seconds=300, volatile_url=True, requires_proxy=True,
        )


def main():
    identity, version = PluginApplication.identity_args("org.waveflow/radiobrowser")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_radio("radiobrowser", Provider()).run()


if __name__ == "__main__":
    main()
