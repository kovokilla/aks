"""Installer tests use synthetic ZIP archives and never contact the network."""

import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_terraform


class InstallTerraformTests(unittest.TestCase):
    def package(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as bundle:
            bundle.writestr("terraform", b"synthetic fixture, not an executable")
        return stream.getvalue()

    def responses(self, data, digest=None):
        digest = digest or hashlib.sha256(data).hexdigest()
        manifest = f"{digest}  terraform_1.13.3_linux_amd64.zip\n".encode()
        return [io.BytesIO(manifest), io.BytesIO(data)]

    @mock.patch.object(install_terraform.urllib.request, "urlopen")
    def test_verified_install(self, urlopen):
        urlopen.side_effect = self.responses(self.package())
        with tempfile.TemporaryDirectory() as directory:
            path = install_terraform.install(directory, "Linux", "x86_64")
            self.assertEqual(path.read_bytes(), b"synthetic fixture, not an executable")
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        for call in urlopen.call_args_list:
            self.assertTrue(call.args[0].startswith("https://releases.hashicorp.com/terraform/1.13.3/"))

    @mock.patch.object(install_terraform.urllib.request, "urlopen")
    def test_checksum_mismatch_writes_nothing(self, urlopen):
        urlopen.side_effect = self.responses(self.package(), digest="0" * 64)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                install_terraform.install(directory, "Linux", "x86_64")
            self.assertEqual(list(Path(directory).iterdir()), [])

    @mock.patch.object(install_terraform.urllib.request, "urlopen")
    def test_existing_binary_not_overwritten(self, urlopen):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terraform"
            path.write_text("keep")
            with self.assertRaisesRegex(ValueError, "Refusing"):
                install_terraform.install(directory, "Linux", "x86_64")
            self.assertEqual(path.read_text(), "keep")
        urlopen.assert_not_called()

    @mock.patch.object(install_terraform.urllib.request, "urlopen")
    def test_unsupported_platform_fails_before_network(self, urlopen):
        with self.assertRaises(ValueError):
            install_terraform.install("/unused", "Windows", "x86_64")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
