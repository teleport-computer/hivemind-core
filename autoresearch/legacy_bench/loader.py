"""Parse ChatGPT conversation export and load into Postgres via the live Hivemind API."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    seq: int


@dataclass
class Conversation:
    number: int
    title: str
    date: str  # ISO date string
    message_count: int
    word_count: int
    messages: list[Message] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_CONV_HEADER = re.compile(
    r"^Conversation\s+(\d+):\s+(.+)$"
)
_DATE_LINE = re.compile(r"^Date:\s+(.+)$")
_META_LINE = re.compile(r"^Messages:\s+(\d+),\s+Words:\s+(\d+)$")
_MSG_START = re.compile(r"^\[(User|ChatGPT)\]\s*(.*)", re.DOTALL)
_SEPARATOR = re.compile(r"^={10,}$")


def parse_conversations(filepath: str) -> list[Conversation]:
    """Parse the ChatGPT export text file into Conversation objects."""
    conversations: list[Conversation] = []
    current: Conversation | None = None
    current_msg_role: str | None = None
    current_msg_lines: list[str] = []
    msg_seq = 0

    def _flush_message():
        nonlocal current_msg_role, current_msg_lines, msg_seq
        if current is not None and current_msg_role is not None:
            content = "\n".join(current_msg_lines).strip()
            if content:
                role = "user" if current_msg_role == "User" else "assistant"
                current.messages.append(Message(role=role, content=content, seq=msg_seq))
                msg_seq += 1
        current_msg_role = None
        current_msg_lines = []

    with open(filepath, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")

            # Separator between conversations
            if _SEPARATOR.match(line):
                _flush_message()
                continue

            # Conversation header
            m = _CONV_HEADER.match(line)
            if m:
                _flush_message()
                if current is not None:
                    conversations.append(current)
                current = Conversation(
                    number=int(m.group(1)),
                    title=m.group(2).strip(),
                    date="",
                    message_count=0,
                    word_count=0,
                )
                msg_seq = 0
                continue

            if current is None:
                continue

            # Date line
            m = _DATE_LINE.match(line)
            if m:
                # Extract just the date part (YYYY-MM-DD)
                raw_date = m.group(1).strip()
                current.date = raw_date[:10] if len(raw_date) >= 10 else raw_date
                continue

            # Messages/Words metadata
            m = _META_LINE.match(line)
            if m:
                current.message_count = int(m.group(1))
                current.word_count = int(m.group(2))
                continue

            # Dashes separator within a conversation header
            if re.match(r"^-{5,}$", line):
                continue

            # Message start
            m = _MSG_START.match(line)
            if m:
                _flush_message()
                current_msg_role = m.group(1)
                rest = m.group(2)
                if rest:
                    current_msg_lines.append(rest)
                continue

            # Continuation of current message
            if current_msg_role is not None:
                current_msg_lines.append(line)

    # Flush last message and conversation
    _flush_message()
    if current is not None:
        conversations.append(current)

    return conversations


# ---------------------------------------------------------------------------
# Loader — sends data to running Hivemind server via /v1/store
# ---------------------------------------------------------------------------

async def _store(client: httpx.AsyncClient, url: str, sql: str, params: list) -> dict:
    """Execute a single /v1/store call."""
    resp = await client.post(
        f"{url}/v1/store",
        json={"sql": sql, "params": params},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


async def create_tables(base_url: str, api_key: str | None = None) -> None:
    """Create conversations and messages tables via the API."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(headers=headers) as client:
        # Drop existing tables (messages first due to FK)
        await _store(client, base_url, "DROP TABLE IF EXISTS messages", [])
        await _store(client, base_url, "DROP TABLE IF EXISTS conversations", [])

        # Use the conversation's own number as the primary key — avoids needing
        # RETURNING id (which the /v1/store endpoint doesn't propagate).
        await _store(client, base_url, """
            CREATE TABLE conversations (
                id INT PRIMARY KEY,
                title TEXT,
                date DATE,
                message_count INT,
                word_count INT
            )
        """, [])

        await _store(client, base_url, """
            CREATE TABLE messages (
                conversation_id INT REFERENCES conversations(id),
                seq INT,
                role TEXT,
                content TEXT,
                PRIMARY KEY (conversation_id, seq)
            )
        """, [])


async def load_conversations(
    conversations: list[Conversation],
    base_url: str,
    api_key: str | None = None,
    batch_size: int = 50,
    max_convos: int | None = None,
) -> dict:
    """Load parsed conversations into the database via /v1/store.

    Returns summary stats.
    """
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if max_convos is not None:
        conversations = conversations[:max_convos]

    total_convos = 0
    total_messages = 0

    async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
        for i in range(0, len(conversations), batch_size):
            batch = conversations[i : i + batch_size]
            for conv in batch:
                conv_id = conv.number

                # Insert conversation — date may be empty for malformed entries
                date_val = conv.date if conv.date else None
                await _store(client, base_url, """
                    INSERT INTO conversations (id, title, date, message_count, word_count)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                """, [conv_id, conv.title, date_val, conv.message_count, conv.word_count])
                total_convos += 1

                # Insert messages
                for msg in conv.messages:
                    await _store(client, base_url, """
                        INSERT INTO messages (conversation_id, seq, role, content)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (conversation_id, seq) DO NOTHING
                    """, [conv_id, msg.seq, msg.role, msg.content])
                    total_messages += 1

            loaded = min(i + batch_size, len(conversations))
            print(f"  Loaded {loaded}/{len(conversations)} conversations ({total_messages} messages)")

    return {
        "conversations_loaded": total_convos,
        "messages_loaded": total_messages,
    }
