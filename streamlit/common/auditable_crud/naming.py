"""Identifier handling and sandbox object naming.

Every identifier that ends up in dynamically built SQL goes through this module:
names are validated or sanitized, then always emitted double-quoted, so no user
supplied text can break out of an identifier position.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MAX_IDENTIFIER_LENGTH = 255
_UNQUOTED_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_NON_IDENT_CHARS = re.compile(r"[^A-Z0-9_]+")
# Longest suffix appended to a sandbox base name (see SandboxNames).
_LONGEST_SUFFIX = len("_RV_STRM")
_SANDBOX_OBJECTS = frozenset(
    {"clone", "stream", "diff", "reval_clone", "reval_stream", "reval_diff"}
)


class IdentifierError(ValueError):
    """Raised when a configured identifier is not a safe Snowflake identifier."""


def normalize_identifier(value: str) -> str:
    """Return the canonical (upper-case) form of an unquoted Snowflake identifier.

    Configuration values are restricted to plain unquoted identifiers, which
    Snowflake resolves case-insensitively as upper case.
    """
    stripped = value.strip()
    if not _UNQUOTED_IDENTIFIER.match(stripped) or len(stripped) > MAX_IDENTIFIER_LENGTH:
        raise IdentifierError(f"Not a valid unquoted Snowflake identifier: {value!r}")
    return stripped.upper()


def quote_identifier(name: str) -> str:
    """Quote an identifier exactly as given, escaping embedded double quotes."""
    if not name:
        raise IdentifierError("Identifier must not be empty")
    return '"' + name.replace('"', '""') + '"'


def fqn(database: str, schema: str, name: str) -> str:
    """Build a fully qualified, fully quoted three-part name."""
    return ".".join(quote_identifier(part) for part in (database, schema, name))


def information_schema(database: str) -> str:
    """Quoted ``"DB".INFORMATION_SCHEMA`` prefix for metadata queries."""
    return f"{quote_identifier(database)}.INFORMATION_SCHEMA"


def sql_string_literal(value: str) -> str:
    """Render ``value`` as a Snowflake single-quoted string literal.

    Only for DDL positions that do not accept bind variables (e.g. ``COMMENT``);
    everywhere else values are bound. Backslash is Snowflake's escape character
    inside single quotes, so it is doubled along with the quote itself.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def sanitize_name_part(value: str, max_length: int) -> str:
    """Turn arbitrary text (e.g. a user name) into an identifier fragment."""
    cleaned = _NON_IDENT_CHARS.sub("_", value.upper()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned) or "X"
    return cleaned[:max_length].rstrip("_") or "X"


@dataclass(frozen=True)
class SandboxNames:
    """Unquoted object names for one change request's sandbox objects.

    ``base`` identifies the request: source table, timestamp, user and the
    request id, so a clone can be traced back to its owner just from its name.
    """

    base: str

    def for_revision(self, revision: int) -> SandboxNames:
        """Return distinct object names for one immutable run of this request."""
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 0 <= revision < 10**38
        ):
            raise ValueError("Sandbox revision must be a non-negative Snowflake integer.")
        if revision == 0:
            return self
        suffix = f"_R{revision}"
        room = MAX_IDENTIFIER_LENGTH - _LONGEST_SUFFIX - len(suffix)
        base = self.base
        if len(base) > room:
            # Retain the beginning of the table name and the timestamp, actor and
            # request ID at the end. The hash distinguishes legacy names which
            # differ only in the portion of the table name that was shortened.
            prefix = base[:16] + "_" + hashlib.sha256(base.encode()).hexdigest()[:16].upper() + "_"
            base = prefix + base[-(room - len(prefix)) :]
        return SandboxNames(base + suffix)

    def published(self, request: Mapping[str, Any], kind: str) -> str:
        """Resolve a request's published artifact, including pre-revision object names."""
        if kind not in _SANDBOX_OBJECTS:
            raise ValueError(f"Unknown sandbox object kind {kind!r}")
        field = "REVAL_SANDBOX_REVISION" if kind.startswith("reval_") else "DRY_SANDBOX_REVISION"
        revision = int(request.get(field) or 0)
        attribute = kind.removeprefix("reval_") if revision else kind
        return str(getattr(self.for_revision(revision), attribute))

    @property
    def clone(self) -> str:
        """Clone of the source table that the developer's DML runs against."""
        return self.base

    @property
    def stream(self) -> str:
        """Stream on the clone capturing exactly the rows the DML touched."""
        return f"{self.base}_STRM"

    @property
    def diff(self) -> str:
        """Materialized copy of the stream contents (stable, browsable)."""
        return f"{self.base}_DIFF"

    @property
    def reval_clone(self) -> str:
        """Clone used by the admin's re-validation against current data."""
        return f"{self.base}_RV"

    @property
    def reval_stream(self) -> str:
        """Stream on the re-validation clone."""
        return f"{self.base}_RV_STRM"

    @property
    def reval_diff(self) -> str:
        """Materialized diff of the re-validation run."""
        return f"{self.base}_RV_DIFF"


def build_sandbox_names(
    source_table: str, user_name: str, request_id: str, created_at: datetime
) -> SandboxNames:
    """Derive unique, identifiable sandbox names for a change request.

    Format: ``<SOURCE>__CR_<yyyymmddHHMMSS>_<USER>_<request UUID hex>``. The source part is
    truncated first so every derived name stays within Snowflake's 255-char limit.
    """
    stamp = created_at.strftime("%Y%m%d%H%M%S")
    user = sanitize_name_part(user_name, 40)
    request_token = sanitize_name_part(request_id.replace("-", ""), 32)
    tail = f"__CR_{stamp}_{user}_{request_token}"
    room = MAX_IDENTIFIER_LENGTH - _LONGEST_SUFFIX - len(tail)
    source = sanitize_name_part(source_table, room)
    return SandboxNames(base=f"{source}{tail}")
