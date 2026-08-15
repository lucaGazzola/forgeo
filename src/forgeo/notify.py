"""Optional run notifications.

Two independent, never-raising channels:

* Telegram (``telegram_bot_token`` + ``telegram_chat_id``) for blocked runs.
* A vendor-neutral webhook (``notify_webhook_url``) that receives a small
  JSON POST on configurable outcomes — ``blocked`` by default, plus
  ``completed`` and ``failed`` when listed in ``notify_webhook_events``.

Both use only the standard library and never raise: a failing notification
is logged as a warning and the outcome of the Forgeo cycle is left unchanged.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

from forgeo.models import ForgeoConfig

logger = logging.getLogger(__name__)

SEND_MESSAGE_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT = 5.0
REASON_LINES = 8


@dataclass
class BlockedNotice:
    """The payload of one blocked-run notification."""

    task_id: str
    task_title: str
    reason: str


def blocked_notice_text(forgeo_name: str, notice: BlockedNotice) -> str:
    """Compose the message body: forgeo name, task id/title, and the reason."""
    lines = [
        f"\u26d4 {forgeo_name} is blocked",
        f"Task {notice.task_id}: {notice.task_title}",
        "",
        *notice.reason.splitlines()[:REASON_LINES],
    ]
    return "\n".join(lines)


def _send_notification_request(
    request: urllib.request.Request, *, channel: str, target: str
) -> bool:
    """Perform one notification request; returns True when delivered.

    A non-200 response or a network error is logged as a warning and
    reported as ``False`` — a failed notification never raises and never
    changes the outcome of the Forgeo cycle.
    """
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200:
                logger.warning(
                    "%s notification failed: HTTP %s from %s.",
                    channel,
                    response.status,
                    target,
                )
                return False
    except (OSError, ValueError) as exc:
        logger.warning("%s notification failed: %s", channel, exc)
        return False
    return True


def send_blocked_notice(config: ForgeoConfig, notice: BlockedNotice) -> bool:
    """Send one ``sendMessage`` request; returns True when delivered.

    Returns ``False`` without a warning when the feature is not configured
    (no notification is expected). Returns ``False`` and logs a warning when
    Telegram rejects or is unreachable — a notification failure never changes
    the outcome of Forgeo cycle.
    """
    if not config.telegram_bot_token or not config.telegram_chat_id:
        return False
    payload = {
        "chat_id": config.telegram_chat_id,
        "text": blocked_notice_text(config.name, notice),
    }
    url = SEND_MESSAGE_URL.format(token=config.telegram_bot_token)
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
    )
    if not _send_notification_request(request, channel="Telegram", target=url):
        return False
    logger.info("Telegram notification sent for blocked run of task %s.", notice.task_id)
    return True


def send_webhook_notice(
    config: ForgeoConfig, outcome: str, notice: BlockedNotice
) -> bool:
    """POST a JSON payload for one run outcome; returns True when delivered.

    The payload carries the forgeo name, the outcome (``blocked``,
    ``completed`` or ``failed``), the task id and title, and the reason.
    Returns ``False`` without a warning when the feature is not configured
    or the outcome is not enabled in ``notify_webhook_events`` (no
    notification is expected). Returns ``False`` and logs a warning when the
    endpoint rejects or is unreachable — a notification failure never changes
    the outcome of Forgeo cycle.
    """
    if not config.notify_webhook_url:
        return False
    if outcome not in config.notify_webhook_events:
        return False
    payload = {
        "forgeo": config.name,
        "outcome": outcome,
        "task_id": notice.task_id,
        "task_title": notice.task_title,
        "reason": notice.reason,
    }
    request = urllib.request.Request(
        config.notify_webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if not _send_notification_request(
        request, channel="Webhook", target=config.notify_webhook_url
    ):
        return False
    logger.info(
        "Webhook notification sent for %s run of task %s.", outcome, notice.task_id
    )
    return True
