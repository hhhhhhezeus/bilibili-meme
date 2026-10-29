import importlib.util
from pathlib import Path
import tempfile
import tomllib
import unittest

spec = importlib.util.spec_from_file_location('login', Path(__file__).resolve().parents[1] / 'tools/bili_login.py')
login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login)

class LoginTests(unittest.TestCase):
    def test_cookie_is_data_and_only_plugin_section_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[other]\nsessdata = "keep"\n[plugin]\n  sessdata = "old" # comment\nenabled = true\n')
            cookie = 'quote"\\slash\n中文'
            login.write_sessdata(path, cookie)
            parsed = tomllib.loads(path.read_text())
            self.assertEqual(parsed['plugin']['sessdata'], cookie)
            self.assertEqual(parsed['other']['sessdata'], 'keep')
            self.assertTrue(parsed['plugin']['enabled'])

    def test_missing_key_does_not_overwrite_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            source = '[plugin]\nenabled = true\n'
            path.write_text(source)
            with self.assertRaises(ValueError):
                login.write_sessdata(path, 'value')
            self.assertEqual(path.read_text(), source)
