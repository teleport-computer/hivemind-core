"""Tape recorder and replay for bridge LLM calls.

Records LLM request/response pairs during agent runs. On subsequent runs,
serves cached responses when request hashes match — enabling the scope agent
to "rewind" query agent executions cheaply.

Thread safety: Tape assumes external synchronization (e.g., BridgeServer._llm_lock).
"""

import hashlib
import json
from dataclasses import dataclass, field


def _canonical_json(obj) -> str:
    """Deterministic JSON: sorted keys, no whitespace, ensure_ascii."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def hash_request(kwargs: dict) -> str:
    """SHA-256 hex digest (first 16 chars) of canonicalized request kwargs."""
    canonical = _canonical_json(kwargs)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class TapeEntry:
    """One recorded LLM request/response pair."""

    request_hash: str
    response: dict  # full result dict from llm_caller
    request_kwargs: dict  # original request kwargs (for debugging)


@dataclass
class Tape:
    """Ordered sequence of LLM call recordings with replay support."""

    entries: list[TapeEntry] = field(default_factory=list)
    _replay_cursor: int = field(default=0, init=False, repr=False)
    _replay_active: bool = field(default=False, init=False, repr=False)

    def enable_replay(self) -> None:
        """Enable replay mode. No-op if tape is empty."""
        if self.entries:
            self._replay_active = True
            self._replay_cursor = 0

    def try_replay(self, request_hash: str) -> dict | None:
        """Try to serve a cached response for the given request hash.

        Returns the cached response dict if the next tape entry matches.
        Returns None (and permanently disables replay) on mismatch or exhaustion.
        """
        if not self._replay_active:
            return None
        if self._replay_cursor >= len(self.entries):
            self._replay_active = False
            return None
        entry = self.entries[self._replay_cursor]
        if entry.request_hash != request_hash:
            self._replay_active = False
            return None
        self._replay_cursor += 1
        return entry.response

    def record(
        self, request_hash: str, request_kwargs: dict, response: dict,
    ) -> None:
        """Append a new entry to the tape."""
        self.entries.append(
            TapeEntry(
                request_hash=request_hash,
                response=response,
                request_kwargs=request_kwargs,
            )
        )

    @property
    def is_replaying(self) -> bool:
        """Whether replay mode is currently active."""
        return self._replay_active

    def to_json(self) -> list[dict]:
        """Serialize to JSON-safe list for wire transport."""
        return [
            {
                "request_hash": e.request_hash,
                "response": e.response,
                "request_kwargs": e.request_kwargs,
            }
            for e in self.entries
        ]

    @classmethod
    def from_json(cls, data: list[dict]) -> "Tape":
        """Deserialize from JSON list."""
        entries = [
            TapeEntry(
                request_hash=d["request_hash"],
                response=d["response"],
                request_kwargs=d.get("request_kwargs", {}),
            )
            for d in data
        ]
        return cls(entries=entries)
