"""Authentication boundary tests: isolated loopback server, no fnOS access."""
import base64
import http.client
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

SOURCE = os.environ.get('BRIDGE_SOURCE', str(Path(__file__).resolve().parents[1] / 'app/bridge.py'))
spec = importlib.util.spec_from_file_location('bridge', SOURCE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        bridge.CFG = {'secret': 'test-secret', 'port': 6800}
        bridge.CFG_PATH = str(Path(self.temp.name) / 'config.json')
        bridge.VAR_DIR = self.temp.name
        Path(bridge.CFG_PATH).write_text(json.dumps(bridge.CFG))
        bridge.RECENT = [{'time': 'now', 'ts': 9999999999, 'kind': 'push', 'detail': 'keep'}]
        self.server = bridge.ThreadingHTTPServer(('127.0.0.1', 0), bridge.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, method='GET', body=None, secret=None, headers=None):
        headers = dict(headers or {})
        if secret is not None:
            headers['Authorization'] = 'Basic ' + base64.b64encode(
                ('aria2bridge:' + secret).encode()).decode()
        if body is not None:
            body = json.dumps(body)
            headers['Content-Type'] = 'application/json'
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        conn.close()
        return result

    def rpc(self, params, method='aria2.getVersion'):
        return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}

    def test_rpc_missing_wrong_and_valid_tokens(self):
        for token in ([], ['token:'], ['token:wrong'], [None], [123], [['token:test-secret']]):
            with self.subTest(token=token):
                status, _, body = self.request('/jsonrpc', 'POST', self.rpc(token))
                self.assertEqual(status, 200)
                self.assertIn('error', json.loads(body))
        _, headers, body = self.request('/jsonrpc', 'POST', self.rpc(['token:test-secret']))
        self.assertIn('result', json.loads(body))
        self.assertEqual(headers.get('Access-Control-Allow-Origin'), '*')

    def test_rpc_get_batch_and_multicall(self):
        for params in ([], ['token:wrong'], ['token:test-secret']):
            _, _, body = self.request('/jsonrpc?' + urlencode({
                'method': 'aria2.getVersion', 'params': json.dumps(params)}))
            self.assertEqual('result' in json.loads(body), params == ['token:test-secret'])
        _, _, body = self.request('/jsonrpc', 'POST', [self.rpc([]), self.rpc(['token:test-secret'])])
        batch = json.loads(body)
        self.assertIn('error', batch[0]); self.assertIn('result', batch[1])
        calls = [{'methodName': 'aria2.getVersion', 'params': p} for p in ([], ['token:test-secret'])]
        _, _, body = self.request('/jsonrpc', 'POST', self.rpc([calls], 'system.multicall'))
        results = json.loads(body)['result']
        self.assertIn('code', results[0]); self.assertIsInstance(results[1], list)

    def test_unauthenticated_management_denied(self):
        paths = ('/', '/index.html', '/index-extra', '/api/config', '/api/config?x=1',
                 '/api/selftest', '/api/logs', '/api/logs/download')
        for path in paths:
            for secret in (None, 'wrong'):
                with self.subTest(path=path, secret=secret):
                    status, headers, body = self.request(path, secret=secret)
                    self.assertEqual(status, 401)
                    self.assertNotIn('test-secret', body)
                    self.assertNotIn('Access-Control-Allow-Origin', headers)
                    self.assertIn('WWW-Authenticate', headers)

    def test_unauthorized_mutations_have_no_effect(self):
        before = Path(bridge.CFG_PATH).read_text()
        for path, body in (('/api/config', {'secret': 'replaced'}), ('/api/events/clear', {})):
            self.assertEqual(self.request(path, 'POST', body)[0], 401)
        self.assertEqual(bridge.CFG['secret'], 'test-secret')
        self.assertEqual(Path(bridge.CFG_PATH).read_text(), before)
        self.assertEqual(len(bridge.RECENT), 1)

    def test_authenticated_management_and_rotation(self):
        for path in ('/', '/api/config', '/api/logs', '/api/logs/download'):
            status, headers, _ = self.request(path, secret='test-secret')
            self.assertEqual(status, 200)
            self.assertNotIn('Access-Control-Allow-Origin', headers)
            self.assertEqual(headers.get('Cache-Control'), 'no-store')
        with patch.object(bridge.Handler, 'selftest', return_value={'login': 'ok'}):
            self.assertEqual(self.request('/api/selftest', secret='test-secret')[0], 200)
        self.assertEqual(self.request('/api/config', 'POST', {'secret': 'new-secret'}, 'test-secret')[0], 200)
        self.assertEqual(self.request('/api/config', secret='test-secret')[0], 401)
        self.assertEqual(self.request('/api/config', secret='new-secret')[0], 200)
        self.assertIn('error', bridge.dispatch(self.rpc(['token:test-secret'])))
        self.assertIn('result', bridge.dispatch(self.rpc(['token:new-secret'])))
        self.assertEqual(self.request('/api/events/clear', 'POST', {}, 'new-secret')[0], 200)
        self.assertEqual(bridge.RECENT, [])

    def test_empty_secret_fails_closed(self):
        for secret in ('', '   ', None):
            bridge.CFG['secret'] = secret
            self.assertEqual(self.request('/api/config')[0], 503)
            self.assertIn('error', bridge.dispatch(self.rpc([])))
            self.assertIn('error', bridge.dispatch(self.rpc(['token:'])))

    def test_cannot_clear_secret_or_partially_apply_invalid_config(self):
        for body in ({'secret': ''}, {'secret': '  '}, {'secret': 'new', 'dir_map': 'bad'}):
            self.assertEqual(self.request('/api/config', 'POST', body, 'test-secret')[0], 400)
            self.assertEqual(bridge.CFG['secret'], 'test-secret')

    def test_cross_origin_management_rejected(self):
        for headers in ({'Origin': 'https://example.com'}, {'Origin': 'null'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request('/api/events/clear', 'POST', {}, 'test-secret', headers)[0], 403)
        host, port = self.server.server_address
        self.assertEqual(self.request('/api/config', secret='test-secret',
                                      headers={'Origin': 'http://%s:%s' % (host, port)})[0], 200)
        self.assertEqual(len(bridge.RECENT), 1)

    def test_malformed_basic_and_unicode_secret(self):
        for auth in ('Bearer test-secret', 'Basic !!!', 'Basic dXNlcjp0ZXN0LXNlY3JldA=='):
            self.assertEqual(self.request('/api/config', headers={'Authorization': auth})[0], 401)
        bridge.CFG['secret'] = '测试:secret'
        self.assertEqual(self.request('/api/config', secret='测试:secret')[0], 200)
        self.assertIn('result', bridge.dispatch(self.rpc(['token:测试:secret'])))

    def test_custom_admin_credentials_replace_defaults(self):
        """安装向导里自设的账号密码应取代 aria2bridge + Secret 的旧逻辑。"""
        bridge.CFG['admin_user'] = '张三'
        bridge.CFG['admin_password'] = 'p@ss-中文-123'

        def basic(user, pwd):
            raw = '%s:%s' % (user, pwd)
            return {'Authorization': 'Basic ' + base64.b64encode(raw.encode('utf-8')).decode()}

        self.assertEqual(self.request('/', headers=basic('张三', 'p@ss-中文-123'))[0], 200)
        self.assertEqual(self.request('/api/config', headers=basic('张三', 'p@ss-中文-123'))[0], 200)
        # 旧默认凭据失效
        self.assertEqual(self.request('/', secret='test-secret')[0], 401)
        # 用户名/密码任一错误都拒绝
        self.assertEqual(self.request('/', headers=basic('张三', 'wrong'))[0], 401)
        self.assertEqual(self.request('/', headers=basic('aria2bridge', 'p@ss-中文-123'))[0], 401)

    def test_status_script_data_is_escaped(self):
        bridge.RECENT[0]['detail'] = '</script><script>alert(1)</script>'
        _, _, body = self.request('/', secret='test-secret')
        self.assertNotIn('</script><script>alert(1)', body)
        self.assertIn('\\u003c/script>', body)



class ConfigValidationTests(unittest.TestCase):
    """设置面板保存管理页凭据的校验与落盘。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        bridge.CFG = {'secret': 's3cret-value', 'port': 6800}
        bridge.CFG_PATH = str(Path(self.temp.name) / 'config.json')
        bridge.VAR_DIR = self.temp.name
        bridge.RECENT = []
        Path(bridge.CFG_PATH).write_text(json.dumps(bridge.CFG))

    def tearDown(self):
        self.temp.cleanup()

    def test_admin_credentials_validation_and_persist(self):
        with self.assertRaises(ValueError):
            bridge.apply_config({'admin_user': '   '})
        with self.assertRaises(ValueError):
            bridge.apply_config({'admin_password': '123'})

        bridge.apply_config({'admin_user': ' bob ', 'admin_password': 'abcdef'})
        self.assertEqual(bridge.CFG['admin_user'], 'bob')
        self.assertEqual(bridge.CFG['admin_password'], 'abcdef')

        saved = json.loads(Path(bridge.CFG_PATH).read_text(encoding='utf-8'))
        self.assertEqual(saved['admin_user'], 'bob')
        self.assertEqual(saved['admin_password'], 'abcdef')
        self.assertNotIn('admin_password', json.dumps(bridge.Handler.view_config(None)))

if __name__ == '__main__':
    unittest.main()
