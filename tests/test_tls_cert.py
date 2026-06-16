"""Tests for self-signed TLS certificate generation/persistence."""
import os
import stat

from cryptography import x509

from tls_cert import ensure_self_signed_cert


def _san_names(cert):
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return ext.value.get_values_for_type(x509.DNSName)


class TestEnsureSelfSignedCert:
    def test_creates_cert_and_key(self, tmp_path):
        cert_path, key_path = ensure_self_signed_cert(str(tmp_path))
        assert os.path.exists(cert_path)
        assert os.path.exists(key_path)
        assert cert_path == os.path.join(str(tmp_path), 'tls', 'cert.pem')
        assert key_path == os.path.join(str(tmp_path), 'tls', 'key.pem')

    def test_key_file_is_owner_only(self, tmp_path):
        _, key_path = ensure_self_signed_cert(str(tmp_path))
        mode = stat.S_IMODE(os.stat(key_path).st_mode)
        assert mode == 0o600

    def test_cert_file_is_world_readable(self, tmp_path):
        cert_path, _ = ensure_self_signed_cert(str(tmp_path))
        mode = stat.S_IMODE(os.stat(cert_path).st_mode)
        assert mode == 0o644

    def test_cert_san_includes_expected_hostnames(self, tmp_path):
        cert_path, _ = ensure_self_signed_cert(str(tmp_path))
        with open(cert_path, 'rb') as f:
            cert = x509.load_pem_x509_certificate(f.read())
        names = _san_names(cert)
        assert 'owl.local' in names
        assert 'retina.local' in names
        assert 'localhost' in names

    def test_second_call_reuses_existing_cert(self, tmp_path):
        cert_path, key_path = ensure_self_signed_cert(str(tmp_path))
        with open(cert_path, 'rb') as f:
            original_cert_bytes = f.read()
        original_mtime = os.stat(key_path).st_mtime_ns

        cert_path_2, key_path_2 = ensure_self_signed_cert(str(tmp_path))

        assert cert_path_2 == cert_path
        assert key_path_2 == key_path
        assert os.stat(key_path).st_mtime_ns == original_mtime
        with open(cert_path, 'rb') as f:
            assert f.read() == original_cert_bytes
