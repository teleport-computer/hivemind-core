import pytest

from hivemind.sandbox.tape import Tape, TapeEntry, hash_request


class TestHashRequest:
    def test_consistent(self):
        kwargs = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}
        assert hash_request(kwargs) == hash_request(kwargs)

    def test_key_order_insensitive(self):
        a = {"max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
        b = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}
        assert hash_request(a) == hash_request(b)

    def test_different_inputs_different_hashes(self):
        a = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}
        b = {"messages": [{"role": "user", "content": "world"}], "max_tokens": 100}
        assert hash_request(a) != hash_request(b)

    def test_nested_dict_order_insensitive(self):
        a = {"messages": [{"content": "hi", "role": "user"}], "max_tokens": 100}
        b = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}
        assert hash_request(a) == hash_request(b)

    def test_returns_hex_string(self):
        h = hash_request({"messages": [], "max_tokens": 1})
        assert len(h) == 16
        int(h, 16)  # should not raise


class TestTapeRecord:
    def test_record_appends(self):
        tape = Tape()
        tape.record("hash1", {"messages": []}, {"content": "a"})
        tape.record("hash2", {"messages": []}, {"content": "b"})
        assert len(tape.entries) == 2
        assert tape.entries[0].request_hash == "hash1"
        assert tape.entries[1].response == {"content": "b"}

    def test_empty_tape(self):
        tape = Tape()
        assert len(tape.entries) == 0


class TestTapeSerialization:
    def test_roundtrip(self):
        tape = Tape()
        tape.record("h1", {"messages": [{"role": "user", "content": "hi"}]}, {"content": "resp1"})
        tape.record("h2", {"messages": [{"role": "user", "content": "bye"}]}, {"content": "resp2"})

        data = tape.to_json()
        restored = Tape.from_json(data)

        assert len(restored.entries) == 2
        assert restored.entries[0].request_hash == "h1"
        assert restored.entries[0].response == {"content": "resp1"}
        assert restored.entries[1].request_kwargs == {"messages": [{"role": "user", "content": "bye"}]}

    def test_roundtrip_empty(self):
        tape = Tape()
        data = tape.to_json()
        assert data == []
        restored = Tape.from_json(data)
        assert len(restored.entries) == 0

    def test_from_json_missing_request_kwargs(self):
        data = [{"request_hash": "h1", "response": {"content": "r"}}]
        tape = Tape.from_json(data)
        assert tape.entries[0].request_kwargs == {}


class TestTapeReplay:
    def _make_tape(self):
        tape = Tape()
        tape.record("hash_a", {"k": "a"}, {"content": "response_a"})
        tape.record("hash_b", {"k": "b"}, {"content": "response_b"})
        tape.record("hash_c", {"k": "c"}, {"content": "response_c"})
        return tape

    def test_replay_not_enabled_returns_none(self):
        tape = self._make_tape()
        assert tape.try_replay("hash_a") is None

    def test_replay_match_returns_cached(self):
        tape = self._make_tape()
        tape.enable_replay()
        result = tape.try_replay("hash_a")
        assert result == {"content": "response_a"}

    def test_replay_advances_cursor(self):
        tape = self._make_tape()
        tape.enable_replay()
        tape.try_replay("hash_a")
        result = tape.try_replay("hash_b")
        assert result == {"content": "response_b"}

    def test_replay_mismatch_disables_permanently(self):
        tape = self._make_tape()
        tape.enable_replay()
        result = tape.try_replay("wrong_hash")
        assert result is None
        assert tape.is_replaying is False
        # Subsequent calls also return None even with correct hash
        assert tape.try_replay("hash_a") is None

    def test_replay_exhaustion_disables(self):
        tape = Tape()
        tape.record("h1", {}, {"content": "only"})
        tape.enable_replay()
        tape.try_replay("h1")  # consume the one entry
        result = tape.try_replay("anything")
        assert result is None
        assert tape.is_replaying is False

    def test_empty_tape_enable_replay_noop(self):
        tape = Tape()
        tape.enable_replay()
        assert tape.is_replaying is False
        assert tape.try_replay("anything") is None

    def test_is_replaying_property(self):
        tape = self._make_tape()
        assert tape.is_replaying is False
        tape.enable_replay()
        assert tape.is_replaying is True
        tape.try_replay("wrong")
        assert tape.is_replaying is False

    def test_full_replay_then_exhaustion(self):
        tape = self._make_tape()
        tape.enable_replay()
        assert tape.try_replay("hash_a") == {"content": "response_a"}
        assert tape.try_replay("hash_b") == {"content": "response_b"}
        assert tape.try_replay("hash_c") == {"content": "response_c"}
        assert tape.is_replaying is True  # still active until next call
        assert tape.try_replay("hash_d") is None  # now exhausted
        assert tape.is_replaying is False
