from __future__ import annotations


class MusicKBError(Exception):
    """Base exception with a stable machine-readable code."""

    code = "music_kb_error"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ValidationError(MusicKBError):
    code = "validation_error"


class DatabaseNotInitializedError(MusicKBError):
    code = "database_not_initialized"


class NotFoundError(MusicKBError):
    code = "not_found"


class SnapshotVerificationError(MusicKBError):
    code = "snapshot_verification_failed"


class ReadOnlyError(MusicKBError):
    code = "read_only"
