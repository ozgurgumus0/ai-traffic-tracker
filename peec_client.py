#!/usr/bin/env python3
"""Minimal Peec AI client over the Peec MCP server (https://api.peec.ai/mcp).

The Peec REST "Customer API" only accepts company/project-scoped keys; the token
we have is a Personal Access Token (PAT), which Peec accepts on its MCP server via
`Authorization: Bearer <PAT>`. Rather than register the MCP server in Claude Code,
we speak the MCP Streamable-HTTP protocol directly with `requests`:

    initialize  ->  notifications/initialized  ->  tools/call

Every report tool returns a {"columns": [...], "rows": [[...]]} table as JSON text
inside the MCP content block; `PeecClient.table()` parses that into list[dict].

    from peec_client import PeecClient
    pc = PeecClient()                      # reads PEEC_PAT from env
    projects = pc.table("list_projects", {})
    rep = pc.table("get_brand_report",
                   {"project_id": pid, "start_date": s, "end_date": e})
"""
from __future__ import annotations

import json
import os

import requests

MCP_URL = "https://api.peec.ai/mcp"
PROTOCOL_VERSION = "2025-06-18"


class PeecError(RuntimeError):
    pass


class PeecClient:
    def __init__(self, token: str | None = None, timeout: int = 120):
        self.token = token or os.environ.get("PEEC_PAT") or os.environ.get("PEEC_API_KEY")
        if not self.token:
            raise PeecError("no PEEC_PAT in environment")
        self.timeout = timeout
        self.session_id: str | None = None
        self._id = 0
        self._handshake()

    # -- transport ---------------------------------------------------------
    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.token}",
             "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse(resp) -> list[dict]:
        """MCP Streamable HTTP replies as JSON or as SSE (text/event-stream).
        Decode the body as UTF-8 explicitly: text/event-stream carries no charset,
        so requests would otherwise fall back to ISO-8859-1 and mangle accented
        Italian (e.g. 'più' -> 'piÃ¹')."""
        text = resp.content.decode("utf-8", "replace")
        if "event-stream" in resp.headers.get("content-type", ""):
            out = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        out.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        pass
            return out
        try:
            return [json.loads(text)]
        except ValueError:
            raise PeecError(f"non-JSON reply: {text[:200]}")

    def _post(self, body: dict):
        return requests.post(MCP_URL, headers=self._headers(), json=body, timeout=self.timeout)

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        resp = self._post(body)
        if resp.status_code >= 400 and not self.session_id:
            raise PeecError(f"{method} -> HTTP {resp.status_code}: {resp.text[:200]}")
        for msg in self._parse(resp):
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise PeecError(f"{method}: {json.dumps(msg['error'])[:300]}")
                return msg.get("result", {})
        return {}

    def _handshake(self) -> None:
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                           "clientInfo": {"name": "ai-traffic-tracker", "version": "1.0"}}}
        resp = self._post(body)
        if resp.status_code >= 400:
            raise PeecError(f"initialize -> HTTP {resp.status_code}: {resp.text[:200]}")
        self.session_id = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        self._id = 1
        # required notification before any tools/call
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # -- tools -------------------------------------------------------------
    def call(self, name: str, arguments: dict | None = None):
        """Call an MCP tool; return the parsed JSON of its first content block
        (a dict), or the raw text if it isn't JSON."""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content") or []
        if not content:
            return result
        text = content[0].get("text", "")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    def table(self, name: str, arguments: dict | None = None) -> list[dict]:
        """Call a report/list tool and return its rows as list[dict] keyed by
        the response's `columns`. Empty list if the shape isn't a table."""
        data = self.call(name, arguments)
        if not isinstance(data, dict) or "columns" not in data or "rows" not in data:
            return []
        cols = data["columns"]
        return [dict(zip(cols, row)) for row in data["rows"]]


if __name__ == "__main__":
    # smoke test: list projects
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent / ".env")
    pc = PeecClient()
    for p in pc.table("list_projects", {}):
        print(p)
