"""Persistent state of the public server, in one SQLite file.

What is in it, and how it is protected (manifesto rule 18):
- persons: the person's VK Teams session, sealed with AES-GCM under a key derived from the operator's
  secret; the person id is the associated data, so a sealed row moved to another person does not open.
- tokens, codes: SHA-256 of the value only; the value itself exists in the client and nowhere here.
- clients: dynamic client registrations (public metadata).
- requests: sign-ins in progress (the client's authorize parameters), short-lived.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from vkteams_mcp.api.session import VKSession

SCHEMA = """
create table if not exists persons(person_id text primary key, host text not null, sealed blob not null,
                                   updated real not null);
create table if not exists clients(client_id text primary key, info text not null, created real not null);
create table if not exists requests(request_id text primary key, client_id text not null, params text not null,
                                    expires real not null);
create table if not exists codes(code_hash text primary key, client_id text not null, person_id text not null,
                                 data text not null, expires real not null);
create table if not exists tokens(token_hash text primary key, kind text not null, client_id text not null,
                                  person_id text not null, family text not null, scopes text not null,
                                  resource text, expires real not null);
create index if not exists tokens_person on tokens(person_id);
create table if not exists drafts(person_id text not null, chat text not null, data blob not null,
                                  expires real not null, primary key(person_id, chat));
create index if not exists tokens_family on tokens(family);
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("the store secret must be at least 32 characters")
        self._aead = AESGCM(
            HKDF(algorithm=hashes.SHA256(), length=32, salt=b"vkteams-mcp/v1", info=b"session-seal").derive(
                secret.encode()
            )
        )
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("pragma journal_mode=wal")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        if str(path) != ":memory:":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def _x(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ---------------------------------------------------------------- persons
    @staticmethod
    def person_id(session: VKSession) -> str:
        return digest(f"{session.host}\n{session.login.strip().lower()}")[:32]

    def save_session(self, session: VKSession) -> str:
        pid = self.person_id(session)
        nonce = os.urandom(12)
        sealed = nonce + self._aead.encrypt(nonce, session.to_json().encode(), pid.encode())

        self._x(
            "insert into persons(person_id, host, sealed, updated) values(?,?,?,?) on conflict(person_id) "
            "do update set sealed=excluded.sealed, host=excluded.host, updated=excluded.updated",
            (pid, session.host, sealed, time.time()),
        )
        return pid

    def load_session(self, person_id: str) -> VKSession | None:
        rows = self._x("select sealed from persons where person_id=?", (person_id,))
        if not rows:
            return None
        blob = rows[0][0]
        try:
            return VKSession.from_json(self._aead.decrypt(blob[:12], blob[12:], person_id.encode()).decode())
        except Exception:
            return None

    def forget_person(self, person_id: str) -> None:
        self._x("delete from tokens where person_id=?", (person_id,))
        self._x("delete from codes where person_id=?", (person_id,))
        self._x("delete from drafts where person_id=?", (person_id,))
        self._x("delete from persons where person_id=?", (person_id,))

    # ---------------------------------------------------------------- clients
    def save_client(self, client_id: str, info: str, now: float) -> None:
        self._x("insert or replace into clients(client_id, info, created) values(?,?,?)", (client_id, info, now))

    def client(self, client_id: str) -> str | None:
        rows = self._x("select info from clients where client_id=?", (client_id,))
        return rows[0][0] if rows else None

    # ---------------------------------------------------------------- sign-in requests
    def save_request(self, request_id: str, client_id: str, params: dict, expires: float) -> None:
        self._x(
            "insert into requests(request_id, client_id, params, expires) values(?,?,?,?)",
            (digest(request_id), client_id, json.dumps(params), expires),
        )

    def take_request(self, request_id: str, now: float) -> tuple[str, dict] | None:
        """The request, once: it is deleted as it is read."""
        h = digest(request_id)
        with self._lock:
            rows = self._db.execute(
                "select client_id, params, expires from requests where request_id=?", (h,)
            ).fetchall()
            self._db.execute("delete from requests where request_id=?", (h,))
        if not rows or rows[0][2] < now:
            return None
        return rows[0][0], json.loads(rows[0][1])

    def request_info(self, request_id: str, now: float) -> tuple[str, dict] | None:
        """The client and parameters of a pending sign-in, without using it up."""
        rows = self._x("select client_id, params, expires from requests where request_id=?", (digest(request_id),))
        if not rows or rows[0][2] < now:
            return None
        return rows[0][0], json.loads(rows[0][1])

    def peek_request(self, request_id: str, now: float) -> bool:
        rows = self._x("select expires from requests where request_id=?", (digest(request_id),))
        return bool(rows) and rows[0][0] >= now

    # ---------------------------------------------------------------- codes
    def save_code(self, code: str, client_id: str, person_id: str, data: dict, expires: float) -> None:
        self._x(
            "insert into codes(code_hash, client_id, person_id, data, expires) values(?,?,?,?,?)",
            (digest(code), client_id, person_id, json.dumps(data), expires),
        )

    def code(self, code: str) -> tuple[str, str, dict, float] | None:
        rows = self._x("select client_id, person_id, data, expires from codes where code_hash=?", (digest(code),))
        return (rows[0][0], rows[0][1], json.loads(rows[0][2]), rows[0][3]) if rows else None

    def delete_code(self, code: str) -> None:
        self._x("delete from codes where code_hash=?", (digest(code),))

    # ---------------------------------------------------------------- tokens
    def save_token(
        self,
        token: str,
        kind: str,
        client_id: str,
        person_id: str,
        family: str,
        scopes: list[str],
        resource: str | None,
        expires: float,
    ) -> None:
        self._x(
            "insert into tokens(token_hash, kind, client_id, person_id, family, scopes, resource, expires) "
            "values(?,?,?,?,?,?,?,?)",
            (digest(token), kind, client_id, person_id, family, json.dumps(scopes), resource, expires),
        )

    def token(self, token: str, kind: str) -> dict | None:
        rows = self._x(
            "select client_id, person_id, family, scopes, resource, expires from tokens where token_hash=? and kind=?",
            (digest(token), kind),
        )
        if not rows:
            return None
        c, p, f, s, r, e = rows[0]
        return {"client_id": c, "person_id": p, "family": f, "scopes": json.loads(s), "resource": r, "expires": e}

    def revoke_family(self, family: str) -> None:
        """Delete the live tokens of a chain; the used-refresh markers stay so a later replay is still caught."""
        self._x("delete from tokens where family=? and kind in ('access', 'refresh')", (family,))

    # ---------------------------------------------------------------- drafts (server-side, never draft/set)
    def _seal(self, plain: bytes, aad: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, plain, aad.encode())

    def _open(self, blob: bytes, aad: str) -> bytes | None:
        try:
            return self._aead.decrypt(blob[:12], blob[12:], aad.encode())
        except Exception:
            return None

    def save_draft(self, person_id: str, chat: str, data: dict, expires: float) -> None:
        # Sealed like a session: a draft is the person's words, and the chat id says to whom.
        sealed = self._seal(json.dumps(data).encode(), f"draft\n{person_id}\n{chat}")
        self._x(
            "insert or replace into drafts(person_id, chat, data, expires) values(?,?,?,?)",
            (person_id, digest(chat), sealed, expires),
        )

    def draft(self, person_id: str, chat: str, now: float) -> dict | None:
        rows = self._x("select data, expires from drafts where person_id=? and chat=?", (person_id, digest(chat)))
        if not rows or rows[0][1] < now:
            return None
        plain = self._open(rows[0][0], f"draft\n{person_id}\n{chat}")
        return json.loads(plain) if plain is not None else None

    def delete_draft(self, person_id: str, chat: str, version: str | None = None) -> None:
        """Delete the draft; with a version, only if it is still that version (a newer save survives)."""
        if version is None:
            self._x("delete from drafts where person_id=? and chat=?", (person_id, digest(chat)))
            return
        current = self.draft(person_id, chat, 0)
        if current is not None and current.get("version") == version:
            self._x("delete from drafts where person_id=? and chat=?", (person_id, digest(chat)))

    def purge(self, now: float) -> None:
        for table in ("tokens", "codes", "requests", "drafts"):
            self._x(f"delete from {table} where expires < ?", (now,))  # noqa: S608 - table from the tuple above
