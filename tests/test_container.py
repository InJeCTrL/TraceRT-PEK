import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from container_entrypoint import initialize_data


class ContainerTests(unittest.TestCase):
    def test_seed_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            initialize_data(data, ROOT)
            (data / 'camera_points.json').write_text('[]')
            (data / 'camera_corrections.json').write_text('{"items":{}}')
            initialize_data(data, ROOT)
            self.assertEqual((data / 'camera_points.json').read_text(), '[]')
            self.assertTrue((data / 'camera_corrections.json').exists())

    def test_missing_credentials_fail_closed(self):
        env = {key: value for key, value in os.environ.items()
               if key not in ('AMAP_API_KEY', 'AMAP_JS_KEY', 'AMAP_JS_SECURITY_CODE', 'ADMIN_PASSWORD')}
        result = subprocess.run([sys.executable, str(ROOT / 'server.py')], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing required environment variables:', result.stderr)

    def test_runtime_config_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            initialize_data(data, ROOT)
            env = {'TRACERT_DATA_DIR': directory, 'AMAP_API_KEY': 'server-private-key',
                   'ADMIN_USERNAME': 'admin', 'ADMIN_PASSWORD': 'private-password',
                   'AMAP_JS_KEY': 'browser-key', 'AMAP_JS_SECURITY_CODE': '</script>test'}
            with patch.dict(os.environ, env):
                spec = importlib.util.spec_from_file_location('test_server', ROOT / 'server.py')
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            self.assertEqual(module.DB_FILE.parent, data)
            self.assertEqual(module.CAMERA_CORRECTIONS_FILE.parent, data)
            server = module.ThreadingHTTPServer(('127.0.0.1', 0), module.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                html = urllib.request.urlopen(base).read().decode()
                self.assertIn('key=browser-key', html)
                self.assertIn('\\u003c/script>test', html)
                self.assertNotIn('__AMAP_', html)
                self.assertNotIn('server-private-key', html)
                self.assertNotIn('private-password', html)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base + '/admin')
                self.assertEqual(error.exception.code, 401)
                opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler())
                import base64
                request = urllib.request.Request(base + '/admin', headers={
                    'Authorization': 'Basic ' + base64.b64encode(b'admin:private-password').decode()})
                self.assertIn('key=browser-key', opener.open(request).read().decode())
                health = json.load(urllib.request.urlopen(base + '/api/health'))
                self.assertTrue(health['ok'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
