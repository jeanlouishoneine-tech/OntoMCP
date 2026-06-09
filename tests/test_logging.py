"""Unit tests for configure_logging level-precedence. No real logging side effects.

We monkeypatch logging.basicConfig to capture the resolved level so the test is
independent of any global logging state already set up by the test session.
"""

import logging

import pytest

from ontomcp.core.logging import LOGGER_NAME, configure_logging


@pytest.fixture
def captured_basic_config(monkeypatch):
    """Capture the kwargs passed to logging.basicConfig."""
    captured: dict = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: captured.update(kw))
    return captured


def test_explicit_level_wins(monkeypatch, captured_basic_config):
    monkeypatch.setenv("ONTOMCP_LOG_LEVEL", "WARNING")
    logger = configure_logging(level="debug")
    assert captured_basic_config["level"] == "DEBUG"  # explicit arg beats env
    assert logger.name == LOGGER_NAME


def test_env_var_used_when_no_arg(monkeypatch, captured_basic_config):
    monkeypatch.setenv("ONTOMCP_LOG_LEVEL", "warning")
    configure_logging()
    assert captured_basic_config["level"] == "WARNING"  # env beats default


def test_defaults_to_info(monkeypatch, captured_basic_config):
    monkeypatch.delenv("ONTOMCP_LOG_LEVEL", raising=False)
    configure_logging()
    assert captured_basic_config["level"] == "INFO"
