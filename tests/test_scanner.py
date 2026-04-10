"""
test_scanner.py — Tests for sensitive content detection.
"""

from mempalace.scanner import scan_content, format_warnings


class TestScanContent:
    # ── API key patterns ──────────────────────────────────────────────

    def test_detects_openai_api_key(self):
        content = "Use this key: sk-abc123def456ghi789jkl012mno345"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "api_key"

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

    def test_detects_stripe_live_key(self):
        prefix = "sk_live_"
        content = f"STRIPE_KEY={prefix}{'x' * 24}"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "api_key"

    def test_detects_stripe_test_key(self):
        prefix = "sk_test_"
        content = f"key = {prefix}{'x' * 24}"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "api_key"

    def test_detects_slack_bot_token(self):
        content = "SLACK_TOKEN=xoxb-123456789012-abcdefghijkl"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "api_key"

    def test_detects_slack_user_token(self):
        content = "token: xoxp-123456789012-abcdefghijkl"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "api_key"

    def test_detects_npm_token(self):
        content = "//registry.npmjs.org/:_authToken=npm_abcdefghijklmnopqrst"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "api_key"

    # ── Bearer token ──────────────────────────────────────────────────

    def test_detects_bearer_token(self):
        content = "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "bearer_token"

    def test_detects_bearer_token_case_insensitive(self):
        content = "authorization: bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "bearer_token"

    # ── Password assignment ───────────────────────────────────────────

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

    # ── Private key ───────────────────────────────────────────────────

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

    # ── Connection strings ────────────────────────────────────────────

    def test_detects_postgres_connection_string(self):
        content = "DATABASE_URL=postgres://user:pass@host:5432/mydb"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "connection_string"

    def test_detects_mongodb_connection_string(self):
        content = "MONGO_URI=mongodb://admin:secret@cluster0.example.net/db"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "connection_string"

    def test_detects_redis_connection_string(self):
        content = "REDIS_URL=redis://default:mypassword@redis.example.com:6379"
        findings = scan_content(content)
        assert len(findings) == 1
        assert findings[0]["pattern_name"] == "connection_string"

    # ── False positives ───────────────────────────────────────────────

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

    # ── Edge cases ────────────────────────────────────────────────────

    def test_empty_content(self):
        assert scan_content("") == []

    def test_none_content(self):
        assert scan_content(None) == []

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

    def test_findings_have_position_not_content(self):
        content = "secret: sk-abc123def456ghi789jkl012mno345"
        findings = scan_content(content)
        assert "start" in findings[0]
        assert "end" in findings[0]
        assert "match" not in findings[0]


class TestFormatWarnings:
    def test_empty_findings(self):
        assert format_warnings([]) == ""

    def test_single_finding(self):
        findings = [{"pattern_name": "api_key", "start": 10, "end": 45}]
        result = format_warnings(findings)
        assert "WARNING" in result
        assert "api_key" in result
        assert "chars 10-45" in result

    def test_no_secret_content_in_output(self):
        findings = [{"pattern_name": "api_key", "start": 0, "end": 40}]
        result = format_warnings(findings)
        # Should only contain pattern name and position, no actual secret
        assert "sk-" not in result
        assert "AKIA" not in result
