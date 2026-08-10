"""Moderation helpers: spam, flood, raid detection, blacklist, and actions."""

import datetime as dt
import logging
from collections import defaultdict
from typing import Optional, Tuple

from sqlalchemy import select
from telegram import ChatPermissions

from .models import BlacklistWord, GroupSettings, get_session

log = logging.getLogger(__name__)

# In-memory counters for flood/repeat/raid detection.
# These are simple; you may want to persist them in Redis or a DB for production.
_flood_counter: dict[int, list[float]] = defaultdict(list)   # user_id -> timestamps
_repeat_cache: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))   # user_id -> {text -> timestamps}
_join_counter: dict[int, list[float]] = defaultdict(list)   # chat_id -> list of join timestamps


def classify(text: str, chat_id: int) -> Optional[Tuple[str, str]]:
    """
    Check message against blacklisted words.
    Returns (reason, severity) if a blacklist word is found, else None.
    """
    text_lower = text.lower()
    with get_session() as session:
        words = session.scalars(
            select(BlacklistWord.word).where(BlacklistWord.chat_id == chat_id)
        ).all()
    for word in words:
        if word in text_lower:
            return (f"blacklisted word: '{word}'", "warn")
    return None


def record_join_and_check_raid(cfg) -> bool:
    """
    Record a join event for the group chat (cfg.group_chat_id) and check if
    the number of joins in the last `cfg.raid_window_seconds` exceeds `cfg.raid_join_count`.
    Returns True if a raid is detected.
    """
    chat_id = cfg.group_chat_id
    now = dt.datetime.utcnow().timestamp()
    _join_counter[chat_id].append(now)

    # Remove old entries
    cutoff = now - cfg.raid_window_seconds
    _join_counter[chat_id] = [t for t in _join_counter[chat_id] if t >= cutoff]

    return len(_join_counter[chat_id]) >= cfg.raid_join_count


def record_and_check_flood(uid: int) -> bool:
    """
    Simple flood detection: more than 5 messages in 5 seconds.
    Returns True if flooding is detected.
    """
    now = dt.datetime.utcnow().timestamp()
    _flood_counter[uid].append(now)

    # Keep only last 10 seconds
    cutoff = now - 5
    _flood_counter[uid] = [t for t in _flood_counter[uid] if t >= cutoff]

    return len(_flood_counter[uid]) > 5   # threshold


def record_and_check_repeat(uid: int, text: str) -> bool:
    """
    Detect repeated identical messages within a short window.
    Returns True if the same text appears more than 2 times in 10 seconds.
    """
    now = dt.datetime.utcnow().timestamp()
    cache = _repeat_cache[uid][text]
    cache.append(now)

    cutoff = now - 10
    _repeat_cache[uid][text] = [t for t in cache if t >= cutoff]

    return len(_repeat_cache[uid][text]) > 2


async def apply_action(context, cfg, chat_id: int, user_id: int, username: str, reason: str, severity: str):
    """
    Apply moderation action based on severity: 'warn', 'mute', or 'kick'.
    Also notifies admins.
    """
    # Log to console
    log.info(f"Moderation action on @{username} ({user_id}): {reason} ({severity})")

    # Notify the user
    try:
        await context.bot.send_message(
            user_id,
            f"⚠️ You were {severity} in the group for: {reason}\n\nPlease review the rules.",
        )
    except Exception:
        pass

    # Notify admins
    for admin_id in cfg.admin_ids:
        try:
            await context.bot.send_message(
                admin_id,
                f"🛡️ *Moderation Action*\n"
                f"User: @{username} (`{user_id}`)\n"
                f"Reason: {reason}\n"
                f"Severity: {severity}",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    # Perform group action (if bot has permission)
    try:
        if severity == "mute":
            # Telegram doesn't have a direct mute; we can restrict permissions for 1 hour.
            await context.bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=dt.datetime.utcnow() + dt.timedelta(hours=1),
            )
        elif severity == "kick":
            await context.bot.ban_chat_member(chat_id, user_id)
            # Optionally unban after a few seconds to allow rejoin (if you want to allow it)
            # await context.bot.unban_chat_member(chat_id, user_id)
    except Exception as e:
        log.warning(f"Failed to apply {severity} to {user_id}: {e}")
