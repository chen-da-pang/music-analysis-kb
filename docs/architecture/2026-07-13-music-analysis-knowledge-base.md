# Architecture decision: local SQLite snapshots

The knowledge base uses a local single-writer SQLite master database and
immutable read-only SQLite snapshots distributed by SSH/rsync. This is the
right first architecture for text-heavy Music Flamingo results, weekly updates,
private distribution, and 100k-scale exact tag retrieval.

The public query surface is FTS5 plus normalized tag, alias, title, and artist
indexes. `sqlite-vec` is deliberately deferred until a real fuzzy-style-search
requirement is measured. Object storage, cloud databases, Feigua metadata, and
multi-writer collaboration are out of scope.

The full operational implementation and contracts are documented in the
repository README and linked documents.
