import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sandbox.helpers import helper_path


class TrustedHelperTests(unittest.TestCase):
    def test_installed_package_uses_trusted_data_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            helper = root / 'share' / 'sang-coding-agent' / 'helpers' / 'sandbox_fs.py'
            helper.parent.mkdir(parents=True)
            helper.write_text('trusted')
            with patch('sandbox.helpers.__file__', str(root / 'lib' / 'sandbox' / 'helpers.py')), patch('sandbox.helpers.sys.prefix', str(root)):
                self.assertEqual(helper_path('sandbox_fs'), helper)
