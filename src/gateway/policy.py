# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Who may do what: the access policy of the bank-access gateway.

Pure functions, no I/O, so the rules can be tested exhaustively. The gateway
(``app.py``) asks :func:`classify` what a request is and :func:`decide` whether
a client may make it.

Deny by default: a route the policy does not recognise is admin-only. That is
what makes the gateway safe against routes Hindsight adds or forgets to guard.

This module runs inside the Hindsight image (Python 3.11): no newer syntax.
"""

from __future__ import annotations

import fnmatch
import hmac
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import unquote

# Routes anyone may call, authenticated or not, as with Hindsight itself.
PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/health/ready",
        "/health/live",
        "/version",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)

BANK_LIST_PATH = "/v1/default/banks"
_BANK_PREFIX = "/v1/default/banks/"
_MCP_PREFIX = "/mcp/"
# Streamable HTTP: POST sends messages, GET opens the event stream, DELETE ends
# the session.
_MCP_METHODS = frozenset({"POST", "GET", "DELETE"})

# POST routes under a bank that only read. Every other POST, and every PUT,
# PATCH and DELETE, writes. Checked against Hindsight 0.9.2's routes.
READ_POSTS = frozenset(
    {
        "memories/recall",
        "reflect",
        "health/llm",
        "document-transfer/export",
    }
)

# MCP tools on the single-bank endpoint (/mcp/{bank}/), whose bank comes from
# the URL. Any other tool, such as list_banks or create_bank, is refused.
READ_TOOLS = frozenset({"recall", "reflect", "list_mental_models", "get_mental_model"})
WRITE_TOOLS = frozenset(
    {
        "retain",
        "sync_retain",
        "create_mental_model",
        "update_mental_model",
        "delete_mental_model",
        "refresh_mental_model",
    }
)

_CLIENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class Kind(Enum):
    """What a request is, as far as access control is concerned."""

    PUBLIC = "public"
    BANK_LIST = "bank-list"
    BANK_READ = "bank-read"
    BANK_WRITE = "bank-write"
    MCP_BANK = "mcp-bank"
    ADMIN_ONLY = "admin-only"
    INVALID = "invalid"


@dataclass(frozen=True)
class Request:
    """The parts of a request the policy looks at."""

    kind: Kind
    bank: str | None = None
    reason: str = ""
    # A DELETE of the bank itself, not of something in it.
    deletes_bank: bool = False


@dataclass(frozen=True)
class Client:
    """A non-admin API key and the banks it may use."""

    name: str
    key: str
    banks: tuple[str, ...] = ()  # read and write
    read_banks: tuple[str, ...] = ()  # read only

    def can_write(self, bank: str) -> bool:
        return _matches(bank, self.banks)

    def can_read(self, bank: str) -> bool:
        return self.can_write(bank) or _matches(bank, self.read_banks)


@dataclass(frozen=True)
class Member:
    """A person, by the email their token names, and the banks granted to it.

    A member has a read-write client's access to exactly its granted banks
    (architecture.md§8), less deleting a bank, which :func:`decide` refuses.
    """

    name: str  # the email, as the refusal log names a client
    granted: frozenset[str] = frozenset()

    def can_write(self, bank: str) -> bool:
        return bank in self.granted

    def can_read(self, bank: str) -> bool:
        return bank in self.granted


@dataclass
class Policy:
    """Every key the gateway accepts."""

    admin_key: str
    clients: tuple[Client, ...] = field(default_factory=tuple)
    # The key that reaches only the grants API; empty while member access is off.
    grants_key: str = ""

    def identify(self, token: str | None) -> Client | str | None:
        """Return ``"admin"``, ``"grants"``, the matching :class:`Client`, or None.

        Compares against every key in constant time, so timing does not reveal
        which key a guess is close to.
        """
        if not token:
            return None
        found: Client | str | None = None
        if hmac.compare_digest(token.encode(), self.admin_key.encode()):
            found = "admin"
        if (
            self.grants_key
            and hmac.compare_digest(token.encode(), self.grants_key.encode())
            and found is None
        ):
            found = "grants"
        for client in self.clients:
            if hmac.compare_digest(token.encode(), client.key.encode()) and found is None:
                found = client
        return found


class PolicyError(ValueError):
    """The configured access policy is invalid."""


def _matches(bank: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(bank, pattern) for pattern in patterns)


def _bank_segment(raw: str) -> str | None:
    """Decode one path segment as a bank id, or None if it is not a safe one."""
    bank = unquote(raw)
    if not bank or bank in {".", ".."} or "/" in bank or "\\" in bank or "\x00" in bank:
        return None
    return bank


def classify(method: str, path: str) -> Request:
    """Classify a request by its method and *raw* (still percent-encoded) path."""
    method = method.upper()
    if "//" in path or "/./" in path or "/../" in path or path.endswith(("/.", "/..")):
        return Request(Kind.INVALID, reason="path is not normalised")

    if path in PUBLIC_PATHS and method in {"GET", "HEAD"}:
        return Request(Kind.PUBLIC)

    if path in {BANK_LIST_PATH, BANK_LIST_PATH + "/"} and method in {"GET", "HEAD"}:
        return Request(Kind.BANK_LIST)

    if path.startswith(_BANK_PREFIX):
        bank_raw, _, rest = path[len(_BANK_PREFIX) :].partition("/")
        bank = _bank_segment(bank_raw)
        if bank is None:
            return Request(Kind.INVALID, reason="invalid bank id")
        rest = rest.rstrip("/")
        if method in {"GET", "HEAD"} or (method == "POST" and rest in READ_POSTS):
            return Request(Kind.BANK_READ, bank=bank)
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            return Request(
                Kind.BANK_WRITE, bank=bank, deletes_bank=method == "DELETE" and not rest
            )
        return Request(Kind.ADMIN_ONLY, reason=f"method {method} on a bank")

    if path.startswith(_MCP_PREFIX) and path != _MCP_PREFIX:
        bank_raw = path[len(_MCP_PREFIX) :].partition("/")[0]
        bank = _bank_segment(bank_raw)
        if bank is None:
            return Request(Kind.INVALID, reason="invalid bank id")
        if method not in _MCP_METHODS:
            # Only a POST body is checked, so no other method may carry tool calls.
            return Request(Kind.ADMIN_ONLY, reason=f"method {method} on MCP")
        return Request(Kind.MCP_BANK, bank=bank)

    # Everything else: the multi-bank MCP endpoint, chunks and files fetched by
    # id across banks, metrics, templates, and any route added later.
    return Request(Kind.ADMIN_ONLY, reason="not a per-bank route")


def mcp_tools_called(body: bytes) -> list[str] | None:
    """Names of the tools a JSON-RPC message (or batch) calls; None if unreadable."""
    try:
        message = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        return None
    messages = message if isinstance(message, list) else [message]
    tools: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            return None
        if item.get("method") == "tools/call":
            params = item.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if not isinstance(name, str):
                return None
            tools.append(name)
    return tools


@dataclass(frozen=True)
class Decision:
    allowed: bool
    status: int = 200
    reason: str = ""


ALLOW = Decision(True)


def decide(client: Client | Member, request: Request, mcp_body: bytes | None = None) -> Decision:
    """Whether ``client`` may make ``request``; admins are not passed here."""
    kind, bank = request.kind, request.bank
    if kind is Kind.INVALID:
        return Decision(False, 400, request.reason)
    if isinstance(client, Member) and request.deletes_bank:
        return Decision(False, 403, "a member cannot delete a bank")
    if kind in {Kind.PUBLIC, Kind.BANK_LIST}:
        return ALLOW  # the gateway filters the list itself
    if kind is Kind.ADMIN_ONLY or bank is None:
        return Decision(False, 403, "this API key cannot use this endpoint")
    if kind is Kind.BANK_READ:
        return ALLOW if client.can_read(bank) else _no_bank(bank)
    if kind is Kind.BANK_WRITE:
        if client.can_write(bank):
            return ALLOW
        if client.can_read(bank):
            return Decision(False, 403, f"this API key has read-only access to bank {bank!r}")
        return _no_bank(bank)
    if kind is Kind.MCP_BANK:
        if not client.can_read(bank):
            return _no_bank(bank)
        if mcp_body is None:
            return ALLOW  # GET (event stream) or DELETE (end of session)
        tools = mcp_tools_called(mcp_body)
        if tools is None:
            return Decision(False, 400, "unreadable MCP request")
        for tool in tools:
            if tool in WRITE_TOOLS and not client.can_write(bank):
                return Decision(False, 403, f"this API key has read-only access to bank {bank!r}")
            if tool not in READ_TOOLS | WRITE_TOOLS:
                return Decision(False, 403, f"MCP tool {tool!r} is not available to this key")
        return ALLOW
    return Decision(False, 403, "this API key cannot use this endpoint")


def _no_bank(bank: str) -> Decision:
    return Decision(False, 403, f"this API key has no access to bank {bank!r}")


def visible_bank(client: Client | Member | str, bank: str) -> bool:
    return client == "admin" or (isinstance(client, Client | Member) and client.can_read(bank))


ADMIN_PREFIX = "/admin/"
_EMAIL = re.compile(r"^[^@\s/\\]+@[^@\s/\\]+$")


@dataclass(frozen=True)
class GrantsRoute:
    """A request to the grants API: ``email`` is lowercased, or None for the bank's routes."""

    action: str  # "bank", "grants" or "grant"
    bank: str
    email: str | None = None


def grants_route(method: str, path: str) -> GrantsRoute | Decision:
    """Parse a raw ``/admin/`` path, or refuse it with the :class:`Decision` to send."""
    if "//" in path or "/./" in path or "/../" in path or path.endswith(("/.", "/..")):
        return Decision(False, 400, "path is not normalised")
    parts = path[len(ADMIN_PREFIX) :].rstrip("/").split("/")
    if len(parts) < 2 or parts[0] != "banks" or len(parts) > 4:
        return Decision(False, 404, "no such grants API route")
    bank = _bank_segment(parts[1])
    if bank is None:
        return Decision(False, 400, "invalid bank id")
    method = method.upper()
    if len(parts) == 2:
        return GrantsRoute("bank", bank) if method == "PUT" else _not_allowed()
    if parts[2] != "grants":
        return Decision(False, 404, "no such grants API route")
    if len(parts) == 3:
        return GrantsRoute("grants", bank) if method == "GET" else _not_allowed()
    email = unquote(parts[3])
    if not _EMAIL.match(email):
        return Decision(False, 400, "not a single email address")
    if method not in {"PUT", "DELETE"}:
        return _not_allowed()
    return GrantsRoute("grant", bank, email.lower())


def _not_allowed() -> Decision:
    return Decision(False, 405, "method not allowed")


def check_member_access(issuer: str, audience: str) -> None:
    """Refuse a member access configuration the gateway could not enforce."""
    if not issuer.startswith("https://") or len(issuer) <= len("https://"):
        raise PolicyError("member-issuer must be an https URL")
    if not audience:
        raise PolicyError("member-issuer needs member-audience")


def load_policy(
    admin_key: str, keys: Mapping[str, str], access: str, grants_key: str = ""
) -> Policy:
    """Build a policy from the admin key, client keys and the bank-access YAML/JSON.

    ``keys`` maps client names to API keys (the ``access-keys`` secret).
    ``access`` maps client names to ``{"banks": [...], "read-banks": [...]}``.
    It is JSON here; the charm converts the operator's YAML before passing it.
    """
    if not admin_key:
        raise PolicyError("an admin key (tenant-api-key) is required")
    if grants_key and (grants_key == admin_key or grants_key in keys.values()):
        raise PolicyError("the grants key is the admin key or a client's key")
    try:
        mapping = json.loads(access) if access.strip() else {}
    except ValueError as exc:
        raise PolicyError(f"bank-access is not valid: {exc}") from exc
    if not isinstance(mapping, dict):
        raise PolicyError("bank-access must map client names to their banks")
    clients = []
    seen_keys = {admin_key}
    for name, rules in mapping.items():
        if not isinstance(name, str) or not _CLIENT_NAME.match(name):
            raise PolicyError(f"invalid client name {name!r}")
        if not isinstance(rules, dict) or set(rules) - {"banks", "read-banks"}:
            raise PolicyError(f"{name}: only 'banks' and 'read-banks' are allowed")
        banks = rules.get("banks") or []
        read_banks = rules.get("read-banks") or []
        for patterns in (banks, read_banks):
            if not isinstance(patterns, list) or not all(
                isinstance(p, str) and p for p in patterns
            ):
                raise PolicyError(f"{name}: banks must be a list of names or patterns")
        key = keys.get(name)
        if not key:
            raise PolicyError(f"{name}: no key for this client in access-keys")
        if key in seen_keys:
            raise PolicyError(f"{name}: its key is the admin key or another client's")
        seen_keys.add(key)
        clients.append(Client(name, key, tuple(banks), tuple(read_banks)))
    return Policy(admin_key=admin_key, clients=tuple(clients), grants_key=grants_key)
