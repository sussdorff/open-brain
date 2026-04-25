"""IMAPEmailIngestor — ingests emails from an IMAP server into open-brain memory.

Flow per ingest_for_person call:
1. Load person memory to get email addresses.
2. Fetch the IMAP password via CommandRunner (op CLI DI pattern).
3. Connect to IMAP and search for emails matching person addresses (FROM/TO/CC).
4. For each matching email UID:
   a. Check idempotency: skip if source_ref already exists in DataLayer.
   b. Parse RFC822 bytes to extract subject, date, body.
   c. Extract summary via Haiku LLM (or store raw body if EMAIL_STORE_RAW_BODIES=True).
   d. Save interaction memory with source_ref=imap:{server}:{uid}.
5. Return IngestResult with all created interaction memory IDs.
"""

import email
import email.parser
import email.utils
import html
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import date, timedelta
from email.header import decode_header
from typing import Any, Protocol

from open_brain.config import get_config
from open_brain.data_layer.interface import DataLayer, SaveMemoryParams, SearchParams
from open_brain.data_layer.llm import LlmMessage, llm_complete
from open_brain.ingest.adapters.base import register
from open_brain.ingest.models import IngestResult

try:
    from imapclient import IMAPClient  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    IMAPClient = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

EMAIL_EXTRACTION_PROMPT = """You are an assistant that extracts structured information from emails.
Return ONLY valid JSON with these fields:
{
  "summary": "one or two sentence summary of the email content",
  "topics": ["topic1", "topic2"]
}

Rules:
- summary: concise description of what was communicated, focusing on key points
- topics: main subjects or themes discussed

Return ONLY the JSON object, no other text.
"""


# ─── CommandRunner protocol + implementations ────────────────────────────────


class CommandRunner(Protocol):
    """Protocol for running external commands (DI for testability)."""

    def run(self, op_ref: str) -> str:
        """Run 'op read <op_ref>' and return the secret value (stripped)."""
        ...


class SubprocessCommandRunner:
    """Production CommandRunner that calls subprocess.check_output."""

    def run(self, op_ref: str) -> str:
        """Fetch a secret from 1Password via the op CLI."""
        result = subprocess.check_output(["op", "read", op_ref])
        return result.decode().strip()


def get_default_runner() -> SubprocessCommandRunner:
    """Return the default CommandRunner (subprocess-based op CLI)."""
    return SubprocessCommandRunner()


# ─── MessageRef dataclass ────────────────────────────────────────────────────


@dataclass
class MessageRef:
    """Lightweight reference to an IMAP message."""

    uid: int
    subject: str | None = None
    date: str | None = None
    from_addr: str | None = None


# ─── Email parsing helpers ───────────────────────────────────────────────────


def _decode_header_value(value: str | None) -> str:
    """Decode an RFC2047-encoded email header value to a plain string."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for fragment, charset in parts:
        if isinstance(fragment, bytes):
            charset = charset or "utf-8"
            try:
                decoded.append(fragment.decode(charset, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded.append(fragment.decode("utf-8", errors="replace"))
        else:
            decoded.append(str(fragment))
    return "".join(decoded)


def _strip_html(html_text: str) -> str:
    """Strip HTML tags from a string to produce plain text."""
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_body(msg: email.message.Message) -> str:
    """Extract plain text body from an email message.

    Prefers text/plain parts; falls back to stripping text/html.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    plain_parts.append(payload.decode(charset, errors="replace"))
            elif ct == "text/html":
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    html_parts.append(payload.decode(charset, errors="replace"))
    else:
        ct = msg.get_content_type()
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            body = payload.decode(charset, errors="replace")
            if ct == "text/html":
                html_parts.append(body)
            else:
                plain_parts.append(body)

    if plain_parts:
        return "\n".join(plain_parts)
    if html_parts:
        return _strip_html("\n".join(html_parts))
    return ""


def _parse_rfc822(raw: bytes) -> tuple[str, str, str, str]:
    """Parse RFC822 bytes into (subject, date, from_addr, body)."""
    parser = email.parser.BytesParser()
    msg = parser.parsebytes(raw)

    subject = _decode_header_value(msg.get("Subject"))
    date = msg.get("Date", "")
    from_addr = msg.get("From", "")
    body = _extract_body(msg)

    return subject, date, from_addr, body


def _build_imap_search_criteria(
    email_addresses: list[str],
    since: date | str | None,
) -> list[Any]:
    """Build IMAP SEARCH criteria for emails matching FROM/TO/CC of given addresses.

    Uses OR to combine multiple addresses. Adds SINCE if provided.

    Args:
        email_addresses: List of email addresses to search for.
        since: Optional date (Python date object or IMAP "DD-Mon-YYYY" string) to filter by.

    Returns:
        IMAP search criteria list compatible with imapclient.
    """
    if not email_addresses:
        return []

    def addr_criteria(addr: str) -> list[Any]:
        """Build OR FROM addr (OR TO addr CC addr) for a single address."""
        return ["OR", ["FROM", addr], ["OR", ["TO", addr], ["CC", addr]]]

    if len(email_addresses) == 1:
        criteria: list[Any] = addr_criteria(email_addresses[0])
    else:
        # Chain multiple addresses with OR
        criteria = addr_criteria(email_addresses[0])
        for addr in email_addresses[1:]:
            criteria = ["OR", criteria, addr_criteria(addr)]

    if since:
        # Wrap with SINCE: imapclient treats a list-of-lists as AND
        criteria = [["SINCE", since], criteria]

    return criteria


# ─── IMAPEmailIngestor ───────────────────────────────────────────────────────


class IMAPEmailIngestor:
    """Ingests emails from an IMAP server into open-brain memory.

    Idempotent: re-running with the same inputs produces no new memories.
    Credentials are fetched at runtime via CommandRunner (never logged).

    Implements the IngestAdapter Protocol (ADR-0001).

    Args:
        data_layer: DataLayer implementation for persistence. Optional — pass
            None to create a sentinel instance for adapter discovery.
        server: IMAP server hostname. Defaults to "" (sentinel only).
        port: IMAP server port (default 993 for SSL).
        user: IMAP username (login address).
        password_op_ref: 1Password op:// reference for the IMAP password.
        runner: CommandRunner to fetch the password. Defaults to SubprocessCommandRunner.
        store_raw_bodies: If True, store raw email body instead of LLM summary.
        extraction_model: LLM model for email summarization.
        folder: IMAP folder/mailbox to search (default "INBOX").
    """

    name = "email_imap"

    def __init__(
        self,
        data_layer: DataLayer | None = None,
        server: str = "",
        port: int = 993,
        user: str = "",
        password_op_ref: str = "",
        runner: CommandRunner | None = None,
        store_raw_bodies: bool = False,
        extraction_model: str = "claude-haiku-4-5-20251001",
        folder: str = "INBOX",
    ) -> None:
        self._dl = data_layer
        self._server = server
        self._port = port
        self._user = user
        self._password_op_ref = password_op_ref
        self._runner: CommandRunner = runner or get_default_runner()
        self._store_raw_bodies = store_raw_bodies
        self._extraction_model = extraction_model
        self._folder = folder

    @classmethod
    def from_config(
        cls,
        data_layer: DataLayer,
        runner: CommandRunner | None = None,
        password_op_ref_override: str | None = None,
    ) -> "IMAPEmailIngestor":
        """Construct an IMAPEmailIngestor from application config.

        Reads IMAP_SERVER, IMAP_PORT, IMAP_USER, IMAP_PASSWORD_OP,
        EMAIL_STORE_RAW_BODIES, and EMAIL_EXTRACTION_MODEL from Config.
        This is the canonical wiring point — callers in server.py should
        use this factory rather than constructing with hard-coded defaults.

        Args:
            data_layer: DataLayer implementation for persistence.
            runner: Optional CommandRunner override. Defaults to SubprocessCommandRunner.
            password_op_ref_override: Optional 1Password op:// reference to override
                the config value IMAP_PASSWORD_OP. Useful when the caller provides
                a custom credential reference (e.g. from the MCP tool config_ref param).

        Returns:
            A fully configured IMAPEmailIngestor instance.
        """
        cfg = get_config()
        return cls(
            data_layer=data_layer,
            server=cfg.IMAP_SERVER,
            port=cfg.IMAP_PORT,
            user=cfg.IMAP_USER,
            password_op_ref=password_op_ref_override or cfg.IMAP_PASSWORD_OP,
            runner=runner,
            store_raw_bodies=cfg.EMAIL_STORE_RAW_BODIES,
            extraction_model=cfg.EMAIL_EXTRACTION_MODEL,
        )

    def _fetch_password(self) -> str:
        """Fetch the IMAP password via the CommandRunner. Never logged."""
        password = self._runner.run(self._password_op_ref)
        logger.info("IMAP credentials loaded for user=%r", self._user)
        return password

    async def _list_by_addresses(
        self,
        email_addresses: list[str],
        since: date | str | None = None,
        until: date | str | None = None,
    ) -> list[int]:
        """Return IMAP UIDs for emails matching FROM/TO/CC of given addresses.

        Internal helper used by list_for_person and ingest_for_person.

        Args:
            email_addresses: List of email addresses to search for.
            since: Optional date to restrict results (inclusive lower bound).
            until: Optional date to restrict results (inclusive upper bound).

        Returns:
            Sorted list of matching IMAP UIDs.
        """
        if not email_addresses:
            return []

        password = self._fetch_password()
        criteria = _build_imap_search_criteria(email_addresses, since)
        if until:
            # IMAP BEFORE is exclusive: "BEFORE date" matches messages BEFORE that date.
            # To make `until` inclusive (messages on that date are included), add 1 day.
            if isinstance(until, date):
                before_date: date | str = until + timedelta(days=1)
            else:
                # String form: caller is responsible for correct value; pass through as-is.
                before_date = until
            if isinstance(criteria, list) and criteria:
                criteria = [["BEFORE", before_date], criteria]
            else:
                criteria = [["BEFORE", before_date]]

        with IMAPClient(host=self._server, port=self._port, ssl=True) as client:
            client.login(self._user, password)
            client.select_folder(self._folder)
            uids = client.search(criteria)

        return list(uids)

    async def list_for_person(
        self,
        person_memory_id: int,
        since: date | None = None,
        until: date | None = None,
        max_results: int = 100,
    ) -> list[MessageRef]:
        """Return MessageRefs for emails matching FROM/TO/CC of a known person.

        Looks up the person memory by ID to obtain email addresses, then
        searches IMAP for matching messages.

        Args:
            person_memory_id: ID of the person memory record.
            since: Optional date to restrict results (inclusive lower bound).
            until: Optional date to restrict results (inclusive upper bound).
            max_results: Maximum number of results to return (default 100).

        Returns:
            List of MessageRef objects (up to max_results).
        """
        # Load person memory
        email_addresses = await self._get_email_addresses_for_person(person_memory_id)
        if not email_addresses:
            return []

        uids = await self._list_by_addresses(email_addresses, since=since, until=until)
        uids = uids[:max_results]

        if not uids:
            return []

        password = self._fetch_password()
        refs: list[MessageRef] = []

        with IMAPClient(host=self._server, port=self._port, ssl=True) as client:
            client.login(self._user, password)
            client.select_folder(self._folder)
            fetch_data = client.fetch(uids, [b"ENVELOPE"])

        for uid in uids:
            uid_data = fetch_data.get(uid) or {}
            envelope = uid_data.get(b"ENVELOPE")
            if envelope is not None:
                try:
                    subject = _decode_header_value(envelope.subject.decode() if isinstance(envelope.subject, bytes) else (envelope.subject or ""))
                    date_str = str(envelope.date) if envelope.date else None
                    from_list = envelope.from_
                    from_addr = ""
                    if from_list:
                        f = from_list[0]
                        from_addr = f"{f.mailbox.decode()}@{f.host.decode()}" if isinstance(f.mailbox, bytes) else f"{f.mailbox}@{f.host}"
                    refs.append(MessageRef(uid=uid, subject=subject, date=date_str, from_addr=from_addr))
                except Exception:
                    refs.append(MessageRef(uid=uid))
            else:
                refs.append(MessageRef(uid=uid))

        return refs

    async def _get_email_addresses_for_person(self, person_memory_id: int) -> list[str]:
        """Load person memory by ID and extract all email addresses.

        Args:
            person_memory_id: ID of the person memory record.

        Returns:
            Deduplicated list of email address strings.
        """
        search_result = await self._dl.search(
            SearchParams(type="person", limit=500)
        )
        person_memory = None
        for mem in search_result.results:
            if mem.id == person_memory_id:
                person_memory = mem
                break

        email_addresses: list[str] = []
        if person_memory is not None:
            md = person_memory.metadata or {}
            # Primary source: email_addresses list
            email_addresses = list(md.get("email_addresses") or [])
            # Also check scalar "email" field
            if md.get("email"):
                email_addresses.append(str(md["email"]))
            # Check aliases for email patterns
            for alias in md.get("aliases") or []:
                if isinstance(alias, str) and "@" in alias:
                    email_addresses.append(alias)
            # Deduplicate preserving order
            seen: set[str] = set()
            deduped: list[str] = []
            for addr in email_addresses:
                if addr not in seen:
                    seen.add(addr)
                    deduped.append(addr)
            email_addresses = deduped

        return email_addresses

    async def ingest_for_person(
        self,
        person_memory_id: int,
        since: date | str | None = None,
    ) -> IngestResult:
        """Ingest all emails matching a person's email addresses.

        Reads the person memory to extract email addresses, then searches IMAP,
        and ingests each matching email as an interaction memory.

        Args:
            person_memory_id: ID of the person memory record.
            since: Optional date to restrict results (inclusive lower bound).

        Returns:
            IngestResult with created interaction memory IDs.
        """
        email_addresses = await self._get_email_addresses_for_person(person_memory_id)

        # Find matching UIDs
        uids = await self._list_by_addresses(email_addresses=email_addresses, since=since)

        if not uids:
            return IngestResult(
                meeting_memory_id=0,
                person_memory_ids=[person_memory_id],
                interaction_memory_ids=[],
            )

        return await self.ingest_uids(uids=uids, person_memory_id=person_memory_id)

    async def ingest_uids(
        self,
        uids: list[int],
        person_memory_id: int | None = None,
    ) -> IngestResult:
        """Fetch and ingest a specific list of IMAP UIDs.

        Args:
            uids: List of IMAP UIDs to fetch and ingest.
            person_memory_id: Optional person memory ID to associate interactions with.

        Returns:
            IngestResult with created or found interaction memory IDs.
        """
        if not uids:
            return IngestResult(
                meeting_memory_id=0,
                person_memory_ids=[person_memory_id] if person_memory_id else [],
            )

        password = self._fetch_password()
        interaction_memory_ids: list[int] = []

        with IMAPClient(host=self._server, port=self._port, ssl=True) as client:
            client.login(self._user, password)
            client.select_folder(self._folder)
            fetch_data = client.fetch(uids, [b"RFC822"])

        skipped_count = 0
        for uid in uids:
            uid_data = fetch_data.get(uid) or {}
            raw = uid_data.get(b"RFC822")
            if raw is None:
                logger.warning("No RFC822 data for UID %d — skipping", uid)
                continue

            is_new = await self._ingest_single_email(
                uid=uid,
                raw=raw,
                person_memory_id=person_memory_id,
                interaction_memory_ids=interaction_memory_ids,
            )
            if not is_new:
                skipped_count += 1

        person_ids = [person_memory_id] if person_memory_id is not None else []
        return IngestResult(
            meeting_memory_id=0,
            person_memory_ids=person_ids,
            interaction_memory_ids=interaction_memory_ids,
            skipped_count=skipped_count,
        )

    async def ingest_inbox(self, max_messages: int = 50) -> tuple[int, int]:
        """Ingest the most recent N emails from the configured IMAP folder (INBOX by default).

        Connects to the IMAP server once, searches for all messages, takes the
        last `max_messages` UIDs (sorted ascending → most recent are highest),
        then fetches their RFC822 data in the same connection and ingests each one.
        Using a single connection avoids a second TCP/TLS handshake and a second
        call to _fetch_password() (op CLI).

        Args:
            max_messages: Maximum number of emails to fetch (default 50).
                Pass 0 to skip entirely and return (0, 0).
                Must be >= 0; negative values raise ValueError.

        Returns:
            Tuple of (ingested_count, skipped_count) where:
            - ingested_count: number of newly saved memories
            - skipped_count: number of already-existing memories (idempotency hits)
        """
        if max_messages < 0:
            raise ValueError("max_messages must be >= 0")

        if max_messages == 0:
            return (0, 0)

        if IMAPClient is None:  # pragma: no cover
            raise ImportError(
                "imapclient is required for email ingestion. "
                "Install it with: pip install imapclient"
            )

        password = self._fetch_password()

        # Single IMAP session: search for UIDs then fetch RFC822 data in one connection.
        with IMAPClient(host=self._server, port=self._port, ssl=True) as client:
            client.login(self._user, password)
            client.select_folder(self._folder)
            all_uids = client.search(["ALL"])

            # Take the most recent N UIDs (UIDs are in ascending order; highest = newest)
            uids = sorted(all_uids)[-max_messages:]

            if not uids:
                return (0, 0)

            fetch_data = client.fetch(uids, [b"RFC822"])

        interaction_memory_ids: list[int] = []
        skipped_count = 0

        for uid in uids:
            uid_data = fetch_data.get(uid) or {}
            raw = uid_data.get(b"RFC822")
            if raw is None:
                logger.warning("No RFC822 data for UID %d — skipping", uid)
                continue

            is_new = await self._ingest_single_email(
                uid=uid,
                raw=raw,
                person_memory_id=None,
                interaction_memory_ids=interaction_memory_ids,
            )
            if not is_new:
                skipped_count += 1

        # Both newly ingested and skipped IDs are appended to interaction_memory_ids,
        # so subtracting skipped_count gives the count of newly saved memories.
        ingested = len(interaction_memory_ids) - skipped_count
        return (ingested, skipped_count)

    # ─── ADR-0001 IngestAdapter Protocol methods ─────────────────────────────

    async def list_recent(self, n: int) -> list[MessageRef]:
        """Return the N most recent messages from the configured IMAP folder.

        Connects to IMAP, searches ALL messages, takes the N highest UIDs
        (ascending sort → highest = newest), then fetches ENVELOPE for each.

        Args:
            n: Maximum number of MessageRef items to return.

        Returns:
            List of MessageRef instances (up to n), newest first.

        Raises:
            RuntimeError: If this is a sentinel instance (server not configured).
        """
        if IMAPClient is None:  # pragma: no cover
            raise ImportError("imapclient is required for email ingestion. Install it with: pip install imapclient")
        if self._dl is None:
            raise RuntimeError(
                "Sentinel instance cannot list — provide data_layer and server config"
            )

        password = self._fetch_password()

        with IMAPClient(host=self._server, port=self._port, ssl=True) as client:
            client.login(self._user, password)
            client.select_folder(self._folder)
            all_uids = client.search(["ALL"])
            uids = sorted(all_uids)[-n:]

            if not uids:
                return []

            fetch_data = client.fetch(uids, [b"ENVELOPE"])

        refs: list[MessageRef] = []
        for uid in uids:
            uid_data = fetch_data.get(uid) or {}
            envelope = uid_data.get(b"ENVELOPE")
            if envelope is not None:
                try:
                    subject = _decode_header_value(
                        envelope.subject.decode()
                        if isinstance(envelope.subject, bytes)
                        else (envelope.subject or "")
                    )
                    date_str = str(envelope.date) if envelope.date else None
                    from_list = envelope.from_
                    from_addr = ""
                    if from_list:
                        f = from_list[0]
                        from_addr = (
                            f"{f.mailbox.decode()}@{f.host.decode()}"
                            if isinstance(f.mailbox, bytes)
                            else f"{f.mailbox}@{f.host}"
                        )
                    refs.append(MessageRef(uid=uid, subject=subject, date=date_str, from_addr=from_addr))
                except (UnicodeDecodeError, AttributeError, TypeError) as exc:
                    logger.warning("Failed to decode envelope for UID %d: %s", uid, exc)
                    refs.append(MessageRef(uid=uid))
            else:
                refs.append(MessageRef(uid=uid))

        return refs

    async def ingest(self, ref: Any, run_id: str) -> "IngestResult":
        """ADR-0001 Protocol method: ingest a single message identified by ref.

        Extracts the UID from a MessageRef or coerces ref to int, calls
        ingest_uids(), then sets run_id on the result.

        Args:
            ref: A MessageRef (from list_recent) or a UID integer/string.
            run_id: UUID string created by the orchestrator for this ingest run.
                Embedded in the returned IngestResult.

        Returns:
            IngestResult with all created memory IDs and the supplied run_id.

        Raises:
            RuntimeError: If this is a sentinel instance (data_layer not provided).
        """
        if self._dl is None:
            raise RuntimeError(
                "Sentinel instance cannot ingest — provide data_layer"
            )
        if isinstance(ref, MessageRef):
            uid = ref.uid
        elif isinstance(ref, int):
            uid = ref
        else:
            raise TypeError(f"ingest() requires MessageRef or int, got {type(ref).__name__}")
        result = await self.ingest_uids(uids=[uid])
        result.run_id = run_id
        return result

    async def _ingest_single_email(
        self,
        uid: int,
        raw: bytes,
        person_memory_id: int | None,
        interaction_memory_ids: list[int],
    ) -> bool:
        """Ingest a single email by UID.

        Checks idempotency before saving. Updates interaction_memory_ids in-place.

        Returns:
            True if the email was newly ingested, False if it was skipped (already exists).
        """
        source_ref = f"imap:{self._server}:{uid}"

        # Idempotency check
        existing = await self._dl.search(
            SearchParams(
                type="interaction",
                project="people",
                metadata_filter={"source_ref": source_ref},
            )
        )
        if existing.results:
            logger.debug("Skipping UID %d — already ingested as source_ref=%r", uid, source_ref)
            interaction_memory_ids.append(existing.results[0].id)
            return False

        # Parse email
        subject, date_raw, from_addr, body = _parse_rfc822(raw)

        # Build the text to save
        if self._store_raw_bodies:
            text = body
        else:
            text = await self._extract_summary(
                subject=subject,
                from_addr=from_addr,
                date=date_raw,
                body=body,
            )

        # Parse RFC2822 date to ISO 8601 for metadata
        try:
            dt = email.utils.parsedate_to_datetime(date_raw)
            occurred_at = dt.isoformat()
        except Exception:
            occurred_at = date_raw  # keep raw on parse failure

        # Determine direction by comparing sender against the IMAP user
        direction = "outbound" if self._user.lower() in from_addr.lower() else "inbound"

        # Build metadata
        metadata: dict[str, Any] = {
            "source_ref": source_ref,
            "channel": "email",
            "direction": direction,
            "occurred_at": occurred_at,
        }
        if person_memory_id is not None:
            metadata["person_ref"] = str(person_memory_id)

        save_result = await self._dl.save_memory(
            SaveMemoryParams(
                text=text,
                type="interaction",
                project="people",
                title=f"Email: {subject or '(no subject)'}",
                metadata=metadata,
            )
        )
        interaction_memory_ids.append(save_result.id)
        logger.debug(
            "Ingested email UID=%d source_ref=%r as interaction memory id=%d",
            uid,
            source_ref,
            save_result.id,
        )
        return True

    async def _extract_summary(
        self,
        subject: str,
        from_addr: str,
        date: str,
        body: str,
    ) -> str:
        """Call the LLM to extract a summary from email content.

        Returns the summary string; falls back to a simple description on error.
        """
        prompt = (
            f"{EMAIL_EXTRACTION_PROMPT}\n\n"
            f"Email:\n"
            f"From: {from_addr}\n"
            f"Date: {date}\n"
            f"Subject: {subject}\n\n"
            f"{body[:4000]}"
        )

        try:
            response = await llm_complete(
                messages=[LlmMessage(role="user", content=prompt)],
                model=self._extraction_model,
                max_tokens=512,
            )
            cleaned = response.strip()
            fence_match = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", cleaned, re.DOTALL)
            if fence_match:
                cleaned = fence_match.group(1)
            data = json.loads(cleaned)
            summary = data.get("summary") or ""
            if summary:
                return summary
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse LLM extraction response: %s", exc)
        except Exception as exc:
            logger.warning("LLM extraction failed: %s", exc, exc_info=True)

        return f"Email from {from_addr}: {subject or '(no subject)'}"


# ─── Module-level registration (ADR-0001) ────────────────────────────────────
# Register a sentinel instance for adapter discovery. The sentinel uses
# data_layer=None and server="" (defaults); real ingest/list_recent calls require
# a properly constructed instance with data_layer and server provided.
register(IMAPEmailIngestor())
