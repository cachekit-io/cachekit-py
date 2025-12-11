"""Pytest configuration for markdown documentation tests.

This conftest.py is discovered by pytest when running markdown tests from the docs/ directory.
It provides the pytest_markdown_docs_globals hook that injects common imports and mocks
into all markdown code examples.
"""

import logging
import os
import time

try:
    from fakeredis import FakeRedis
except ImportError:
    FakeRedis = None

import numpy as np
import pandas as pd

from cachekit import cache


def pytest_markdown_docs_globals():
    """Provide global fixtures for markdown documentation examples.

    This hook injects common imports and mocks into all markdown code examples,
    allowing documentation to remain clean while tests remain functional.

    Returns:
        dict: Global variables available to markdown examples
    """

    # Stub functions for documentation examples
    def do_expensive_computation():
        """Stub for expensive computation examples."""
        return {"result": "computed", "value": 42}

    def fetch_from_database(user_id):
        """Stub for database fetch examples."""
        return {"id": user_id, "name": "Alice", "email": f"user{user_id}@example.com"}

    def build_profile(user_id):
        """Stub for profile building examples."""
        return {"user_id": user_id, "profile": "data", "settings": {}}

    def fetch_user(user_id):
        """Stub for user fetch examples."""
        return {"id": user_id, "name": "Bob", "active": True}

    def process_business_logic(request_id):
        """Stub for business logic examples."""
        return {"request_id": request_id, "status": "processed", "result": "success"}

    def process_data(data):
        """Stub for data processing examples."""
        return {"processed": True, "data": data}

    def expensive_operation():
        """Stub for expensive operation examples."""
        return {"computed": True, "timestamp": time.time()}

    def compute_intensive_result():
        """Stub for compute intensive examples."""
        return {"result": "computed", "iterations": 1000}

    def process_item(item_id):
        """Stub for item processing examples."""
        return {"item_id": item_id, "processed": True}

    def important_data():
        """Stub for important data examples."""
        return {"data": "important", "priority": "high"}

    def transform(data):
        """Stub for transform examples."""
        return {"transformed": data}

    def process_tenant_request(tenant_id, request):
        """Stub for tenant request examples."""
        return {"tenant_id": tenant_id, "request": request, "result": "ok"}

    def trained_ml_model():
        """Stub for ML model examples."""
        return {"model": "trained", "accuracy": 0.95}

    def expensive_computation():
        """Stub for expensive computation examples."""
        return {"computed": True}

    # Create a logger for examples
    logger = logging.getLogger("cachekit.examples")
    logger.setLevel(logging.INFO)

    # Secret key for encryption examples (test value only)
    secret_key = "a" * 64  # 32 bytes in hex
    # Set env var so @cache.secure validation passes
    os.environ["CACHEKIT_MASTER_KEY"] = secret_key

    globals_dict = {
        "cache": cache,
        "asyncio": __import__("asyncio"),
        "time": time,
        "logging": logging,
        "logger": logger,
        "np": np,
        "pd": pd,
        # Stub functions
        "do_expensive_computation": do_expensive_computation,
        "fetch_from_database": fetch_from_database,
        "build_profile": build_profile,
        "fetch_user": fetch_user,
        "process_business_logic": process_business_logic,
        "process_data": process_data,
        "expensive_operation": expensive_operation,
        "compute_intensive_result": compute_intensive_result,
        "process_item": process_item,
        "important_data": important_data,
        "transform": transform,
        "process_tenant_request": process_tenant_request,
        "trained_ml_model": trained_ml_model,
        "expensive_computation": expensive_computation,
        "secret_key": secret_key,
    }

    # Add redis mock if fakeredis is available
    if FakeRedis is not None:
        globals_dict["redis"] = FakeRedis()

    return globals_dict
