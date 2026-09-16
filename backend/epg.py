"""EPG XMLTV 拉取、解析、匹配、存储"""

import asyncio
import gzip
import json
import logging
import os
import re
import tempfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx

from m3u8_parser import normalize_channel_name
from epg_source_model import BUILTIN_EPG_SOURCE_PRESETS

logger = logging.getLogger("waveflow.epg")

# 内置默认 EPG 源
DEFAULT_EPG_SOURCES = [
    {"builtin_key": preset.key, "name": preset.name, "url": preset.url}
    for preset in BUILTIN_EPG_SOURCE_PRESETS
]

EPG_DOWNLOAD_TIMEOUT_SECONDS = 30.0
EPG_MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
EPG_MAX_DECOMPRESSED_BYTES = 128 * 1024 * 1024
EPG_IO_CHUNK_SIZE = 64 * 1024
EPG_XML_SNIFF_BYTES = 8 * 1024
EPG_ERROR_MAX_LENGTH = 1024

_epg_source_refresh_locks: dict[int, asyncio.Lock] = {}


class EpgRefreshError(Exception):
    """可安全展示和持久化的 EPG 刷新错误。"""


@dataclass
class EpgParseResult:
    channels: list[dict] = field(default_factory=list)
    programmes: list[dict] = field(default_factory=list)
    parsed_channel_count: int = 0
    parsed_programme_count: int = 0
    skipped_programme_count: int = 0
    invalid_time_count: int = 0
    missing_channel_count: int = 0
    data_start_at: str = ''
    data_end_at: str = ''
    warnings: list[str] = field(default_factory=list)


@dataclass
class XmltvDownload:
    path: str
    downloaded_bytes: int
    decompressed_bytes: int
    temp_paths: tuple[str, ...]

    def cleanup(self) -> None:
        for path in self.temp_paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        hostname = parts.hostname or ''
        if not hostname:
            return ''
        if ':' in hostname and not hostname.startswith('['):
            hostname = f'[{hostname}]'
        try:
            port = parts.port
        except ValueError:
            port = None
        netloc = hostname if port is None else f'{hostname}:{port}'
        return urlunsplit((parts.scheme, netloc, parts.path, '', ''))
    except Exception:
        return ''


def _sanitize_error(error: BaseException | str) -> str:
    text = str(error).replace('\x00', ' ').strip()
    urls: list[str] = []

    def _hide_url(match) -> str:
        urls.append(_public_url(match.group(0)))
        return f'__EPG_URL_{len(urls) - 1}__'

    text = re.sub(r'https?://[^\s]+', _hide_url, text)
    text = re.sub(r'(?<!\w)(?:[A-Za-z]:)?/(?:[^\s/:]+/)*[^\s/:]+', '[path]', text)
    for index, url in enumerate(urls):
        text = text.replace(f'__EPG_URL_{index}__', url)
    text = re.sub(r'\s+', ' ', text)
    return text[:EPG_ERROR_MAX_LENGTH] or 'EPG 刷新失败'


def sanitize_epg_error(error: BaseException | str) -> str:
    """Return the bounded URL-redacted EPG diagnostic contract."""
    return _sanitize_error(error)


def _stop_requested(stop_event) -> bool:
    if stop_event is None:
        return False
    if callable(stop_event):
        return bool(stop_event())
    is_set = getattr(stop_event, 'is_set', None)
    return bool(is_set and is_set())


def _raise_if_stopped(stop_event) -> None:
    if _stop_requested(stop_event):
        raise asyncio.CancelledError


def parse_xmltv_time(raw: str) -> str | None:
    """解析带明确时区的 XMLTV/ISO 时间并统一返回 UTC ISO 8601。"""
    value = (raw or '').strip()
    if not value:
        return None

    compact = re.fullmatch(r'(\d{12}|\d{14})\s*([+-]\d{4})', value)
    if compact:
        timestamp, offset = compact.groups()
        pattern = '%Y%m%d%H%M%S %z' if len(timestamp) == 14 else '%Y%m%d%H%M %z'
        try:
            parsed = datetime.strptime(f'{timestamp} {offset}', pattern)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            return None

    iso_value = value[:-1] + '+00:00' if value.endswith(('Z', 'z')) else value
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _local_tag(element: ET.Element) -> str:
    return element.tag.rsplit('}', 1)[-1]


def _parse_xmltv_stream(stream) -> EpgParseResult:
    channels: list[dict] = []
    programme_candidates: list[dict] = []
    skipped = 0
    invalid_time = 0

    try:
        for _, element in ET.iterparse(stream, events=('end',)):
            tag = _local_tag(element)
            if tag == 'channel':
                channel_id = (element.get('id') or '').strip()
                display_names = [
                    child.text.strip()
                    for child in element
                    if _local_tag(child) == 'display-name' and child.text and child.text.strip()
                ]
                if channel_id and display_names:
                    normalized = [normalize_channel_name(name) for name in display_names]
                    channels.append({
                        'channel_id': channel_id,
                        'display_names': json.dumps(display_names, ensure_ascii=False),
                        'normalized_names': json.dumps(normalized, ensure_ascii=False),
                    })
            elif tag == 'programme':
                channel_id = (element.get('channel') or '').strip()
                start = parse_xmltv_time(element.get('start') or '')
                stop = parse_xmltv_time(element.get('stop') or '')
                title = ''
                description = ''
                for child in element:
                    child_tag = _local_tag(child)
                    if child_tag == 'title' and not title and child.text:
                        title = child.text.strip()
                    elif child_tag == 'desc' and not description and child.text:
                        description = child.text.strip()
                if not start or not stop:
                    skipped += 1
                    invalid_time += 1
                elif stop <= start or not channel_id or not title:
                    skipped += 1
                else:
                    programme_candidates.append({
                        'channel_id': channel_id,
                        'start': start,
                        'stop': stop,
                        'title': title,
                        'description': description,
                    })
            if tag in {'channel', 'programme'}:
                element.clear()
    except (ET.ParseError, OSError, UnicodeError) as error:
        raise EpgRefreshError(f'XMLTV 解析失败: {error}') from error

    channel_ids = {item['channel_id'] for item in channels}
    programmes = []
    missing_channel = 0
    for item in programme_candidates:
        if item['channel_id'] not in channel_ids:
            skipped += 1
            missing_channel += 1
            continue
        programmes.append(item)

    warnings = []
    attempted_programmes = len(programmes) + skipped
    if attempted_programmes and skipped / attempted_programmes >= 0.9:
        warnings.append('大部分 programme 因时间或频道关联无效而被跳过')

    starts = [item['start'] for item in programmes]
    stops = [item['stop'] for item in programmes]
    result = EpgParseResult(
        channels=channels,
        programmes=programmes,
        parsed_channel_count=len(channels),
        parsed_programme_count=len(programmes),
        skipped_programme_count=skipped,
        invalid_time_count=invalid_time,
        missing_channel_count=missing_channel,
        data_start_at=min(starts) if starts else '',
        data_end_at=max(stops) if stops else '',
        warnings=warnings,
    )
    logger.info(
        'XMLTV 解析: %d channels, %d programmes, %d skipped',
        result.parsed_channel_count,
        result.parsed_programme_count,
        result.skipped_programme_count,
    )
    return result


def parse_xmltv(xml_text: str) -> EpgParseResult:
    """解析内存中的 XMLTV 文本并返回结构化结果。"""
    return _parse_xmltv_stream(StringIO(xml_text))


def parse_xmltv_file(path: str) -> EpgParseResult:
    """使用 iterparse 解析下载后的 XMLTV 临时文件。"""
    with open(path, 'rb') as stream:
        return _parse_xmltv_stream(stream)


def _is_xmltv_payload_prefix(prefix: bytes) -> bool:
    """Recognize an XMLTV root without trusting URL or response metadata."""
    candidate = prefix
    if candidate.startswith(b'\xef\xbb\xbf'):
        candidate = candidate[3:]
    candidate = candidate.lstrip(b' \t\r\n')
    if not candidate:
        return False

    parser = ET.XMLPullParser(events=('start',))
    try:
        parser.feed(candidate)
        for _, element in parser.read_events():
            return _local_tag(element) == 'tv'
    except (ET.ParseError, UnicodeError):
        return False
    return False


async def download_xmltv(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = EPG_DOWNLOAD_TIMEOUT_SECONDS,
    max_download_bytes: int = EPG_MAX_DOWNLOAD_BYTES,
    max_decompressed_bytes: int = EPG_MAX_DECOMPRESSED_BYTES,
    stop_event=None,
    temp_dir: str | None = None,
) -> XmltvDownload:
    """流式下载 XMLTV，并在有界条件下解压到临时文件。"""
    if max_download_bytes <= 0 or max_decompressed_bytes <= 0:
        raise ValueError('EPG 下载和解压上限必须大于 0')

    temp_paths: list[str] = []
    raw_path = ''
    try:
        raw_fd, raw_path = tempfile.mkstemp(prefix='waveflow-epg-', suffix='.download', dir=temp_dir)
        os.close(raw_fd)
        temp_paths.append(raw_path)
        downloaded_bytes = 0
        async with client.stream('GET', url, follow_redirects=True, timeout=timeout) as response:
            if response.status_code >= 400:
                raise EpgRefreshError(f'EPG 下载失败: HTTP {response.status_code}')
            content_length = response.headers.get('content-length')
            if content_length:
                try:
                    if int(content_length) > max_download_bytes:
                        raise EpgRefreshError('EPG 下载大小超过限制')
                except ValueError:
                    pass
            with open(raw_path, 'wb') as output:
                async for chunk in response.aiter_bytes(EPG_IO_CHUNK_SIZE):
                    _raise_if_stopped(stop_event)
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > max_download_bytes:
                        raise EpgRefreshError('EPG 下载大小超过限制')
                    output.write(chunk)
        _raise_if_stopped(stop_event)
        with open(raw_path, 'rb') as stream:
            prefix = stream.read(EPG_XML_SNIFF_BYTES)
        # httpx's aiter_bytes() may already decode Content-Encoding. Classify
        # the bytes we received instead of trusting URL or header metadata.
        is_gzip = prefix[:2] == b'\x1f\x8b'
        if not is_gzip and not _is_xmltv_payload_prefix(prefix):
            raise EpgRefreshError('EPG payload 不是有效 XMLTV')
        if not is_gzip:
            if downloaded_bytes > max_decompressed_bytes:
                raise EpgRefreshError('EPG 解压大小超过限制')
            return XmltvDownload(raw_path, downloaded_bytes, downloaded_bytes, tuple(temp_paths))

        xml_fd, xml_path = tempfile.mkstemp(prefix='waveflow-epg-', suffix='.xml', dir=temp_dir)
        os.close(xml_fd)
        temp_paths.append(xml_path)
        decompressed_bytes = 0
        try:
            with gzip.open(raw_path, 'rb') as compressed, open(xml_path, 'wb') as output:
                while True:
                    _raise_if_stopped(stop_event)
                    chunk = compressed.read(EPG_IO_CHUNK_SIZE)
                    if not chunk:
                        break
                    decompressed_bytes += len(chunk)
                    if decompressed_bytes > max_decompressed_bytes:
                        raise EpgRefreshError('EPG 解压大小超过限制')
                    output.write(chunk)
        except (gzip.BadGzipFile, EOFError, OSError, zlib.error) as error:
            raise EpgRefreshError('EPG gzip 数据无效') from error
        return XmltvDownload(xml_path, downloaded_bytes, decompressed_bytes, tuple(temp_paths))
    except asyncio.CancelledError:
        for path in temp_paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        raise
    except BaseException:
        for path in temp_paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        raise


async def fetch_xmltv(client: httpx.AsyncClient, url: str) -> str | None:
    """兼容旧调用：使用有界下载读取 XMLTV 文本。"""
    download = None
    try:
        download = await download_xmltv(client, url)
        with open(download.path, encoding='utf-8', errors='replace') as stream:
            return stream.read()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning('拉取 EPG 失败 %s: %s', _public_url(url), _sanitize_error(error))
        return None
    finally:
        if download is not None:
            download.cleanup()


async def store_epg(source_id: int, channels: list[dict], programmes: list[dict]):
    """兼容旧调用：使用来源当前 revision 原子替换数据集。"""
    import database as db

    source = await db.get_epg_source(source_id)
    if source is None:
        raise EpgRefreshError('EPG 来源不存在')
    starts = [item['start'] for item in programmes]
    stops = [item['stop'] for item in programmes]
    now = _utc_now()
    result = await db.replace_epg_dataset_atomic(
        source_id,
        int(source['revision']),
        channels,
        programmes,
        stats={
            'channel_count': len(channels),
            'programme_count': len(programmes),
            'data_start_at': min(starts) if starts else '',
            'data_end_at': max(stops) if stops else '',
            'attempted_at': now,
            'finished_at': now,
        },
    )
    if not result['committed']:
        raise EpgRefreshError('EPG 来源已变化，数据集未写入')


def _source_result(source: dict, started_at: str) -> dict:
    return {
        'source_id': source['id'],
        'source_name': source.get('name', ''),
        'requested_url': _public_url(source.get('url', '')),
        'current_url': _public_url(source.get('url', '')),
        'source_revision': int(source.get('revision') or 1),
        'status': 'failed',
        'usable_dataset': False,
        'stale': False,
        'downloaded_bytes': 0,
        'channel_count': 0,
        'programme_count': 0,
        'skipped_programme_count': 0,
        'data_start_at': '',
        'data_end_at': '',
        'cache_updated': False,
        'error': '',
        'started_at': started_at,
        'finished_at': '',
    }


def _finish_source_result(result: dict, *, status: str, error: str = '') -> dict:
    result['status'] = status
    result['usable_dataset'] = status == 'success'
    result['stale'] = status == 'stale'
    result['error'] = _sanitize_error(error) if error else ''
    result['finished_at'] = _utc_now()
    return result


def _dataset_error(parsed: EpgParseResult) -> str:
    if parsed.parsed_channel_count <= 0:
        return 'EPG 数据集没有有效频道'
    if parsed.parsed_programme_count <= 0:
        return 'EPG 数据集没有有效节目'
    if not parsed.data_start_at or not parsed.data_end_at:
        return 'EPG 数据集缺少有效时间范围'
    return ''


async def _current_source_identity(source: dict) -> tuple[str, dict | None]:
    import database as db

    current = await db.get_epg_source(source['id'])
    if current is None:
        return 'revision_discarded', None
    if (
        int(current.get('revision') or 1) != int(source.get('revision') or 1)
        or current.get('url', '') != source.get('url', '')
    ):
        return 'revision_discarded', current
    if not current.get('enabled'):
        return ('disabled' if not source.get('enabled') else 'revision_discarded'), current
    return '', current


async def refresh_epg_source(
    source: dict,
    client: httpx.AsyncClient,
    *,
    stop_event=None,
) -> dict:
    """安全刷新单个来源，并返回属于本次调用的结构化结果。"""
    import database as db

    source_id = int(source['id'])
    expected_revision = int(source.get('revision') or 1)
    lock = _epg_source_refresh_locks.setdefault(source_id, asyncio.Lock())
    async with lock:
        started_at = _utc_now()
        result = _source_result(source, started_at)
        download = None
        parsed = None
        try:
            _raise_if_stopped(stop_event)
            identity_status, current = await _current_source_identity(source)
            result['current_url'] = _public_url(current.get('url', '')) if current else ''
            if identity_status == 'disabled':
                await db.record_epg_source_disabled(
                    source_id,
                    expected_revision,
                    attempted_at=started_at,
                )
                return _finish_source_result(result, status='disabled')
            if identity_status:
                return _finish_source_result(result, status='revision_discarded')

            began = await db.begin_epg_source_refresh(
                source_id,
                expected_revision,
                attempted_at=started_at,
            )
            if not began:
                identity_status, current = await _current_source_identity(source)
                result['current_url'] = _public_url(current.get('url', '')) if current else ''
                return _finish_source_result(
                    result,
                    status=identity_status or 'revision_discarded',
                )

            logger.info('刷新 EPG 源: %s', result['requested_url'])
            download = await download_xmltv(
                client,
                source['url'],
                stop_event=stop_event,
            )
            result['downloaded_bytes'] = download.downloaded_bytes
            _raise_if_stopped(stop_event)

            identity_status, current = await _current_source_identity(source)
            result['current_url'] = _public_url(current.get('url', '')) if current else ''
            if identity_status:
                return _finish_source_result(result, status='revision_discarded')

            parsed = await asyncio.to_thread(parse_xmltv_file, download.path)
            result.update({
                'channel_count': parsed.parsed_channel_count,
                'programme_count': parsed.parsed_programme_count,
                'skipped_programme_count': parsed.skipped_programme_count,
                'data_start_at': parsed.data_start_at,
                'data_end_at': parsed.data_end_at,
            })
            _raise_if_stopped(stop_event)

            identity_status, current = await _current_source_identity(source)
            result['current_url'] = _public_url(current.get('url', '')) if current else ''
            if identity_status:
                return _finish_source_result(result, status='revision_discarded')

            validation_error = _dataset_error(parsed)
            if validation_error:
                raise EpgRefreshError(validation_error)

            finished_at = _utc_now()
            commit_result = await db.replace_epg_dataset_atomic(
                source_id,
                expected_revision,
                parsed.channels,
                parsed.programmes,
                stats={
                    'channel_count': parsed.parsed_channel_count,
                    'programme_count': parsed.parsed_programme_count,
                    'data_start_at': parsed.data_start_at,
                    'data_end_at': parsed.data_end_at,
                    'attempted_at': started_at,
                    'finished_at': finished_at,
                },
            )
            if not commit_result['committed']:
                current = commit_result.get('source')
                result['current_url'] = _public_url(current.get('url', '')) if current else ''
                return _finish_source_result(result, status='revision_discarded')
            result['cache_updated'] = True
            result['finished_at'] = finished_at
            result['status'] = 'success'
            result['usable_dataset'] = True
            logger.info(
                'EPG 来源刷新完成: %s, %d channels, %d programmes',
                result['requested_url'],
                result['channel_count'],
                result['programme_count'],
            )
            return result
        except asyncio.CancelledError:
            identity_status, _ = await _current_source_identity(source)
            if not identity_status:
                await db.record_epg_source_refresh_failure(
                    source_id,
                    expected_revision,
                    status='cancelled',
                    attempted_at=started_at,
                    error='EPG 刷新已取消',
                )
            raise
        except Exception as error:
            identity_status, current = await _current_source_identity(source)
            result['current_url'] = _public_url(current.get('url', '')) if current else ''
            if identity_status:
                return _finish_source_result(result, status='revision_discarded')
            has_dataset = await db.has_epg_dataset(source_id)
            status = 'stale' if has_dataset else 'failed'
            safe_error = _sanitize_error(error)
            await db.record_epg_source_refresh_failure(
                source_id,
                expected_revision,
                status=status,
                attempted_at=started_at,
                error=safe_error,
            )
            logger.warning('EPG 来源刷新失败 %s: %s', result['requested_url'], safe_error)
            return _finish_source_result(result, status=status, error=safe_error)
        finally:
            if download is not None:
                download.cleanup()


async def ensure_default_epg_sources() -> list[dict]:
    """Ensure all persisted WaveFlow presets without doing network I/O."""
    from epg_source_management import ensure_builtin_epg_sources

    return await ensure_builtin_epg_sources()


async def run_epg_refresh_maintenance(*, trigger: str = 'epg_refresh') -> dict:
    """Run the existing binding lifecycle after a committed EPG dataset.

    Dataset commit and maintenance deliberately remain separate transactions.
    A maintenance failure is returned as degradation and never rolls back the
    already committed source dataset.
    """
    from epg_maintenance import run_epg_binding_maintenance
    result = await run_epg_binding_maintenance(
        sync_logical=True,
        trigger=trigger,
    )
    if result['status'] != 'success':
        logger.warning('EPG binding maintenance deferred: %s', _sanitize_error(result['error']))
    return result


async def refresh_epg_sources(
    client: httpx.AsyncClient | None = None,
    *,
    stop_event=None,
) -> dict:
    """串行编排所有启用来源，并汇总本轮结构化结果。"""
    import database as db

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=EPG_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True)

    source_results: list[dict] = []
    maintenance_error = ''
    maintenance_result = None
    try:
        sources = await ensure_default_epg_sources()
        active = [source for source in sources if source['enabled']]

        for source in active:
            _raise_if_stopped(stop_event)
            source_results.append(await refresh_epg_source(source, client, stop_event=stop_event))

        success_count = sum(result['status'] == 'success' for result in source_results)
        if success_count:
            try:
                maintenance_result = await run_epg_refresh_maintenance(trigger='epg_refresh')
                if maintenance_result['status'] != 'success':
                    maintenance_error = _sanitize_error(
                        maintenance_result.get('error') or 'EPG binding maintenance degraded'
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                maintenance_error = f'EPG binding maintenance 失败: {_sanitize_error(error)}'
                logger.warning(maintenance_error)

        statuses = ('success', 'stale', 'failed', 'revision_discarded', 'disabled')
        counts = {
            status: sum(result['status'] == status for result in source_results)
            for status in statuses
        }
        if source_results and success_count == len(source_results):
            refresh_status = 'success'
        elif success_count > 0:
            refresh_status = 'partial'
        else:
            refresh_status = 'failed'

        errors = [result['error'] for result in source_results if result['error']]
        if maintenance_error:
            errors.append(maintenance_error)
            if refresh_status == 'success':
                refresh_status = 'partial'
        if not source_results:
            errors.append('没有可刷新的 EPG 来源')
        return {
            'refresh_status': refresh_status,
            'source_results': source_results,
            'source_result_counts': counts,
            'binding_maintenance': maintenance_result,
            **{
                f'{status}_source_ids': [
                    result['source_id'] for result in source_results if result['status'] == status
                ]
                for status in statuses
            },
            'channel_count': sum(
                result['channel_count'] for result in source_results if result['status'] == 'success'
            ),
            'programme_count': sum(
                result['programme_count'] for result in source_results if result['status'] == 'success'
            ),
            'skipped_programme_count': sum(
                result['skipped_programme_count'] for result in source_results
            ),
            'error': '; '.join(errors)[:EPG_ERROR_MAX_LENGTH],
        }
    finally:
        if owns_client:
            await client.aclose()
