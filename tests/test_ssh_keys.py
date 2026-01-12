"""Tests for SSH key validation."""
import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import is_valid_ssh_key

# Valid test keys
VALID_ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample user@host"
VALID_RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQExample user@host"
VALID_ECDSA = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY= user@host"


class TestValidKeys:
    """Test that valid SSH keys are accepted."""

    def test_valid_ed25519(self):
        assert is_valid_ssh_key(VALID_ED25519)

    def test_valid_rsa(self):
        assert is_valid_ssh_key(VALID_RSA)

    def test_valid_ecdsa(self):
        assert is_valid_ssh_key(VALID_ECDSA)

    def test_valid_with_no_comment(self):
        assert is_valid_ssh_key("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample")

    def test_valid_ecdsa_nistp384(self):
        assert is_valid_ssh_key("ecdsa-sha2-nistp384 AAAAE2VjZHNhLXNoYTI= user")

    def test_valid_ecdsa_nistp521(self):
        assert is_valid_ssh_key("ecdsa-sha2-nistp521 AAAAE2VjZHNhLXNoYTI= user")

    def test_valid_sk_ed25519(self):
        assert is_valid_ssh_key("sk-ssh-ed25519@openssh.com AAAAG2VjZHNhLXNoYTI= user")

    def test_valid_sk_ecdsa(self):
        assert is_valid_ssh_key("sk-ecdsa-sha2-nistp256@openssh.com AAAAG2VjZHNhLXNoYTI= user")


class TestInvalidInputs:
    """Test that invalid/malicious inputs are rejected."""

    def test_empty_string(self):
        assert not is_valid_ssh_key("")

    def test_random_text(self):
        assert not is_valid_ssh_key("hello world")

    def test_command_injection_semicolon(self):
        assert not is_valid_ssh_key("ssh-ed25519; rm -rf /")

    def test_command_injection_backticks(self):
        assert not is_valid_ssh_key("ssh-ed25519 `whoami`")

    def test_command_injection_dollar(self):
        assert not is_valid_ssh_key("ssh-ed25519 $(whoami)")

    def test_command_injection_pipe(self):
        assert not is_valid_ssh_key("ssh-ed25519 AAAA | cat /etc/passwd")

    def test_newline_injection(self):
        assert not is_valid_ssh_key("ssh-ed25519 AAAA\nssh-rsa BBBB")

    def test_carriage_return_injection(self):
        assert not is_valid_ssh_key("ssh-ed25519 AAAA\rssh-rsa BBBB")

    def test_too_long(self):
        assert not is_valid_ssh_key("ssh-ed25519 " + "A" * 3000)

    def test_invalid_key_type_prefix_trick(self):
        assert not is_valid_ssh_key("ssh-evil AAAA user@host")

    def test_invalid_key_type_suffix(self):
        assert not is_valid_ssh_key("ssh-ed25519-malicious AAAA user@host")

    def test_missing_key_data(self):
        assert not is_valid_ssh_key("ssh-ed25519")

    def test_invalid_base64_exclamation(self):
        assert not is_valid_ssh_key("ssh-ed25519 not!valid user")

    def test_invalid_base64_at_sign(self):
        assert not is_valid_ssh_key("ssh-ed25519 invalid@base64 user")

    def test_invalid_base64_special_chars(self):
        assert not is_valid_ssh_key("ssh-ed25519 <script>alert(1)</script> user")


class TestEdgeCases:
    """Test edge cases and whitespace handling."""

    def test_key_with_extra_spaces(self):
        assert is_valid_ssh_key("ssh-ed25519   AAAAC3NzaC1lZDI1NTE5AAAAIExample   user@host")

    def test_key_with_tabs(self):
        assert is_valid_ssh_key("ssh-ed25519\tAAAAC3NzaC1lZDI1NTE5AAAAIExample\tuser@host")

    def test_key_with_long_comment(self):
        long_comment = "user@" + "x" * 500
        assert is_valid_ssh_key(f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample {long_comment}")

    def test_key_with_multiple_comment_parts(self):
        assert is_valid_ssh_key("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample user@host extra comment parts")

    def test_base64_with_padding(self):
        assert is_valid_ssh_key("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample== user")

    def test_base64_with_slashes(self):
        assert is_valid_ssh_key("ssh-ed25519 AAAA/BBB+CCC/DDD= user")
