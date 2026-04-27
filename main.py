"""
AI Email Triage & Summary Bot
=============================

Fetches unread Gmail messages over IMAP, classifies each one with an LLM
(Groq + Llama 3.3), then routes the structured result to Slack (webhook)
and Notion (database). Processed emails are marked as read so the script
is safe to run on a schedule (cron / Task Scheduler / GitHub Actions).

Run:
    python main.py                # process at most BATCH_LIMIT unread emails
    python main.py --dry-run      # process but DON'T mark as read
    python main.py --limit 5      # cap how many emails are touched

All third-party integrations (Slack, Notion) are optional - if the
relevant env vars are missing the script logs the payload and continues.
This keeps local development unblocked when you only have a Groq key.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Literal, Optional

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration & logging
# ---------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("email_triage")

# Email account
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USERNAME = os.getenv("IMAP_USERNAME")  # full Gmail address
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD")  # 16-char app password
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")

# LLM (Groq, free tier)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# Slack
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

# Notion
NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_VERSION = "2022-06-28"

# Behaviour
BATCH_LIMIT = int(os.getenv("BATCH_LIMIT", "25"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))

VALID_CLASSIFICATIONS = ("Lead", "Support", "Spam", "Internal")
VALID_PRIORITIES = ("High", "Medium", "Low")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class EmailRecord:
    """A normalized snapshot of an email after parsing."""

    uid: str
    subject: str
    sender: str
    received_at: Optional[datetime]
    body: str

    @property
    def received_iso(self) -> Optional[str]:
        return self.received_at.isoformat() if self.received_at else None


@dataclass
class TriageResult:
    """Structured output produced by the LLM for a single email."""

    classification: Literal["Lead", "Support", "Spam", "Internal"]
    priority: Literal["High", "Medium", "Low"]
    summary: List[str]
    suggested_action: str


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------
def _decode(value: Optional[str]) -> str:
    """Decode RFC 2047 MIME-encoded header values into clean unicode."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_body(msg: Message) -> str:
    """Return the best-effort plain-text body of an email message.

    Prefers `text/plain` parts; falls back to a stripped version of the
    `text/html` body when no plain text exists. HTML stripping is intentionally
    simple - we only need enough signal for the LLM to classify.
    """
    plain_parts: List[str] = []
    html_parts: List[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain_parts.append(text)
            elif ctype == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    raw = "\n\n".join(plain_parts) if plain_parts else "\n\n".join(html_parts)
    if not plain_parts and html_parts:
        # Strip HTML tags + collapse whitespace so the LLM sees readable text.
        raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.S | re.I)
        raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = re.sub(r"\s+", " ", raw)

    # Hard-cap the body so we don't blow the LLM context window on huge emails.
    return raw.strip()[:6000]


def _parse_message(uid: str, raw_bytes: bytes) -> EmailRecord:
    msg = email.message_from_bytes(raw_bytes)
    received: Optional[datetime] = None
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            received = parsedate_to_datetime(date_hdr)
        except (TypeError, ValueError):
            received = None

    return EmailRecord(
        uid=uid,
        subject=_decode(msg.get("Subject")),
        sender=_decode(msg.get("From")),
        received_at=received,
        body=_extract_body(msg),
    )


# ---------------------------------------------------------------------------
# IMAP client
# ---------------------------------------------------------------------------
class GmailIMAPClient:
    """Thin context-managed wrapper around `imaplib.IMAP4_SSL`."""

    def __init__(self, host: str, port: int, username: str, password: str, folder: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.folder = folder
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    def __enter__(self) -> "GmailIMAPClient":
        logger.info("Connecting to %s:%d as %s", self.host, self.port, self.username)
        self._conn = imaplib.IMAP4_SSL(self.host, self.port)
        self._conn.login(self.username, self.password)
        status, _ = self._conn.select(self.folder)
        if status != "OK":
            raise RuntimeError(f"Could not select folder '{self.folder}'.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            self._conn.logout()
        except Exception:
            pass

    def fetch_unread(self, limit: int) -> List[EmailRecord]:
        """Return up to `limit` unread emails (oldest first)."""
        assert self._conn is not None
        status, data = self._conn.uid("search", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("IMAP UID SEARCH failed.")

        uids = (data[0] or b"").split()
        if not uids:
            return []
        uids = uids[:limit]

        results: List[EmailRecord] = []
        for uid_bytes in uids:
            uid = uid_bytes.decode()
            # BODY.PEEK keeps the message as unread until WE explicitly mark it.
            status, msg_data = self._conn.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                logger.warning("Skipping UID %s: fetch failed.", uid)
                continue
            raw = msg_data[0][1]
            try:
                results.append(_parse_message(uid, raw))
            except Exception:
                logger.exception("Failed to parse UID %s", uid)
        return results

    def mark_seen(self, uid: str) -> None:
        assert self._conn is not None
        status, _ = self._conn.uid("store", uid, "+FLAGS", "(\\Seen)")
        if status != "OK":
            logger.warning("Could not mark UID %s as seen.", uid)


# ---------------------------------------------------------------------------
# LLM (Groq) classifier
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an expert email triage assistant for a busy founder.

For every email you receive you must analyze the subject and body and return
ONLY a single JSON object - no commentary, no markdown fences - matching this
exact schema:

{
  "classification": "Lead" | "Support" | "Spam" | "Internal",
  "priority": "High" | "Medium" | "Low",
  "summary": ["bullet 1", "bullet 2", "bullet 3"],
  "suggested_action": "<one short sentence the human should do next>"
}

Definitions:
- "Lead": a prospective client, sales inquiry, partnership, or new business opportunity.
- "Support": an existing customer asking for help, reporting a bug, or requesting changes.
- "Spam": unsolicited marketing, phishing, or low-signal automated mail.
- "Internal": communication from teammates, contractors, or internal tooling.

Priority rules:
- "High": time-sensitive (within 24h), revenue-impacting, or from a hot lead/angry customer.
- "Medium": meaningful but not urgent.
- "Low": informational, newsletters, or low-effort follow-ups.

Summary rules: 2 to 3 short bullets, each under 18 words, written in plain English.
Suggested action: a single concrete sentence (e.g. "Reply within 4 hours with pricing").

Return strictly valid JSON. Do not wrap it in code fences."""


def _post_with_retries(url: str, **kwargs: Any) -> requests.Response:
    """POST with simple exponential backoff for 429/5xx and network errors."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            response = requests.post(url, timeout=HTTP_TIMEOUT, **kwargs)
            if response.status_code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt
                logger.warning(
                    "HTTP %s from %s (attempt %d/%d) - retrying in %ds",
                    response.status_code, url, attempt, LLM_MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            return response
        except requests.RequestException as exc:
            last_exc = exc
            wait = 2 ** attempt
            logger.warning(
                "Network error talking to %s (attempt %d/%d): %s - retrying in %ds",
                url, attempt, LLM_MAX_RETRIES, exc, wait,
            )
            time.sleep(wait)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Exhausted retries for {url}")


def classify_email(record: EmailRecord) -> TriageResult:
    """Send the email to Groq and parse the JSON response into a TriageResult."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is missing - cannot classify emails.")

    user_prompt = (
        f"Subject: {record.subject}\n"
        f"From: {record.sender}\n"
        f"Received: {record.received_iso or 'unknown'}\n\n"
        f"Body:\n{record.body or '(empty)'}"
    )

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    response = _post_with_retries(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers,
        json=payload,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM did not return valid JSON: {content!r}") from exc

    return _validate_triage(data)


def _validate_triage(data: Dict[str, Any]) -> TriageResult:
    """Strict validation - protects downstream consumers from bad LLM output."""
    classification = str(data.get("classification", "")).strip()
    priority = str(data.get("priority", "")).strip()
    summary_raw = data.get("summary", [])
    suggested_action = str(data.get("suggested_action", "")).strip()

    if classification not in VALID_CLASSIFICATIONS:
        raise ValueError(f"Invalid classification: {classification!r}")
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority: {priority!r}")

    # Normalize summary: accept list, string, or comma-separated string.
    if isinstance(summary_raw, str):
        summary = [s.strip(" -•") for s in summary_raw.splitlines() if s.strip()]
    elif isinstance(summary_raw, list):
        summary = [str(s).strip() for s in summary_raw if str(s).strip()]
    else:
        summary = []
    summary = summary[:3] or ["(no summary)"]

    return TriageResult(
        classification=classification,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        summary=summary,
        suggested_action=suggested_action or "Review manually.",
    )


# ---------------------------------------------------------------------------
# Slack notifier
# ---------------------------------------------------------------------------
PRIORITY_COLOUR = {"High": "#e74c3c", "Medium": "#f39c12", "Low": "#3498db"}
CLASSIFICATION_EMOJI = {
    "Lead": "🚀",
    "Support": "🛟",
    "Spam": "🗑️",
    "Internal": "🏢",
}


def send_to_slack(record: EmailRecord, result: TriageResult) -> bool:
    """Post a formatted alert to Slack. Returns False if no webhook configured."""
    if not SLACK_WEBHOOK_URL:
        logger.info("Slack webhook not configured - skipping Slack notification.")
        return False

    emoji = CLASSIFICATION_EMOJI.get(result.classification, "📧")
    summary_text = "\n".join(f"• {s}" for s in result.summary)

    payload = {
        "attachments": [
            {
                "color": PRIORITY_COLOUR.get(result.priority, "#95a5a6"),
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"{emoji} {result.classification} · {result.priority} priority",
                        },
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*From:*\n{record.sender}"},
                            {"type": "mrkdwn", "text": f"*Subject:*\n{record.subject or '(no subject)'}"},
                        ],
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*Summary*\n{summary_text}"},
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Suggested action*\n_{result.suggested_action}_",
                        },
                    },
                ],
            }
        ]
    }

    try:
        r = _post_with_retries(SLACK_WEBHOOK_URL, json=payload)
        r.raise_for_status()
        logger.info("Slack notification sent for UID %s", record.uid)
        return True
    except Exception:
        logger.exception("Failed to send Slack notification for UID %s", record.uid)
        return False


# ---------------------------------------------------------------------------
# Notion writer
# ---------------------------------------------------------------------------
def add_to_notion(record: EmailRecord, result: TriageResult) -> bool:
    """Append a row to the configured Notion database. Returns False if not configured.

    Expected database properties (create them with these exact names & types):
        Title              (Title)
        From               (Rich text)
        Classification     (Select)
        Priority           (Select)
        Summary            (Rich text)
        Suggested Action   (Rich text)
        Received           (Date)
    """
    if not (NOTION_API_TOKEN and NOTION_DATABASE_ID):
        logger.info("Notion not configured - skipping Notion write.")
        return False

    summary_text = "\n".join(f"• {s}" for s in result.summary)
    properties: Dict[str, Any] = {
        "Title": {
            "title": [{"text": {"content": (record.subject or "(no subject)")[:200]}}]
        },
        "From": {
            "rich_text": [{"text": {"content": record.sender[:1000]}}]
        },
        "Classification": {"select": {"name": result.classification}},
        "Priority": {"select": {"name": result.priority}},
        "Summary": {
            "rich_text": [{"text": {"content": summary_text[:2000]}}]
        },
        "Suggested Action": {
            "rich_text": [{"text": {"content": result.suggested_action[:2000]}}]
        },
    }
    if record.received_iso:
        properties["Received"] = {"date": {"start": record.received_iso}}

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
    }
    headers = {
        "Authorization": f"Bearer {NOTION_API_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    try:
        r = _post_with_retries(
            "https://api.notion.com/v1/pages",
            headers=headers,
            json=payload,
        )
        if r.status_code >= 400:
            logger.error(
                "Notion API error %s for UID %s: %s",
                r.status_code, record.uid, r.text[:500],
            )
            return False
        logger.info("Notion row created for UID %s", record.uid)
        return True
    except Exception:
        logger.exception("Failed to write to Notion for UID %s", record.uid)
        return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def process_emails(limit: int, dry_run: bool) -> Dict[str, int]:
    """Run a single triage pass. Returns a stats dict."""
    if not (IMAP_USERNAME and IMAP_PASSWORD):
        raise RuntimeError(
            "IMAP_USERNAME / IMAP_PASSWORD missing - set them in .env first."
        )

    stats = {"fetched": 0, "classified": 0, "slack_sent": 0, "notion_sent": 0, "marked_read": 0, "errors": 0}

    with GmailIMAPClient(IMAP_HOST, IMAP_PORT, IMAP_USERNAME, IMAP_PASSWORD, IMAP_FOLDER) as inbox:
        messages = inbox.fetch_unread(limit=limit)
        stats["fetched"] = len(messages)
        if not messages:
            logger.info("No unread emails. Nothing to do.")
            return stats

        logger.info("Found %d unread email(s). Triage starting.", len(messages))

        for record in messages:
            logger.info(
                "Triaging UID=%s | From=%s | Subject=%s",
                record.uid, record.sender[:80], (record.subject or "")[:80],
            )
            try:
                result = classify_email(record)
            except Exception:
                logger.exception("Classification failed for UID %s - skipping.", record.uid)
                stats["errors"] += 1
                continue
            stats["classified"] += 1

            logger.info(
                "  -> %s / %s | %s",
                result.classification, result.priority, result.suggested_action,
            )

            if send_to_slack(record, result):
                stats["slack_sent"] += 1
            if add_to_notion(record, result):
                stats["notion_sent"] += 1

            if dry_run:
                logger.info("  [dry-run] Not marking UID %s as read.", record.uid)
            else:
                inbox.mark_seen(record.uid)
                stats["marked_read"] += 1

            # Always emit the structured payload so a console-only run is useful.
            logger.debug("Payload: %s", json.dumps(asdict(result), indent=2))

    return stats


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI Email Triage & Summary Bot")
    p.add_argument(
        "--limit", type=int, default=BATCH_LIMIT,
        help=f"Max number of unread emails to process (default: {BATCH_LIMIT}).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Process emails but DO NOT mark them as read.",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    started = datetime.now(timezone.utc)
    logger.info("=== Email triage started at %s ===", started.isoformat(timespec="seconds"))

    try:
        stats = process_emails(limit=args.limit, dry_run=args.dry_run)
    except imaplib.IMAP4.error as exc:
        logger.error("IMAP error: %s", exc)
        return 2
    except RuntimeError as exc:
        logger.error("Configuration error: %s", exc)
        return 3
    except Exception:
        logger.exception("Unhandled error during triage.")
        return 1

    logger.info("=== Done. Stats: %s ===", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
