"""Tests for the runtime_type resolver in src.database_postgres.

Covers _sanitize_runtime_type (whitelist validation with safe fallback) and
get_runtime_type (reads config via get_config_value and sanitizes the result).
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# Mock psycopg2 and related modules before importing database_postgres so that
# importing the module does not require a real PostgreSQL driver/connection.
mock_psycopg2 = MagicMock()
mock_psycopg2.pool = MagicMock()
mock_psycopg2.extras = MagicMock()
sys.modules.setdefault("psycopg2", mock_psycopg2)
sys.modules.setdefault("psycopg2.pool", mock_psycopg2.pool)
sys.modules.setdefault("psycopg2.extras", mock_psycopg2.extras)

from src.database_postgres import (  # noqa: E402
    ALLOWED_RUNTIME_TYPES,
    DEFAULT_RUNTIME_TYPE,
    _sanitize_runtime_type,
    get_runtime_type,
)


class TestSanitizeRuntimeType:
    """Validate the whitelist sanitizer."""

    def test_runc_is_allowed(self):
        assert _sanitize_runtime_type("runc") == "runc"

    def test_kata_is_allowed(self):
        assert _sanitize_runtime_type("kata") == "kata"

    def test_invalid_value_falls_back_to_runc_and_warns(self):
        with patch("src.database_postgres.logger") as mock_logger:
            assert _sanitize_runtime_type("rm -rf") == "runc"
            mock_logger.warning.assert_called_once()

    def test_empty_string_falls_back_without_warning(self):
        with patch("src.database_postgres.logger") as mock_logger:
            assert _sanitize_runtime_type("") == "runc"
            mock_logger.warning.assert_not_called()

    def test_none_falls_back_without_warning(self):
        with patch("src.database_postgres.logger") as mock_logger:
            assert _sanitize_runtime_type(None) == "runc"
            mock_logger.warning.assert_not_called()

    def test_default_matches_whitelist(self):
        assert DEFAULT_RUNTIME_TYPE in ALLOWED_RUNTIME_TYPES


class TestGetRuntimeType:
    """Validate get_runtime_type reads config and sanitizes."""

    def test_returns_kata_when_configured(self):
        with patch("src.database_postgres.get_config_value", return_value="kata"):
            assert get_runtime_type() == "kata"

    def test_returns_runc_when_configured(self):
        with patch("src.database_postgres.get_config_value", return_value="runc"):
            assert get_runtime_type() == "runc"

    def test_invalid_config_value_falls_back_to_runc(self):
        with patch("src.database_postgres.get_config_value", return_value="danger"):
            assert get_runtime_type() == "runc"

    def test_missing_config_defaults_to_runc(self):
        # When unset, get_config_value returns the provided default ('runc').
        with patch(
            "src.database_postgres.get_config_value", return_value=DEFAULT_RUNTIME_TYPE
        ):
            assert get_runtime_type() == "runc"
