import json
from dataclasses import dataclass
from typing import Callable

from .models import Scope
from .storage import Storage

# Default chunk size for read_record (chars). Agent can request smaller/larger.
DEFAULT_READ_CHUNK = 20_000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., str]

    def to_openai_def(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def build_agent_file_tools(agent_store, query_agent_id: str) -> list[Tool]:
    """Build tools for scoping agents to inspect a query agent's source code.

    The tools are pre-scoped to the specific query agent being evaluated.
    The scoping agent doesn't need to know the agent ID — it just calls
    "list the query agent's files" and "read a file."
    """

    def list_query_agent_files() -> str:
        files = agent_store.list_file_paths(query_agent_id)
        if not files:
            return json.dumps({
                "files": [],
                "note": "No source files extracted for this agent. "
                "The image may contain only compiled binaries.",
            })
        return json.dumps({"files": files})

    def read_query_agent_file(file_path: str) -> str:
        content = agent_store.read_file(query_agent_id, file_path)
        if content is None:
            return "File not found. Use list_query_agent_files to see available files."
        return content

    return [
        Tool(
            name="list_query_agent_files",
            description=(
                "List all source files extracted from the query agent's Docker image. "
                "Returns file paths and sizes. Use this to understand what code the "
                "query agent contains before deciding which records it should access."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=list_query_agent_files,
        ),
        Tool(
            name="read_query_agent_file",
            description=(
                "Read the contents of a specific source file from the query agent's "
                "Docker image. Use this to inspect the agent's code and determine "
                "whether it can be trusted with specific records."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path of the file to read (from list_query_agent_files)",
                    },
                },
                "required": ["file_path"],
            },
            handler=read_query_agent_file,
        ),
    ]


def build_tools(
    storage: Storage, scope: Scope, space_id: str | None = None
) -> list[Tool]:
    def search_index(query: str, limit: int = 20, user_id: str | None = None) -> str:
        results = storage.search_index(query, scope, space_id, limit)
        if user_id:
            results = [r for r in results if r.get("user_id") == user_id]
        return json.dumps(results, default=str)

    def read_record(
        record_id: str, offset: int = 0, limit: int = DEFAULT_READ_CHUNK
    ) -> str:
        record = storage.read_record(record_id, scope)
        if record is None:
            return "Record not found"
        text = record["text"]
        total = len(text)
        chunk = text[offset : offset + limit]

        # Include metadata header on the first chunk
        header = ""
        if offset == 0:
            meta_parts = [f"record_id: {record['id']}"]
            if record.get("user_id"):
                meta_parts.append(f"user_id: {record['user_id']}")
            if record.get("space_id"):
                meta_parts.append(f"space_id: {record['space_id']}")
            header = "[" + ", ".join(meta_parts) + "]\n\n"

        # Tell the agent how much is left so it can request more
        if offset + limit < total:
            remaining = total - offset - limit
            return (
                f"{header}{chunk}\n\n--- offset {offset}, showing {len(chunk)} of "
                f"{total} chars, {remaining} remaining. "
                f"Call read_record again with offset={offset + limit} to continue. ---"
            )
        return f"{header}{chunk}" if header else chunk

    def list_index(limit: int = 20, offset: int = 0) -> str:
        results = storage.list_index(scope, space_id, limit, offset)
        return json.dumps(results, default=str)

    def list_by_user(user_id: str, limit: int = 50, offset: int = 0) -> str:
        results = storage.list_by_user(user_id, scope, limit, offset)
        return json.dumps(results, default=str)

    def list_users(limit: int = 100) -> str:
        results = storage.list_users(scope)
        return json.dumps(results[:limit], default=str)

    return [
        Tool(
            name="search_index",
            description=(
                "Search the knowledge base using a text query. "
                "Returns matching record summaries with IDs, user_ids, and space_ids. "
                "Optionally filter by user_id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                    "user_id": {
                        "type": "string",
                        "description": "Filter results to records owned by this user (optional)",
                    },
                },
                "required": ["query"],
            },
            handler=search_index,
        ),
        Tool(
            name="read_record",
            description=(
                "Read the text of a record by ID. Returns metadata (user_id, space_id) "
                "and the text content. For large records, returns a chunk "
                "and tells you how to fetch the next chunk with offset. "
                "Use offset/limit to page through long documents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "record_id": {
                        "type": "string",
                        "description": "The record ID to read",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset to start reading from (default 0)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Max characters to return (default {DEFAULT_READ_CHUNK})",
                        "default": DEFAULT_READ_CHUNK,
                    },
                },
                "required": ["record_id"],
            },
            handler=read_record,
        ),
        Tool(
            name="list_index",
            description=(
                "Browse recent indexed records. "
                "Returns summaries with user_ids and space_ids, sorted by most recent."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip first N results (default 0)",
                        "default": 0,
                    },
                },
                "required": [],
            },
            handler=list_index,
        ),
        Tool(
            name="list_by_user",
            description=(
                "List all records owned by a specific user. "
                "Returns summaries sorted by most recent."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "The user ID to list records for",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip first N results (default 0)",
                        "default": 0,
                    },
                },
                "required": ["user_id"],
            },
            handler=list_by_user,
        ),
        Tool(
            name="list_users",
            description=(
                "List all users who have contributed records, with record counts. "
                "Useful for discovering who has data in the knowledge base."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 100)",
                        "default": 100,
                    },
                },
                "required": [],
            },
            handler=list_users,
        ),
    ]
