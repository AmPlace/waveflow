import asyncio
import gzip
import os
import tempfile
import unittest

import httpx

import epg


def xmltv(channels: str, programmes: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><tv>{channels}{programmes}</tv>'


CHANNELS = '<channel id="cctv1"><display-name>CCTV-1 综合</display-name></channel>'


class _CancellingStream(httpx.AsyncByteStream):
    def __init__(self, stop_event: asyncio.Event):
        self.stop_event = stop_event

    async def __aiter__(self):
        yield b'<?xml version="1.0"?><tv>'
        self.stop_event.set()
        yield b'</tv>'


class _ExplodingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError('Content-Length 超限时不应读取响应体')
        yield b''


class _AlreadyDecodedResponse:
    status_code = 200
    headers = httpx.Headers({'content-encoding': 'gzip', 'content-type': 'application/gzip'})

    def __init__(self, payload: bytes):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def aiter_bytes(self, _chunk_size):
        yield self.payload


class _AlreadyDecodedClient:
    def __init__(self, payload: bytes):
        self.payload = payload

    def stream(self, *_args, **_kwargs):
        return _AlreadyDecodedResponse(self.payload)


class EpgParserTest(unittest.IsolatedAsyncioTestCase):
    def test_standard_xmltv_returns_structured_statistics(self):
        result = epg.parse_xmltv(xmltv(
            CHANNELS,
            '<programme channel="cctv1" start="20260806120000 +0800" stop="20260806130000 +0800">'
            '<title>午间新闻</title><desc>新闻摘要</desc></programme>',
        ))

        self.assertEqual(result.parsed_channel_count, 1)
        self.assertEqual(result.parsed_programme_count, 1)
        self.assertEqual(result.skipped_programme_count, 0)
        self.assertEqual(result.invalid_time_count, 0)
        self.assertEqual(result.missing_channel_count, 0)
        self.assertEqual(result.programmes[0]['start'], '2026-08-06T04:00:00+00:00')
        self.assertEqual(result.programmes[0]['stop'], '2026-08-06T05:00:00+00:00')
        self.assertEqual(result.data_start_at, '2026-08-06T04:00:00+00:00')
        self.assertEqual(result.data_end_at, '2026-08-06T05:00:00+00:00')

    def test_supported_xmltv_time_variants_are_normalized_to_utc(self):
        cases = {
            '20260806120000 +0800': '2026-08-06T04:00:00+00:00',
            '202608061200 +0800': '2026-08-06T04:00:00+00:00',
            '20260806120000+0800': '2026-08-06T04:00:00+00:00',
            '2026-08-06T12:00:00+08:00': '2026-08-06T04:00:00+00:00',
            '2026-08-06T04:00:00Z': '2026-08-06T04:00:00+00:00',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(epg.parse_xmltv_time(raw), expected)

    def test_time_without_offset_is_rejected(self):
        self.assertIsNone(epg.parse_xmltv_time('20260806120000'))
        self.assertIsNone(epg.parse_xmltv_time('2026-08-06T12:00:00'))

    def test_invalid_start_stop_and_non_positive_duration_are_skipped(self):
        result = epg.parse_xmltv(xmltv(
            CHANNELS,
            '<programme channel="cctv1" start="bad" stop="20260806130000 +0800"><title>A</title></programme>'
            '<programme channel="cctv1" start="20260806120000 +0800" stop="bad"><title>B</title></programme>'
            '<programme channel="cctv1" start="20260806130000 +0800" stop="20260806120000 +0800"><title>C</title></programme>',
        ))
        self.assertEqual(result.programmes, [])
        self.assertEqual(result.parsed_programme_count, 0)
        self.assertEqual(result.skipped_programme_count, 3)
        self.assertEqual(result.invalid_time_count, 2)

    def test_unknown_channel_programmes_are_skipped(self):
        result = epg.parse_xmltv(xmltv(
            CHANNELS,
            '<programme channel="missing" start="20260806120000 +0800" stop="20260806130000 +0800"><title>A</title></programme>',
        ))
        self.assertEqual(result.programmes, [])
        self.assertEqual(result.missing_channel_count, 1)
        self.assertEqual(result.skipped_programme_count, 1)

    def test_partially_invalid_programmes_keep_valid_items(self):
        result = epg.parse_xmltv(xmltv(
            CHANNELS,
            '<programme channel="cctv1" start="bad" stop="20260806130000 +0800"><title>A</title></programme>'
            '<programme channel="cctv1" start="20260806130000 +0800" stop="20260806140000 +0800"><title>B</title></programme>',
        ))
        self.assertEqual([item['title'] for item in result.programmes], ['B'])
        self.assertEqual(result.parsed_programme_count, 1)
        self.assertEqual(result.skipped_programme_count, 1)

    async def test_gzip_download_succeeds_and_reports_sizes(self):
        xml_bytes = xmltv(CHANNELS, '').encode()
        payload = gzip.compress(xml_bytes)
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            headers={'content-type': 'application/gzip'},
            content=payload,
        ))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                download = await epg.download_xmltv(client, 'https://example.test/epg.xml.gz', temp_dir=tmpdir)
                try:
                    self.assertEqual(download.downloaded_bytes, len(payload))
                    self.assertEqual(download.decompressed_bytes, len(xml_bytes))
                    with open(download.path, 'rb') as fh:
                        self.assertIn(b'<channel id="cctv1">', fh.read())
                finally:
                    download.cleanup()
                self.assertEqual(os.listdir(tmpdir), [])

    async def test_gzip_metadata_does_not_force_decompression_of_plain_xml(self):
        xml_bytes = xmltv(CHANNELS, '').encode()
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            headers={'content-type': 'application/gzip'},
            content=xml_bytes,
        ))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                download = await epg.download_xmltv(client, 'https://example.test/epg.xml.gz', temp_dir=tmpdir)
                try:
                    with open(download.path, 'rb') as fh:
                        self.assertEqual(fh.read(), xml_bytes)
                finally:
                    download.cleanup()

    async def test_gzip_mime_does_not_force_decompression_of_plain_xml(self):
        xml_bytes = xmltv(CHANNELS, '').encode()
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            headers={'content-type': 'application/gzip'},
            content=xml_bytes,
        ))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                download = await epg.download_xmltv(client, 'https://example.test/epg.xml', temp_dir=tmpdir)
                try:
                    self.assertEqual(download.decompressed_bytes, len(xml_bytes))
                finally:
                    download.cleanup()

    async def test_plain_xml_download_succeeds(self):
        xml_bytes = xmltv(CHANNELS, '').encode()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=xml_bytes))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                download = await epg.download_xmltv(client, 'https://example.test/epg.xml', temp_dir=tmpdir)
                try:
                    self.assertEqual(download.downloaded_bytes, len(xml_bytes))
                finally:
                    download.cleanup()

    async def test_corrupted_gzip_fails_closed(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            content=b'\x1f\x8b\x08\x00corrupted',
        ))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaisesRegex(epg.EpgRefreshError, 'gzip 数据无效'):
                    await epg.download_xmltv(client, 'https://example.test/epg.xml', temp_dir=tmpdir)
            self.assertEqual(os.listdir(tmpdir), [])

    async def test_html_error_page_fails_before_xmltv_parse(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            headers={'content-type': 'application/gzip'},
            content=b'<html><body>upstream error</body></html>',
        ))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaisesRegex(epg.EpgRefreshError, '不是有效 XMLTV'):
                    await epg.download_xmltv(client, 'https://example.test/epg.xml.gz', temp_dir=tmpdir)
            self.assertEqual(os.listdir(tmpdir), [])

    async def test_garbage_payload_fails_closed(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b'not xml or gzip'))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaisesRegex(epg.EpgRefreshError, '不是有效 XMLTV'):
                    await epg.download_xmltv(client, 'https://example.test/epg.xml', temp_dir=tmpdir)
            self.assertEqual(os.listdir(tmpdir), [])

    async def test_xmltv_bom_and_leading_whitespace_are_supported(self):
        xml_bytes = b'\xef\xbb\xbf \r\n\t<tv>' + CHANNELS.encode() + b'</tv>'
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=xml_bytes))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                download = await epg.download_xmltv(client, 'https://example.test/epg.xml', temp_dir=tmpdir)
                try:
                    self.assertEqual(download.decompressed_bytes, len(xml_bytes))
                    parsed = epg.parse_xmltv_file(download.path)
                    self.assertEqual(parsed.parsed_channel_count, 1)
                finally:
                    download.cleanup()

    async def test_already_decoded_xml_is_not_decompressed_again(self):
        xml_bytes = xmltv(CHANNELS, '').encode()
        with tempfile.TemporaryDirectory() as tmpdir:
            download = await epg.download_xmltv(
                _AlreadyDecodedClient(xml_bytes),
                'https://example.test/epg.xml.gz',
                temp_dir=tmpdir,
            )
            try:
                with open(download.path, 'rb') as fh:
                    self.assertEqual(fh.read(), xml_bytes)
            finally:
                download.cleanup()

    async def test_httpx_content_encoding_decode_is_not_repeated(self):
        xml_bytes = xmltv(CHANNELS, '').encode()
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            headers={'content-encoding': 'gzip'},
            content=gzip.compress(xml_bytes),
        ))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                download = await epg.download_xmltv(client, 'https://example.test/epg.xml.gz', temp_dir=tmpdir)
                try:
                    with open(download.path, 'rb') as fh:
                        self.assertEqual(fh.read(), xml_bytes)
                finally:
                    download.cleanup()

    async def test_compressed_download_limit_rejects_and_cleans_temp_files(self):
        payload = b'x' * 64
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaisesRegex(epg.EpgRefreshError, '下载大小'):
                    await epg.download_xmltv(
                        client,
                        'https://example.test/epg.xml',
                        max_download_bytes=32,
                        temp_dir=tmpdir,
                    )
                self.assertEqual(os.listdir(tmpdir), [])

    async def test_content_length_limit_rejects_before_streaming(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            headers={'content-length': '4096'},
            stream=_ExplodingStream(),
        ))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaisesRegex(epg.EpgRefreshError, '下载大小'):
                    await epg.download_xmltv(
                        client,
                        'https://example.test/epg.xml',
                        max_download_bytes=128,
                        temp_dir=tmpdir,
                    )
                self.assertEqual(os.listdir(tmpdir), [])

    def test_malformed_xml_raises_safe_parser_error(self):
        with self.assertRaisesRegex(epg.EpgRefreshError, 'XMLTV 解析失败'):
            epg.parse_xmltv('<tv><channel>')

    async def test_gzip_expansion_limit_rejects_bomb_and_cleans_temp_files(self):
        payload = gzip.compress(b'a' * 4096)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaisesRegex(epg.EpgRefreshError, '解压大小'):
                    await epg.download_xmltv(
                        client,
                        'https://example.test/epg.xml.gz',
                        max_decompressed_bytes=128,
                        temp_dir=tmpdir,
                    )
                self.assertEqual(os.listdir(tmpdir), [])

    async def test_cancelled_download_propagates_and_cleans_temp_files(self):
        stop_event = asyncio.Event()
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200,
            stream=_CancellingStream(stop_event),
        ))
        with tempfile.TemporaryDirectory() as tmpdir:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(asyncio.CancelledError):
                    await epg.download_xmltv(
                        client,
                        'https://example.test/epg.xml',
                        stop_event=stop_event,
                        temp_dir=tmpdir,
                    )
                self.assertEqual(os.listdir(tmpdir), [])


if __name__ == '__main__':
    unittest.main()
