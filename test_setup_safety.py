import os
import tempfile
import unittest
from pathlib import Path
from buildingwatch.config import Building
from test_building_watch import config
from whatsapp_setup import save_token


class SetupSafetyTests(unittest.TestCase):
    def test_existing_ignore_file_and_directory_mode_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            os.chmod(root, 0o755)
            ignore = root / '.gitignore'
            ignore.write_text('important-rule\n')
            path = save_token(root/'whapi.json', 'FAKE-TEST-TOKEN')
            self.assertEqual(ignore.read_text(), 'important-rule\n')
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                save_token(path, 'FAKE-SECOND-TOKEN')
            self.assertEqual(ignore.read_text(), 'important-rule\n')

    def test_unknown_timezone_has_a_clear_error(self):
        c = config(); c['timezone'] = 'America/New_Yrok'
        with self.assertRaisesRegex(ValueError, 'Unknown timezone: America/New_Yrok'):
            Building(c)


if __name__ == '__main__': unittest.main()
