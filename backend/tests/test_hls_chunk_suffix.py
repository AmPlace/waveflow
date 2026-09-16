import unittest
from unittest import mock
from urllib.parse import urlsplit, parse_qs

from fastapi import HTTPException


class HlsChunkSuffixTest(unittest.TestCase):
    def setUp(self):
        secret = mock.patch.dict('os.environ', {'WAVEFLOW_PROXY_HANDLE_SECRET': 'hls-suffix-fixture'})
        secret.start()
        self.addCleanup(secret.stop)
        from core.m3u8_rewriter import RewriteContext, rewrite_m3u8
        from routers.media_proxy import _safe_decode
        self.context = RewriteContext(
            base_url='https://origin.test/live/index.m3u8', src_id='source-one',
            ctx_id='context-one', propagated_access_token='fixture-credential',
        )
        self.rewrite = rewrite_m3u8
        self.decode = _safe_decode

    def test_media_suffixes_preserve_exact_signed_target_and_credential(self):
        for suffix in ('.ts', '.m4s', '.mp4', '.aac', '.cmfv', '.cmfa'):
            with self.subTest(suffix=suffix):
                target = 'segment' + suffix + '?sig=origin-fixture'
                uri = self.rewrite('#EXTM3U\n#EXTINF:2,\n' + target, self.context).splitlines()[-1]
                parsed = urlsplit(uri)
                self.assertTrue(parsed.path.endswith(suffix))
                payload = self.decode(parsed.path.rsplit('/', 1)[1], expected_kind='chunk')
                self.assertEqual(payload.url, 'https://origin.test/live/' + target)
                self.assertEqual(payload.src_id, 'source-one')
                self.assertEqual(payload.ctx, 'context-one')
                self.assertEqual(parse_qs(parsed.query)['access_token'], ['fixture-credential'])

    def test_map_and_extensionless_cmaf_are_not_labeled_ts(self):
        text = '#EXTM3U\n#EXT-X-MAP:URI="init"\n#EXT-X-PART:DURATION=1,URI="part"\n#EXTINF:2,\nsegment\n'
        import re
        output = self.rewrite(text, self.context)
        uris = re.findall(r'/api/media/proxy/chunk/[^"\s]+', output)
        self.assertEqual([urlsplit(uri).path.rsplit('.', 1)[1] for uri in uris], ['mp4', 'm4s', 'm4s'])
        for uri, name in zip(uris, ('init', 'part', 'segment')):
            payload = self.decode(urlsplit(uri).path.rsplit('/', 1)[1], expected_kind='chunk')
            self.assertEqual(payload.url, 'https://origin.test/live/' + name)

    def test_suffix_does_not_bypass_signature_kind_or_expiry(self):
        from security.proxy_handles import issue_handle
        handle = issue_handle(kind='chunk', url='https://origin.test/segment.ts')
        self.assertEqual(self.decode(handle, expected_kind='chunk').url, 'https://origin.test/segment.ts')
        body, signature = handle.split('.')
        tampered = body + '.' + ('A' if signature[0] != 'A' else 'B') + signature[1:] + '.mp4'
        for invalid in (tampered, handle + '.exe', handle + '.ts.ts'):
            with self.subTest(invalid_type=invalid[-6:]), self.assertRaises(HTTPException):
                self.decode(invalid, expected_kind='chunk')
        wrong_kind = issue_handle(kind='playlist', url='https://origin.test/index.m3u8')
        with self.assertRaises(HTTPException):
            self.decode(wrong_kind + '.ts', expected_kind='chunk')
        with mock.patch('time.time', return_value=1):
            expired = issue_handle(kind='chunk', url='https://origin.test/a.ts', ttl_seconds=1)
        with self.assertRaises(HTTPException) as error:
            self.decode(expired + '.ts', expected_kind='chunk')
        self.assertEqual(error.exception.status_code, 410)
