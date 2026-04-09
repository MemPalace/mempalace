"""
test_scanner.py — Tests for sensitive content detection.
"""

from mempalace.scanner import scan_content, format_warnings


class TestScanContent:
    def test_detects_openai_api_key(self):
        content = "Use this key: sk-abc123def456ghi789jkl012mno345"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "api_key"
        assert findings[0]["match"].startswith("sk-")

    def test_detects_aws_access_key(self):
        content = "AWS key is AKIAIOSFODNN7EXAMPLE1234"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "api_key"

    def test_detects_github_personal_token(self):
        content = "export TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTuvwx"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "api_key"

    def test_detects_github_oauth_token(self):
        content = "token: gho_abcdefghijklmnopqrstuv"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "api_key"

    def test_detects_github_pat(self):
        content = "pat = github_pat_ABCDEFGHIJKLMNOPQRST1234567890"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "api_key"

    def test_detects_bearer_token(self):
        content = "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "bearer_token"

    def test_detects_password_assignment(self):
        content = 'db_config = {"password": "s3cret_passw0rd!"}'
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "password_assignment"

    def test_detects_password_with_equals(self):
        content = "PASSWORD = 'hunter2_is_not_secure'"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "password_assignment"

    def test_detects_private_key(self):
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA..."
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "private_key"

    def test_detects_ec_private_key(self):
        content = "-----BEGIN EC PRIVATE KEY-----\nMHQCAQEEI..."
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "private_key"

    def test_detects_generic_private_key(self):
        content = "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBg..."
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "private_key"

    def test_no_false_positives_on_normal_text(self):
        content = (
            "The authentication module uses JWT tokens for session management. "
            "Tokens expire after 24 hours. We discussed the password policy "
            "at the team meeting. The bearer of bad news arrived late."
        )
        findings = scan_content(content)
        assert len(findings) == 0

    def test_no_false_positives_on_code(self):
        content = (
            "def get_password_hash(password: str) -> str:\n"
            "    return bcrypt.hashpw(password.encode(), bcrypt.gensalt())\n"
        )
        findings = scan_content(content)
        assert len(findings) == 0

    def test_empty_content(self):
        assert scan_content("") == []

    def test_multiple_findings(self):
        content = (
            "Keys:\n"
            "  OPENAI: sk-proj-abcdefghijklmnopqrst1234\n"
            "  AWS: AKIAIOSFODNN7EXAMPLE1234\n"
            "  password = 'admin123_secret'\n"
        )
        findings = scan_content(content)
        names = [f["pattern_name"] for f in findings]
        assert "api_key" in names
        assert "password_assignment" in names
        assert len(findings) >= 3


class TestFormatWarnings:
    def test_empty_findings(self):
        assert format_warnings([]) == ""

    def test_single_finding(self):
        findings = [{"pattern_name": "api_key", "match": "sk-abc123def456ghi789jkl012", "start": 0, "end": 26}]
        result = format_warnings(findings)
        assert "WARNING" in result
        assert "api_key" in result

    def test_long_match_truncated(self):
        long_match = "sk-" + "a" * 100
        findings = [{"pattern_name": "api_key", "match": long_match, "start": 0, "end": 103}]
        result = format_warnings(findings)
        assert "..." in result
