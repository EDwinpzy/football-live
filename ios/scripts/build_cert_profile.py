"""Export the existing public migration CA for the iPhone system trust store."""

import hashlib
import pathlib
import plistlib
import ssl
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
CERT_DIR = ROOT / "ios" / "Certificates"


def build_profile(der: bytes) -> dict:
    fingerprint = hashlib.sha256(der).hexdigest()
    identifier = "com.qiubo.mytv.ios.ca"
    certificate = {
        "PayloadType": "com.apple.security.root",
        "PayloadVersion": 1,
        "PayloadIdentifier": identifier + ".certificate",
        "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, identifier + fingerprint)),
        "PayloadDisplayName": "My TV Root CA",
        "PayloadDescription": "Public root certificate for the existing My TV HTTPS service.",
        "PayloadCertificateFileName": "MyTV-Root-CA.cer",
        "PayloadContent": der,
    }
    return {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": identifier,
        "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, identifier)),
        "PayloadDisplayName": "My TV HTTPS Certificate",
        "PayloadDescription": "Contains only the public My TV root CA. After installation, enable full trust in Settings > General > About > Certificate Trust Settings.",
        "PayloadOrganization": "My TV",
        "PayloadRemovalDisallowed": False,
        "PayloadContent": [certificate],
    }


if __name__ == "__main__":
    pem = (ROOT / "android/app/src/main/res/raw/mytv_migration_ca.pem").read_text(encoding="ascii")
    if "PRIVATE KEY" in pem or pem.count("BEGIN CERTIFICATE") != 1:
        raise ValueError("Expected exactly one public certificate, without a private key")
    der = ssl.PEM_cert_to_DER_cert(pem)
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    (CERT_DIR / "MyTV-Root-CA.cer").write_bytes(der)
    (CERT_DIR / "MyTV-Root-CA.mobileconfig").write_bytes(plistlib.dumps(build_profile(der), sort_keys=False))
    print("Public CA SHA-256:", hashlib.sha256(der).hexdigest())
