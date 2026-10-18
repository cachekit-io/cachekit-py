"""Fixtures shared by the CachekitIO backend tests that need a real TLS peer."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_FAKE = Path(__file__).parents[2] / "performance" / "loopback_saas.py"


@pytest.fixture(autouse=True)
def _no_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """No proxy from the developer's environment: the client honours one, and would send loopback TLS through it.

    Tests that exercise proxy selection set their own variables after this runs.
    """
    # urllib.request.getproxies() reads every *_proxy variable, in either case.
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)


@pytest.fixture(scope="module")
def fake_saas(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[int, Path]]:
    """tests/performance/loopback_saas.py on a free loopback port, over TLS (HTTP/1.1): (port, CA cert).

    Needs ``openssl`` on PATH; a module using it skips without one.
    """
    tmp = tmp_path_factory.mktemp("loopback-tls")
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(  # noqa: S603 (trusted: literal openssl argv)
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1"]
        + ["-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
        check=True,
        capture_output=True,
    )
    proc = subprocess.Popen(  # noqa: S603 (trusted: this interpreter and a repo script)
        [sys.executable, str(_FAKE), "0", str(cert), str(key), "2"], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().split()
    if line[:1] != ["ready"]:
        proc.kill()
        pytest.fail(f"loopback fake failed to start (exit {proc.wait()})")
    yield int(line[1]), cert
    proc.kill()
    proc.wait()
