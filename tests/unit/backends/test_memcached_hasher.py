"""NativeRendezvousHash picks the server pymemcache's RendezvousHash picks, for every key.

A different pick moves every cached key to another server, so these compare against pymemcache
itself rather than against recorded values.
"""

from __future__ import annotations

from enum import Enum, StrEnum

import pytest

pytest.importorskip("pymemcache")

from hypothesis import given
from hypothesis import strategies as st
from pymemcache.client.hash import HashClient
from pymemcache.client.murmur3 import murmur3_32
from pymemcache.client.rendezvous import RendezvousHash

from cachekit._rust_serializer import rendezvous_node
from cachekit.backends.memcached.backend import MemcachedBackend, _parse_server
from cachekit.backends.memcached.config import MemcachedBackendConfig
from cachekit.backends.memcached.hasher import NativeRendezvousHash

server = st.builds("{}:{}".format, st.from_regex(r"[a-z0-9.\-]{1,40}", fullmatch=True), st.integers(1, 65535))
servers = st.lists(server, min_size=1, max_size=5, unique=True)


def both(nodes: list[str]) -> tuple[RendezvousHash, NativeRendezvousHash]:
    reference, native = RendezvousHash(), NativeRendezvousHash()
    for node in nodes:
        reference.add_node(node)
        native.add_node(node)
    return reference, native


@given(nodes=servers, key=st.text(max_size=300))
def test_same_server_as_pymemcache_for_any_str_key(nodes: list[str], key: str) -> None:
    # Any text, non-ASCII included: pymemcache hashes each character's low 8 bits.
    reference, native = both(nodes)
    assert native.get_node(key) == reference.get_node(key)


@given(nodes=servers, key=st.from_regex(r"[!-~]{1,250}", fullmatch=True))
def test_same_server_as_pymemcache_for_memcached_keys(nodes: list[str], key: str) -> None:
    # The keys HashClient actually routes: printable ASCII, no spaces, at most 250 characters.
    reference, native = both(nodes)
    assert native.get_node(key) == reference.get_node(key)


@given(nodes=servers, key=st.binary(max_size=64))
def test_bytes_key_falls_back_to_pymemcache(nodes: list[str], key: bytes) -> None:
    reference, native = both(nodes)
    assert native.get_node(key) == reference.get_node(key)


@given(data=st.text(max_size=300), seed=st.integers(0, 2**32 - 1))
def test_native_murmur3_is_pymemcache_murmur3(data: str, seed: int) -> None:
    # One node "" scores f"-{data}", so compare rendezvous_node's pick against a hand scoring.
    nodes = ["", data]
    want = max(nodes, key=lambda n: (murmur3_32(f"{n}-k", seed), n))
    assert rendezvous_node(nodes, "k", seed) == want


@pytest.mark.parametrize("order", [["mc46898:11211", "mc61399:11211"], ["mc61399:11211", "mc46898:11211"]])
def test_score_tie_goes_to_the_greater_string(order: list[str]) -> None:
    # These two nodes score the same for key "k" (a murmur3 collision, found by brute force).
    assert murmur3_32("mc46898:11211-k") == murmur3_32("mc61399:11211-k")
    reference, native = both(order)
    assert reference.get_node("k") == "mc61399:11211"
    assert native.get_node("k") == "mc61399:11211"


def test_no_nodes_and_one_node() -> None:
    native = NativeRendezvousHash()
    assert native.get_node("k") is None
    native.add_node("mc1:11211")
    assert native.get_node("k") == "mc1:11211"
    native.remove_node("mc1:11211")
    assert native.get_node("k") is None


class CacheKey(str, Enum):  # noqa: UP042 - the mixin formats as "CacheKey.USER"; StrEnum does not
    USER = "user"


class StrKey(StrEnum):
    USER = "user"


class Name(str):
    def __format__(self, spec: str) -> str:
        return "renamed"


THREE = ["mc1:11211", "mc2:11211", "mc3:11211"]


def test_str_subclass_key_is_formatted_as_pymemcache_formats_it() -> None:
    # pymemcache hashes f"{node}-{key}", so a (str, Enum) key hashes as "CacheKey.USER", not "user".
    reference, native = both(THREE)
    for key in (CacheKey.USER, StrKey.USER, Name("user")):
        assert native.get_node(key) == reference.get_node(key)
    assert reference.get_node(CacheKey.USER) != reference.get_node("user")  # the case that moved


def test_str_subclass_node_is_formatted_as_pymemcache_formats_it() -> None:
    reference, native = RendezvousHash(), NativeRendezvousHash()
    for node in [Name("mc1:11211"), "mc2:11211", "mc3:11211"]:
        reference.add_node(node)
        native.add_node(node)
    for key in ("a", "b", "c", "d"):
        assert native.get_node(key) == reference.get_node(key)


def test_backend_routes_a_str_enum_key_like_a_default_hash_client() -> None:
    backend = MemcachedBackend(MemcachedBackendConfig(servers=THREE))
    default = HashClient(servers=[_parse_server(n) for n in THREE], use_pooling=True)
    assert backend._client._get_client(CacheKey.USER).server == default._get_client(CacheKey.USER).server


def test_last_server_removed_after_the_length_check_picks_no_server() -> None:
    class Emptied(list):
        """A node list another thread emptied between get_node's length check and its index."""

        def __len__(self) -> int:
            return 1

    native = NativeRendezvousHash()
    native.nodes = Emptied()
    assert native.get_node("k") is None


def test_non_str_node_falls_back_to_pymemcache() -> None:
    reference, native = RendezvousHash(), NativeRendezvousHash()
    for node in [("mc1", 11211), "mc2:11211"]:
        reference.add_node(node)
        native.add_node(node)
    assert native.get_node("k") == reference.get_node("k")


@given(nodes=servers, keys=st.lists(st.from_regex(r"[!-~]{1,250}", fullmatch=True), max_size=20))
def test_backend_routes_like_a_default_hash_client(nodes: list[str], keys: list[str]) -> None:
    backend = MemcachedBackend(MemcachedBackendConfig(servers=nodes))
    default = HashClient(servers=[_parse_server(n) for n in nodes], use_pooling=True)
    assert isinstance(backend._client.hasher, NativeRendezvousHash)
    for key in keys:
        assert backend._client._get_client(key).server == default._get_client(key).server
