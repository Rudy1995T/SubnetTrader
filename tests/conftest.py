"""
Pytest configuration – set env vars before app modules load.
"""
import os
import sys

# Ensure tests can import app modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set test env vars before any app imports
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("JSONL_DIR", "/tmp/subnet_trader_test_logs")
os.environ.setdefault("FLAMEWIRE_API_KEY", "")
os.environ.setdefault("TAOSTATS_API_KEY", "")
os.environ.setdefault("BT_WALLET_NAME", "test")
os.environ.setdefault("BT_WALLET_HOTKEY", "test")
