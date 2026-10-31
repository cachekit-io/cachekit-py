"""The Memcached backend's node choice: pymemcache's rendezvous hashing, without its pure-Python hash.

pymemcache's default ``RendezvousHash`` runs its pure-Python ``murmur3_32`` once per server on every
get, set and delete: 16 µs an operation with one server, 48 µs with three. This hasher picks the
same server for every key, since a different pick moves every cached key to another server: one
server is returned without hashing, and several are scored by the same murmur3 in the Rust
extension. Imported only with pymemcache installed (the ``memcached`` extra).
"""

from __future__ import annotations

from typing import Any

from pymemcache.client.rendezvous import RendezvousHash

from cachekit._rust_serializer import rendezvous_node


class NativeRendezvousHash(RendezvousHash):
    """pymemcache's ``RendezvousHash`` with ``get_node`` answered natively; its node list is kept as is.

    ``add_node`` and ``remove_node`` are pymemcache's, so ``HashClient``'s dead-server handling
    edits the same ``nodes`` list it always has.
    """

    def get_node(self, key: Any) -> Any:
        nodes: list[Any] = self.nodes
        if len(nodes) == 1:
            return nodes[0]
        node = rendezvous_node(nodes, key, self.seed)
        # None: no nodes, or a key or node that is not a str, which pymemcache formats itself.
        return super().get_node(key) if node is None else node
