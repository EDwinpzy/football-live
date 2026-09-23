import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tools import license_gen


class LicenseGeneratorRuntimeSecretTests(unittest.TestCase):
    def test_license_generator_reads_the_original_private_key_from_a_runtime_file(self):
        expected = ec.generate_private_key(ec.SECP256R1())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private_key.pem"
            path.write_bytes(expected.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            if os.name == "posix":
                path.chmod(0o600)
            with patch.dict(os.environ, {"OTV_PRIV_KEY": "", "OTV_PRIV_KEY_FILE": str(path)}):
                actual = license_gen.load_priv()
        self.assertEqual(
            actual.private_numbers().private_value,
            expected.private_numbers().private_value,
        )


if __name__ == "__main__":
    unittest.main()
