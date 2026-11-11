"""
Discord Crypto Exchange Calculator Bot
======================================
Slash-command bot for INR ↔ Crypto/USD calculations.

All user-facing messages use Components V2 (LayoutView + Container).
No classic embeds are sent.

Commands
--------
/seti2c         Administrator only — set the I2C rate (INR per $1)
/setc2i         Administrator only — set the C2I rate (INR per $1)
/sendcalc       Administrator only — post the public calculator panel
/send_ticket    Administrator only — post the ticket panel in a channel
/tixstaff       Administrator only — set the ticket staff role
/tixcatogery    Administrator only — set the ticket category
/set_addy       Administrator only — save an LTC address
/list_addy      Administrator only — list saved LTC addresses
/set_upi        Administrator only — save a UPI ID
/list_upi       Administrator only — list saved UPI IDs
/send_addy      Administrator only — post a saved LTC address by name
/send_upi       Administrator only — post a saved UPI ID by name
/set prefix     Administrator only — change the text command prefix
/process        Administrator only — start the exchange process in a ticket
/gstart         Administrator only — start a giveaway
.i2c [amount]   Convert INR you have into crypto
.c2i [amount]   Convert crypto you have into INR

Rates are persisted in rates.json so they survive restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# Bootstrap / config.json
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
RATES_FILE = BASE_DIR / "rates.json"
CONFIG_FILE = BASE_DIR / "config.json"
SETTINGS_FILE = BASE_DIR / "settings.json"
WALLETS_FILE = BASE_DIR / "wallets.json"
UPI_FILE = BASE_DIR / "upi.json"
PROCESS_FILE = BASE_DIR / "process_sessions.json"
GIVEAWAYS_FILE = BASE_DIR / "giveaways.json"
EMOJIS_FILE = BASE_DIR / "emojis.json"


def _parse_snowflake(value: Any) -> Optional[int]:
    """Accept a Discord ID as int, numeric string, or empty/invalid → None."""
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if text.isdigit():
        parsed = int(text)
        return parsed if parsed > 0 else None
    return None


def load_config() -> tuple[str, int]:
    """Read token and guild_id from config.json. Both keys are required."""
    if not CONFIG_FILE.exists():
        raise SystemExit(
            "config.json was not found. Copy config.example.json to config.json "
            "and add your bot token and guild_id."
        )

    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config.json is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise SystemExit("config.json must contain a JSON object.")

    token = str(raw.get("token") or raw.get("DISCORD_TOKEN") or "").strip()
    guild_id = _parse_snowflake(raw.get("guild_id", raw.get("GUILD_ID")))

    if guild_id is None:
        raise SystemExit(
            "config.json is missing a valid \"guild_id\". "
            "Add your Discord server ID so slash commands can sync to that guild."
        )

    return token, guild_id


DISCORD_TOKEN, GUILD_ID = load_config()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("exchange-bot")

ACCENT_GOLD = 0xF0B90B
ACCENT_OK = 0x2ECC71
ACCENT_ERR = 0xE74C3C

SELECT_CUSTOM_ID = "exchange_calc_select"
TICKET_OPEN_CUSTOM_ID = "ticket_open_exchange"
TICKET_CLOSE_CUSTOM_ID = "ticket_close"
TICKET_CLAIM_CUSTOM_ID = "ticket_claim"
PROCESS_TOS_YES_ID = "process_tos_yes"
PROCESS_TOS_NO_ID = "process_tos_no"
PROCESS_PAID_ID = "process_paid"
PROCESS_COPY_ID = "process_copy"
PROCESS_DETAILS_ID = "process_details"
ADDY_SELECT_CUSTOM_ID = "ltc_addy_select"

DEFAULT_EMOJIS = {
    "thunder": "<a:thunder:1548719862094889120>",
    "exchange": "<a:exchange:1548720613986803853>",
    "inr": "<:inr:1548719937122599013>",
    "crypto": "<:crypto:1548720014008393768>",
    "blue_dot": "<:Blue_Dot:1548724213035044986>",
    "warning": "<:warningg:1548724614576734230>",
    "cross": "<:cross:1548727337204326501>",
    "tick": "<:tick:1548732489202143252>",
    "wallet": "<:wallet:1548732796493631563>",
    "details": "<:details:1548733606124585065>",
    "dot": "<:Dot:1548733869836992512>",
    "arrow_addy": "<a:arrow_arrow:1548733133015490671>",
    "arrow_name": "<a:arrow_arrow:1523550928261873684>",
    "note": "<a:note:1548976235856797748>",
    "sa_tick": "<a:SA_Tick:1547159231202070568>",
    "gift": "<a:st_gift:1549136100512112780>",
    "falcon_react": "<a:falcon_react:1549137231124496484>",
    "dev": "<a:dev:1549143395161612368>",
    "slash": "<:slash:1549143505379795096>",
    "star": "<:star_star:1549143533724631182>",
    "calculator": "🧮",
    "ok": "✅",
    "err": "❌",
    "currency": "💱",
}

_CUSTOM_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]+):(\d+)>")


def parse_emoji_markup(value: str) -> Optional[discord.PartialEmoji]:
    raw = (value or "").strip()
    if not raw:
        return None
    match = _CUSTOM_EMOJI_RE.fullmatch(raw)
    if match:
        return discord.PartialEmoji(
            animated=bool(match.group(1)),
            name=match.group(2),
            id=int(match.group(3)),
        )
    return discord.PartialEmoji(name=raw)


def load_emojis() -> dict[str, str]:
    merged = dict(DEFAULT_EMOJIS)
    if EMOJIS_FILE.exists():
        try:
            raw = json.loads(EMOJIS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if isinstance(value, str) and value.strip():
                        merged[str(key)] = value.strip()
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read emojis.json (%s); using defaults.", exc)
    EMOJIS_FILE.write_text(json.dumps(merged, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    return merged


def e(key: str) -> str:
    return EMOJI_MAP.get(key) or DEFAULT_EMOJIS.get(key) or ""


def pe(key: str) -> Optional[discord.PartialEmoji]:
    return parse_emoji_markup(e(key))


def emoji_id(key: str) -> Optional[int]:
    parsed = pe(key)
    return parsed.id if parsed is not None else None


EMOJI_MAP = load_emojis()

EMOJI_THUNDER = e("thunder")
EMOJI_EXCHANGE = e("exchange")
EMOJI_INR = e("inr")
EMOJI_CRYPTO = e("crypto")
EMOJI_DOT = e("blue_dot")
EMOJI_WARNING = e("warning")
EMOJI_CROSS = e("cross")
EMOJI_WALLET = e("wallet")
EMOJI_DETAILS = e("details")
EMOJI_DOT_ALT = e("dot")
EMOJI_ARROW_ADDY = e("arrow_addy")
EMOJI_ARROW_NAME = e("arrow_name")
EMOJI_NOTE = e("note")
EMOJI_SA_TICK = e("sa_tick")
EMOJI_DOT_PLAIN = e("dot")
EMOJI_GIFT = e("gift")
EMOJI_FALCON = e("falcon_react")

PARTIAL_INR = pe("inr")
PARTIAL_CRYPTO = pe("crypto")
PARTIAL_EXCHANGE = pe("exchange")
PARTIAL_CROSS = pe("cross")
PARTIAL_TICK = pe("tick")
PARTIAL_FALCON = pe("falcon_react")
PARTIAL_DEV = pe("dev")
PARTIAL_SLASH = pe("slash")
PARTIAL_STAR = pe("star")
PARTIAL_CALCULATOR = pe("calculator")
FALCON_EMOJI_ID = emoji_id("falcon_react")


# ---------------------------------------------------------------------------
# Rate persistence
# ---------------------------------------------------------------------------

_rates_lock = asyncio.Lock()


def _default_rates() -> dict[str, Any]:
    return {"i2c": None, "c2i": None}


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return dec


def load_rates_sync() -> dict[str, Optional[Decimal]]:
    if not RATES_FILE.exists():
        save_rates_sync(_default_rates())
        return {"i2c": None, "c2i": None}

    try:
        raw = json.loads(RATES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read rates.json (%s); starting unconfigured.", exc)
        return {"i2c": None, "c2i": None}

    return {
        "i2c": _decimal_or_none(raw.get("i2c")),
        "c2i": _decimal_or_none(raw.get("c2i")),
    }


def save_rates_sync(rates: dict[str, Any]) -> None:
    payload = {
        "i2c": _json_number(rates.get("i2c")),
        "c2i": _json_number(rates.get("c2i")),
    }
    tmp = RATES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    tmp.replace(RATES_FILE)


def _json_number(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        as_int = value.to_integral_value()
        if as_int == value:
            return int(as_int)
        return float(value)
    return value


async def get_rate(kind: str) -> Optional[Decimal]:
    async with _rates_lock:
        rates = load_rates_sync()
        return rates.get(kind)


async def set_rate(kind: str, value: Decimal) -> None:
    async with _rates_lock:
        rates = load_rates_sync()
        rates[kind] = value
        save_rates_sync(rates)


# ---------------------------------------------------------------------------
# Ticket settings persistence
# ---------------------------------------------------------------------------

_settings_lock = asyncio.Lock()


def _default_settings() -> dict[str, Any]:
    return {
        "staff_role_id": None,
        "ticket_category_id": None,
        "ticket_counter": 0,
        "prefix": ".",
    }


def load_settings_sync() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        data = _default_settings()
        save_settings_sync(data)
        return data
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read settings.json (%s); using defaults.", exc)
        return _default_settings()
    data = _default_settings()
    data["staff_role_id"] = _parse_snowflake(raw.get("staff_role_id"))
    data["ticket_category_id"] = _parse_snowflake(raw.get("ticket_category_id"))
    try:
        data["ticket_counter"] = max(0, int(raw.get("ticket_counter") or 0))
    except (TypeError, ValueError):
        data["ticket_counter"] = 0
    prefix = str(raw.get("prefix") or ".").strip()
    data["prefix"] = prefix if prefix else "."
    return data


def save_settings_sync(data: dict[str, Any]) -> None:
    payload = {
        "staff_role_id": data.get("staff_role_id"),
        "ticket_category_id": data.get("ticket_category_id"),
        "ticket_counter": int(data.get("ticket_counter") or 0),
        "prefix": str(data.get("prefix") or "."),
    }
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


async def get_settings() -> dict[str, Any]:
    async with _settings_lock:
        return load_settings_sync()


async def update_settings(**kwargs: Any) -> dict[str, Any]:
    async with _settings_lock:
        data = load_settings_sync()
        data.update(kwargs)
        save_settings_sync(data)
        return data


async def next_ticket_number() -> int:
    async with _settings_lock:
        data = load_settings_sync()
        data["ticket_counter"] = int(data.get("ticket_counter") or 0) + 1
        save_settings_sync(data)
        return int(data["ticket_counter"])


def ticket_channel_name(username: str, number: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", username or "")[:20] or "user"
    return f"Ticket-{cleaned}-{number}"


def safe_name_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "")[:20]
    return cleaned or "user"


# ---------------------------------------------------------------------------
# LTC wallets persistence
# ---------------------------------------------------------------------------

_wallets_lock = asyncio.Lock()


def _default_wallets() -> dict[str, Any]:
    return {"wallets": []}


def load_wallets_sync() -> list[dict[str, str]]:
    if not WALLETS_FILE.exists():
        save_wallets_sync([])
        return []
    try:
        raw = json.loads(WALLETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read wallets.json (%s); starting empty.", exc)
        return []
    items = raw.get("wallets") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        address = str(item.get("address") or item.get("ltc_address") or "").strip()
        if name and address:
            cleaned.append({"name": name, "address": address})
    return cleaned


def save_wallets_sync(wallets: list[dict[str, str]]) -> None:
    payload = {"wallets": wallets}
    tmp = WALLETS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    tmp.replace(WALLETS_FILE)


async def get_wallets() -> list[dict[str, str]]:
    async with _wallets_lock:
        return load_wallets_sync()


async def save_wallet(name: str, address: str) -> None:
    async with _wallets_lock:
        wallets = load_wallets_sync()
        updated = False
        for item in wallets:
            if item["name"].lower() == name.lower():
                item["name"] = name
                item["address"] = address
                updated = True
                break
        if not updated:
            wallets.append({"name": name, "address": address})
        save_wallets_sync(wallets)


# ---------------------------------------------------------------------------
# UPI persistence
# ---------------------------------------------------------------------------

_upi_lock = asyncio.Lock()


def load_upi_sync() -> list[dict[str, str]]:
    if not UPI_FILE.exists():
        save_upi_sync([])
        return []
    try:
        raw = json.loads(UPI_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read upi.json (%s); starting empty.", exc)
        return []
    items = raw.get("upi") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        upi_id = str(item.get("upi_id") or item.get("address") or "").strip()
        if name and upi_id:
            cleaned.append({"name": name, "upi_id": upi_id})
    return cleaned


def save_upi_sync(entries: list[dict[str, str]]) -> None:
    payload = {"upi": entries}
    tmp = UPI_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    tmp.replace(UPI_FILE)


async def get_upi_entries() -> list[dict[str, str]]:
    async with _upi_lock:
        return load_upi_sync()


async def save_upi(name: str, upi_id: str) -> None:
    async with _upi_lock:
        entries = load_upi_sync()
        updated = False
        for item in entries:
            if item["name"].lower() == name.lower():
                item["name"] = name
                item["upi_id"] = upi_id
                updated = True
                break
        if not updated:
            entries.append({"name": name, "upi_id": upi_id})
        save_upi_sync(entries)


# ---------------------------------------------------------------------------
# Exchange process sessions (per ticket channel)
# ---------------------------------------------------------------------------

_process_lock = asyncio.Lock()


def load_process_sync() -> dict[str, Any]:
    if not PROCESS_FILE.exists():
        save_process_sync({})
        return {}
    try:
        raw = json.loads(PROCESS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read process_sessions.json (%s).", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    sessions = raw.get("sessions", raw)
    return sessions if isinstance(sessions, dict) else {}


def save_process_sync(sessions: dict[str, Any]) -> None:
    tmp = PROCESS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"sessions": sessions}, indent=4) + "\n", encoding="utf-8")
    tmp.replace(PROCESS_FILE)


async def get_process_session(channel_id: int) -> Optional[dict[str, Any]]:
    async with _process_lock:
        sessions = load_process_sync()
        data = sessions.get(str(channel_id))
        return dict(data) if isinstance(data, dict) else None


async def upsert_process_session(channel_id: int, **kwargs: Any) -> dict[str, Any]:
    async with _process_lock:
        sessions = load_process_sync()
        current = sessions.get(str(channel_id))
        if not isinstance(current, dict):
            current = {}
        current.update(kwargs)
        sessions[str(channel_id)] = current
        save_process_sync(sessions)
        return dict(current)


async def delete_process_session(channel_id: int) -> None:
    async with _process_lock:
        sessions = load_process_sync()
        sessions.pop(str(channel_id), None)
        save_process_sync(sessions)


def is_ticket_channel(channel: object) -> bool:
    if not isinstance(channel, discord.TextChannel):
        return False
    name = (channel.name or "").lower()
    topic = channel.topic or ""
    return name.startswith("ticket-") or name.startswith("claimed-") or "owner:" in topic


def owner_id_from_topic(topic: Optional[str]) -> Optional[int]:
    if not topic or not topic.startswith("owner:"):
        return None
    try:
        return int(topic.split("|", 1)[0].replace("owner:", "").strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Giveaways
# ---------------------------------------------------------------------------

_giveaway_lock = asyncio.Lock()


def load_giveaways_sync() -> list[dict[str, Any]]:
    if not GIVEAWAYS_FILE.exists():
        save_giveaways_sync([])
        return []
    try:
        raw = json.loads(GIVEAWAYS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read giveaways.json (%s).", exc)
        return []
    items = raw.get("giveaways") if isinstance(raw, dict) else raw
    return items if isinstance(items, list) else []


def save_giveaways_sync(giveaways: list[dict[str, Any]]) -> None:
    tmp = GIVEAWAYS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"giveaways": giveaways}, indent=4) + "\n", encoding="utf-8")
    tmp.replace(GIVEAWAYS_FILE)


async def add_giveaway(record: dict[str, Any]) -> None:
    async with _giveaway_lock:
        giveaways = load_giveaways_sync()
        giveaways.append(record)
        save_giveaways_sync(giveaways)


async def update_giveaway(message_id: int, **kwargs: Any) -> Optional[dict[str, Any]]:
    async with _giveaway_lock:
        giveaways = load_giveaways_sync()
        found = None
        for item in giveaways:
            if int(item.get("message_id") or 0) == message_id:
                item.update(kwargs)
                found = dict(item)
                break
        save_giveaways_sync(giveaways)
        return found


async def add_giveaway_entry(message_id: int, user_id: int) -> None:
    async with _giveaway_lock:
        giveaways = load_giveaways_sync()
        for item in giveaways:
            if int(item.get("message_id") or 0) != message_id:
                continue
            if item.get("ended"):
                return
            entries = [int(x) for x in item.get("entries") or []]
            if user_id not in entries:
                entries.append(user_id)
                item["entries"] = entries
            break
        save_giveaways_sync(giveaways)


async def remove_giveaway_entry(message_id: int, user_id: int) -> None:
    async with _giveaway_lock:
        giveaways = load_giveaways_sync()
        for item in giveaways:
            if int(item.get("message_id") or 0) != message_id:
                continue
            entries = [int(x) for x in item.get("entries") or [] if int(x) != user_id]
            item["entries"] = entries
            break
        save_giveaways_sync(giveaways)


def giveaway_panel_text(item: str, winners: int, end_ts: int, host: discord.abc.User) -> str:
    return (
        f"{EMOJI_GIFT} **{item}** {EMOJI_GIFT}\n\n"
        f"{EMOJI_DOT} Winners: {winners}\n"
        f"{EMOJI_DOT} Ends: <t:{end_ts}:R>\n"
        f"{EMOJI_DOT} Hosted by: {host.mention}\n\n"
        f"{EMOJI_DOT} React {EMOJI_FALCON} with to participate!"
    )


class GiveawayPanel(discord.ui.LayoutView):
    def __init__(self, text: str) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                accent_color=ACCENT_GOLD,
            )
        )


async def finish_giveaway(bot: commands.Bot, record: dict[str, Any]) -> None:
    message_id = int(record.get("message_id") or 0)
    async with _giveaway_lock:
        giveaways = load_giveaways_sync()
        data = None
        for item in giveaways:
            if int(item.get("message_id") or 0) == message_id:
                if item.get("ended"):
                    return
                item["ended"] = True
                data = dict(item)
                break
        save_giveaways_sync(giveaways)
    if data is None:
        return

    channel = bot.get_channel(int(data.get("channel_id") or 0))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(data.get("channel_id") or 0))
        except discord.HTTPException:
            return
    if not isinstance(channel, discord.TextChannel):
        return

    entries = [int(x) for x in data.get("entries") or []]
    winner_count = max(1, int(data.get("winners") or 1))
    item = str(data.get("item") or "prize")
    host_id = data.get("host_id")
    host_mention = f"<@{host_id}>" if host_id else "the host"

    if not entries:
        text = f"No valid entries. The giveaway for **{item}** ended with no winners."
        await channel.send(view=NoticeView(text, color=ACCENT_ERR, timeout=None))
        return

    pick = min(winner_count, len(entries))
    winners = random.sample(entries, pick)
    mentions = ", ".join(f"<@{uid}>" for uid in winners)
    text = (
        f"Congrats, {mentions} you have won **{item}**, hosted by {host_mention}"
    )
    await channel.send(
        view=NoticeView(text, color=ACCENT_OK, timeout=None),
        allowed_mentions=discord.AllowedMentions(users=True),
    )


async def giveaway_watcher(bot: commands.Bot) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = int(time.time())
            async with _giveaway_lock:
                giveaways = load_giveaways_sync()
            due = [
                item
                for item in giveaways
                if not item.get("ended") and int(item.get("end_ts") or 0) <= now
            ]
            for item in due:
                try:
                    await finish_giveaway(bot, item)
                except Exception:
                    log.exception("Failed to end giveaway %s", item.get("message_id"))
        except Exception:
            log.exception("Giveaway watcher error")
        await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# Formatting & validation
# ---------------------------------------------------------------------------

def format_inr(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if quantized == quantized.to_integral_value():
        return f"₹{quantized.to_integral_value()}"
    return f"₹{quantized}"


def format_amount(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_crypto(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    text = format(quantized.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_rate_number(rate: Decimal) -> str:
    return format_amount(rate)


def parse_positive_decimal(raw: str) -> Decimal:
    cleaned = (raw or "").strip().replace(",", "")
    if not cleaned:
        raise ValueError("empty")
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid") from exc
    if not value.is_finite():
        raise ValueError("invalid")
    if value <= 0:
        raise ValueError("non_positive")
    return value


_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_duration(raw: str) -> tuple[int, str]:
    """
    Parse 10s / 5m / 1h / 1d / 2w into (seconds, original_label).
    Raises ValueError on bad input.
    """
    cleaned = (raw or "").strip().lower()
    match = _DURATION_RE.fullmatch(cleaned)
    if not match:
        raise ValueError("invalid")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = amount * _DURATION_UNITS[unit]
    if amount <= 0 or seconds <= 0:
        raise ValueError("invalid")
    return seconds, cleaned


def guild_display_name(interaction: discord.Interaction) -> str:
    if interaction.guild and interaction.guild.name:
        return interaction.guild.name
    return "Server"


def panel_text(server_name: str, i2c: Optional[Decimal], c2i: Optional[Decimal]) -> str:
    i2c_display = format_rate_number(i2c) if i2c is not None else "Not set"
    c2i_display = format_rate_number(c2i) if c2i is not None else "Not set"
    return (
        f"## {EMOJI_THUNDER} **{server_name} EXCHANGES**\n\n"
        f"Below are the official rates for all **{server_name}** exchange services.\n\n"
        f"**{EMOJI_EXCHANGE} EXCHANGES**\n\n"
        f"> {EMOJI_INR} *INR to Crypto:* `{i2c_display} INR = $1`\n"
        f"> {EMOJI_CRYPTO} *Crypto To INR:*\n"
        f"`{c2i_display} INR = $1`"
    )


# ---------------------------------------------------------------------------
# Components V2 helpers (no classic embeds)
# ---------------------------------------------------------------------------

class NoticeView(discord.ui.LayoutView):
    """One-off Components V2 message: container + markdown text."""

    def __init__(self, text: str, color: int = ACCENT_GOLD, timeout: Optional[float] = 180) -> None:
        super().__init__(timeout=timeout)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                accent_color=color,
            )
        )


async def send_v2(
    interaction: discord.Interaction,
    text: str,
    *,
    color: int = ACCENT_GOLD,
    ephemeral: bool = False,
    view: Optional[discord.ui.LayoutView] = None,
) -> None:
    layout = view if view is not None else NoticeView(text, color=color)
    if interaction.response.is_done():
        await interaction.followup.send(view=layout, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(view=layout, ephemeral=ephemeral)


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class I2CAmountModal(discord.ui.Modal, title="I2C Calculator"):
    """INR you have → crypto you will get.  crypto = inr / i2c_rate"""

    amount = discord.ui.TextInput(
        label="Enter INR Amount",
        placeholder="e.g. 103 or 206.50",
        required=True,
        min_length=1,
        max_length=24,
        style=discord.TextStyle.short,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            inr_amount = parse_positive_decimal(str(self.amount.value))
        except ValueError:
            await send_v2(
                interaction,
                "## ❌ Invalid Amount\n\nPlease enter a valid amount greater than 0.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        rate = await get_rate("i2c")
        if rate is None:
            await send_v2(
                interaction,
                "## ❌ Rate Not Configured\n\n"
                "The I2C rate has not been configured yet.\n"
                "Please ask an administrator to set it first.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        crypto = (inr_amount / rate).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        text = (
            "## 💱 I2C Calculation\n\n"
            f"**I2C Rate**\n`{format_rate_number(rate)} INR = $1`\n\n"
            f"**INR You Have**\n{format_inr(inr_amount)}\n\n"
            f"**Crypto You Will Get**\n`${format_crypto(crypto)}`"
        )
        await send_v2(interaction, text, color=ACCENT_GOLD, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("I2C modal error: %s", error)
        await send_v2(
            interaction,
            "## ❌ Something Went Wrong\n\nPlease try again.",
            color=ACCENT_ERR,
            ephemeral=True,
        )


class C2IAmountModal(discord.ui.Modal, title="C2I Calculator"):
    """Crypto you have → INR you will get.  inr = crypto × c2i_rate"""

    amount = discord.ui.TextInput(
        label="Enter Crypto Amount",
        placeholder="e.g. 1 or 1.25 or 0.5",
        required=True,
        min_length=1,
        max_length=24,
        style=discord.TextStyle.short,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            crypto_amount = parse_positive_decimal(str(self.amount.value))
        except ValueError:
            await send_v2(
                interaction,
                "## ❌ Invalid Amount\n\nPlease enter a valid amount greater than 0.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        rate = await get_rate("c2i")
        if rate is None:
            await send_v2(
                interaction,
                "## ❌ Rate Not Configured\n\n"
                "The C2I rate has not been configured yet.\n"
                "Please ask an administrator to set it first.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        inr = (crypto_amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        text = (
            "## 💱 C2I Calculation\n\n"
            f"**C2I Rate**\n`{format_rate_number(rate)} INR = $1`\n\n"
            f"**Crypto You Have**\n`${format_crypto(crypto_amount)}`\n\n"
            f"**INR You Will Get**\n{format_inr(inr)}"
        )
        await send_v2(interaction, text, color=ACCENT_GOLD, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("C2I modal error: %s", error)
        await send_v2(
            interaction,
            "## ❌ Something Went Wrong\n\nPlease try again.",
            color=ACCENT_ERR,
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Components V2 interactive layouts
# ---------------------------------------------------------------------------

class CalculateI2CButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Calculate",
            emoji=PARTIAL_CALCULATOR,
            style=discord.ButtonStyle.primary,
            custom_id="ephemeral_i2c_calculate",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(I2CAmountModal())


class CalculateC2IButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Calculate",
            emoji=PARTIAL_CALCULATOR,
            style=discord.ButtonStyle.primary,
            custom_id="ephemeral_c2i_calculate",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(C2IAmountModal())


class CalculatorStepView(discord.ui.LayoutView):
    """Ephemeral step after the user picks I2C or C2I."""

    def __init__(self, text: str, calc_type: str) -> None:
        super().__init__(timeout=300)
        row = discord.ui.ActionRow()
        if calc_type == "i2c":
            row.add_item(CalculateI2CButton())
        else:
            row.add_item(CalculateC2IButton())
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=ACCENT_GOLD,
            )
        )


class ExchangeTypeSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            custom_id=SELECT_CUSTOM_ID,
            placeholder="Select the exchange type…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="I2C Rates",
                    description="INR you have → Crypto you get",
                    emoji=PARTIAL_INR,
                    value="i2c",
                ),
                discord.SelectOption(
                    label="C2I Rates",
                    description="Crypto you have → INR you get",
                    emoji=PARTIAL_CRYPTO,
                    value="c2i",
                ),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        choice = self.values[0]
        if choice == "i2c":
            rate = await get_rate("i2c")
            rate_line = (
                f"`{format_rate_number(rate)} INR = $1`"
                if rate is not None
                else "Not configured yet"
            )
            text = (
                "## 💱 I2C Calculator\n\n"
                "Enter the **INR amount you have**. The bot will tell you how much crypto you will get.\n\n"
                f"**Current I2C Rate**\n{rate_line}"
            )
            await interaction.response.send_message(
                view=CalculatorStepView(text, "i2c"),
                ephemeral=True,
            )
        else:
            rate = await get_rate("c2i")
            rate_line = (
                f"`{format_rate_number(rate)} INR = $1`"
                if rate is not None
                else "Not configured yet"
            )
            text = (
                "## 💱 C2I Calculator\n\n"
                "Enter the **crypto amount you have**. The bot will tell you how much INR you will get.\n\n"
                f"**Current C2I Rate**\n{rate_line}"
            )
            await interaction.response.send_message(
                view=CalculatorStepView(text, "c2i"),
                ephemeral=True,
            )


class CalculatorPanel(discord.ui.LayoutView):
    """Public calculator panel sent by /sendcalc."""

    def __init__(self, server_name: str, i2c: Optional[Decimal], c2i: Optional[Decimal]) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(ExchangeTypeSelect())
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(panel_text(server_name, i2c, c2i)),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=ACCENT_GOLD,
            )
        )


class PersistentCalculatorView(discord.ui.LayoutView):
    """
    Registered in setup_hook so the dropdown on an existing /sendcalc
    message still works after a restart.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(ExchangeTypeSelect())
        self.add_item(row)


# ---------------------------------------------------------------------------
# Ticket system
# ---------------------------------------------------------------------------

def ticket_panel_text(server_name: str) -> str:
    return (
        f"# {EMOJI_THUNDER} {server_name} Exchange Ticket\n\n"
        f"Welcome to **{server_name} Exchange** — fast, secure & reliable exchanges.\n"
        "Select the exchange type below, enter the required details, and wait for a staff member.\n"
        f"# {EMOJI_EXCHANGE}  Exchanges\n\n"
        f"> {EMOJI_DOT} I2C → INR ➜ Crypto\n"
        f"> {EMOJI_DOT} C2I → Crypto ➜ INR\n\n"
        f"### {EMOJI_WARNING}  Important\n\n"
        "> * Minimum exchange: **$1 USD**\n"
        "> * Provide **correct payment details**.\n"
        "> * **Do not send funds** until staff confirms.\n"
        "> * Double-check wallet/account details.\n"
        "> * Scamming or wasting staff time may result in a **blacklist**."
    )


def ticket_details_text(
    user: discord.abc.User,
    staff_mention: str,
    have: str,
    want: str,
    pay_display: str,
    get_display: str,
) -> str:
    return (
        f"{user.mention} {staff_mention}\n\n"
        f"{EMOJI_DOT} **Currency {user.mention} Have:-**\n"
        f"`{have}`\n\n"
        f"{EMOJI_DOT} **Currency {user.mention} Want:-**\n"
        f"`{want}`\n\n"
        f"{EMOJI_DOT} **{user.mention} Have To Pay:-**\n"
        f"`{pay_display}`\n\n"
        f"{EMOJI_DOT} **{user.mention} Will Get:-**\n"
        f"`{get_display}`\n\n"
        "*__Kindly wait for staff to respond.__*"
    )


class CloseTicketButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Close Ticket",
            emoji=PARTIAL_CROSS,
            style=discord.ButtonStyle.danger,
            custom_id=TICKET_CLOSE_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_close_ticket(interaction)


class ClaimTicketButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Claim Ticket",
            emoji=PARTIAL_TICK,
            style=discord.ButtonStyle.success,
            custom_id=TICKET_CLAIM_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_claim_ticket(interaction)


class TicketTranscriptView(discord.ui.LayoutView):
    """Message posted inside a new ticket channel."""

    def __init__(self, text: str) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(CloseTicketButton())
        row.add_item(ClaimTicketButton())
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=ACCENT_GOLD,
            )
        )


class PersistentCloseTicketView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(CloseTicketButton())
        row.add_item(ClaimTicketButton())
        self.add_item(row)


class ExchangeTicketModal(discord.ui.Modal, title="Exchange Ticket"):
    currency_want = discord.ui.Label(
        text="Which Currency You want to exchange?",
        component=discord.ui.Select(
            placeholder="Select currency...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="INR", value="INR", emoji=PARTIAL_INR),
                discord.SelectOption(label="Crypto", value="Crypto", emoji=PARTIAL_CRYPTO),
            ],
        ),
    )
    currency_have = discord.ui.Label(
        text="Which Currency You Have?",
        component=discord.ui.Select(
            placeholder="Select currency...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="INR", value="INR", emoji=PARTIAL_INR),
                discord.SelectOption(label="Crypto", value="Crypto", emoji=PARTIAL_CRYPTO),
            ],
        ),
    )
    amount = discord.ui.Label(
        text="How Much You Want To Exchange?",
        component=discord.ui.TextInput(
            placeholder="e.g. 100 or 1.5",
            required=True,
            min_length=1,
            max_length=24,
            style=discord.TextStyle.short,
        ),
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        want_vals = self.currency_want.component.values
        have_vals = self.currency_have.component.values
        raw_amount = str(self.amount.component.value or "")

        if not want_vals or not have_vals:
            await send_v2(
                interaction,
                "## ❌ Missing Selection\n\nPlease choose both currencies.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        want = want_vals[0]
        have = have_vals[0]

        if have == want:
            await send_v2(
                interaction,
                "## ❌ Invalid Pair\n\nThe currency you have and the currency you want must be different.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        try:
            amount = parse_positive_decimal(raw_amount)
        except ValueError:
            await send_v2(
                interaction,
                "## ❌ Invalid Amount\n\nPlease enter a valid amount greater than 0.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        await create_exchange_ticket(interaction, have=have, want=want, amount=amount)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Ticket modal error: %s", error)
        await send_v2(
            interaction,
            "## ❌ Something Went Wrong\n\nPlease try again.",
            color=ACCENT_ERR,
            ephemeral=True,
        )


class OpenTicketButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Exchange",
            emoji=PARTIAL_EXCHANGE,
            style=discord.ButtonStyle.primary,
            custom_id=TICKET_OPEN_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ExchangeTicketModal())


class TicketPanel(discord.ui.LayoutView):
    def __init__(self, server_name: str) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(OpenTicketButton())
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(ticket_panel_text(server_name)),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=ACCENT_GOLD,
            )
        )


class PersistentTicketPanelView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(OpenTicketButton())
        self.add_item(row)


async def create_exchange_ticket(
    interaction: discord.Interaction,
    *,
    have: str,
    want: str,
    amount: Decimal,
) -> None:
    if interaction.guild is None:
        await send_v2(
            interaction,
            "## ❌ Server Only\n\nTickets can only be opened inside a server.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    settings = await get_settings()
    category_id = settings.get("ticket_category_id")
    staff_role_id = settings.get("staff_role_id")

    if category_id is None:
        await send_v2(
            interaction,
            "## ❌ Ticket Category Not Set\n\nAn administrator must run `/tixcatogery` first.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    if staff_role_id is None:
        await send_v2(
            interaction,
            "## ❌ Staff Role Not Set\n\nAn administrator must run `/tixstaff` first.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    category = interaction.guild.get_channel(int(category_id))
    staff_role = interaction.guild.get_role(int(staff_role_id))
    if not isinstance(category, discord.CategoryChannel):
        await send_v2(
            interaction,
            "## ❌ Ticket Category Missing\n\nThe saved category was deleted. Set it again with `/tixcatogery`.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    if staff_role is None:
        await send_v2(
            interaction,
            "## ❌ Staff Role Missing\n\nThe saved staff role was deleted. Set it again with `/tixstaff`.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    # Amount is the currency the user HAS.
    if have == "INR" and want == "Crypto":
        rate = await get_rate("i2c")
        if rate is None:
            await send_v2(
                interaction,
                "## ❌ Rate Not Configured\n\nThe I2C rate has not been set yet.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return
        crypto_out = (amount / rate).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        if crypto_out < Decimal("1"):
            await send_v2(
                interaction,
                "## ❌ Minimum Not Met\n\nMinimum exchange is **$1 USD**.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return
        pay_display = format_inr(amount)
        get_display = f"${format_crypto(crypto_out)}"
    elif have == "Crypto" and want == "INR":
        rate = await get_rate("c2i")
        if rate is None:
            await send_v2(
                interaction,
                "## ❌ Rate Not Configured\n\nThe C2I rate has not been set yet.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return
        if amount < Decimal("1"):
            await send_v2(
                interaction,
                "## ❌ Minimum Not Met\n\nMinimum exchange is **$1 USD**.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return
        inr_out = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        pay_display = f"${format_crypto(amount)}"
        get_display = format_inr(inr_out)
    else:
        await send_v2(
            interaction,
            "## ❌ Invalid Pair\n\nPlease choose INR ➜ Crypto or Crypto ➜ INR.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    number = await next_ticket_number()
    channel_name = ticket_channel_name(interaction.user.name, number)
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
        interaction.guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
        ),
        staff_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            manage_messages=True,
        ),
    }

    try:
        ticket_channel = await interaction.guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"owner:{interaction.user.id}|ticket:{number}",
            reason=f"Exchange ticket #{number} for {interaction.user}",
        )
    except discord.Forbidden:
        await send_v2(
            interaction,
            "## ❌ Missing Permissions\n\n"
            "I need **Manage Channels** in that category to open tickets.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    except discord.HTTPException as exc:
        log.exception("Failed to create ticket channel: %s", exc)
        await send_v2(
            interaction,
            "## ❌ Ticket Failed\n\nCould not create the ticket channel. Please try again.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    details = ticket_details_text(
        interaction.user,
        staff_role.mention,
        have,
        want,
        pay_display,
        get_display,
    )
    await ticket_channel.send(
        view=TicketTranscriptView(details),
        allowed_mentions=discord.AllowedMentions(users=True, roles=True),
    )
    await upsert_process_session(
        ticket_channel.id,
        owner_id=interaction.user.id,
        have=have,
        want=want,
        pay_display=pay_display,
        get_display=get_display,
        stage="open",
    )
    await send_v2(
        interaction,
        f"## ✅ Ticket Opened\n\nYour ticket is {ticket_channel.mention}.",
        color=ACCENT_OK,
        ephemeral=True,
    )


async def handle_close_ticket(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await send_v2(
            interaction,
            "## ❌ Cannot Close\n\nThis button only works inside a ticket channel.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    channel = interaction.channel
    settings = await get_settings()
    staff_role_id = settings.get("staff_role_id")
    topic = channel.topic or ""
    owner_id = None
    if topic.startswith("owner:"):
        try:
            owner_id = int(topic.split("|", 1)[0].replace("owner:", "").strip())
        except ValueError:
            owner_id = None

    member = interaction.user
    is_admin = bool(getattr(member, "guild_permissions", None) and member.guild_permissions.administrator)
    is_staff = False
    if staff_role_id and isinstance(member, discord.Member):
        is_staff = any(role.id == int(staff_role_id) for role in member.roles)
    is_owner = owner_id is not None and member.id == owner_id

    if not (is_admin or is_staff or is_owner):
        await send_v2(
            interaction,
            "## ❌ Permission Denied\n\nOnly the ticket owner or staff can close this ticket.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    await send_v2(
        interaction,
        f"## {EMOJI_CROSS} Ticket Closed\n\nClosed by {member.mention}. This channel will be deleted.",
        color=ACCENT_ERR,
    )
    await delete_process_session(channel.id)
    await asyncio.sleep(3)
    try:
        await channel.delete(reason=f"Ticket closed by {member}")
    except discord.HTTPException as exc:
        log.warning("Could not delete ticket channel %s: %s", channel.id, exc)


async def handle_claim_ticket(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await send_v2(
            interaction,
            "## ❌ Cannot Claim\n\nThis button only works inside a ticket channel.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    channel = interaction.channel
    settings = await get_settings()
    staff_role_id = settings.get("staff_role_id")
    member = interaction.user
    is_admin = bool(getattr(member, "guild_permissions", None) and member.guild_permissions.administrator)
    is_staff = False
    if staff_role_id and isinstance(member, discord.Member):
        is_staff = any(role.id == int(staff_role_id) for role in member.roles)

    if not (is_admin or is_staff):
        await send_v2(
            interaction,
            "## ❌ Permission Denied\n\nOnly staff can claim this ticket.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    current = channel.name
    if current.lower().startswith("ticket-"):
        remaining = current.split("-", 1)[1]
    elif current.lower().startswith("claimed-"):
        parts = current.split("-")
        remaining = "-".join(parts[2:]) if len(parts) >= 3 else current
    else:
        remaining = current

    claimed_by = safe_name_part(getattr(member, "display_name", None) or member.name)
    new_name = f"Claimed-{claimed_by}-{remaining}"
    if len(new_name) > 100:
        new_name = new_name[:100]

    topic = channel.topic or ""
    extra = f"claimed:{member.id}"
    if "claimed:" in topic:
        bits = [part for part in topic.split("|") if not part.startswith("claimed:")]
        bits.append(extra)
        new_topic = "|".join(bits)
    else:
        new_topic = f"{topic}|{extra}" if topic else extra

    try:
        await channel.edit(name=new_name, topic=new_topic, reason=f"Ticket claimed by {member}")
    except discord.Forbidden:
        await send_v2(
            interaction,
            "## ❌ Missing Permissions\n\nI need **Manage Channels** to rename this ticket.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    except discord.HTTPException as exc:
        log.warning("Could not rename ticket channel %s: %s", channel.id, exc)
        await send_v2(
            interaction,
            "## ❌ Rename Failed\n\nCould not rename this ticket. Please try again.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    await send_v2(
        interaction,
        f"## ✅ Ticket Claimed\n\nClaimed by {member.mention}.\nNew name: `{new_name}`",
        color=ACCENT_OK,
    )


class AddySelect(discord.ui.Select):
    def __init__(self, wallets: list[dict[str, str]]) -> None:
        options = [
            discord.SelectOption(
                label=item["name"][:100],
                description="View saved LTC address",
                value=str(index),
                emoji=PARTIAL_TICK,
            )
            for index, item in enumerate(wallets[:25])
        ]
        super().__init__(
            placeholder="Select an owner name…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.wallets = wallets[:25]

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            index = int(self.values[0])
            item = self.wallets[index]
        except (ValueError, IndexError):
            await send_v2(
                interaction,
                "## ❌ Not Found\n\nThat LTC addy is no longer saved.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        text = (
            f"{EMOJI_DETAILS}  **NAME:** `{item['name']}`\n"
            f"{EMOJI_DETAILS}  **LTC Address:** `{item['address']}`"
        )
        await send_v2(interaction, text, color=ACCENT_GOLD, ephemeral=True)


class ListAddyView(discord.ui.LayoutView):
    def __init__(self, wallets: list[dict[str, str]]) -> None:
        super().__init__(timeout=300)
        row = discord.ui.ActionRow()
        row.add_item(AddySelect(wallets))
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    f"## {EMOJI_WALLET}  All Saved LTC Addy\n\n"
                    f"{EMOJI_DETAILS}  Click The Drop-down Below To View All LTC Addy\n\n"
                    f"-# {EMOJI_WARNING}  Use /set_addy to save any LTC Addy."
                ),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=ACCENT_GOLD,
            )
        )


class UpiSelect(discord.ui.Select):
    def __init__(self, entries: list[dict[str, str]]) -> None:
        options = [
            discord.SelectOption(
                label=item["name"][:100],
                description="View saved UPI ID",
                value=str(index),
                emoji=PARTIAL_TICK,
            )
            for index, item in enumerate(entries[:25])
        ]
        super().__init__(
            placeholder="Select an owner name…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.entries = entries[:25]

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            index = int(self.values[0])
            item = self.entries[index]
        except (ValueError, IndexError):
            await send_v2(
                interaction,
                "## ❌ Not Found\n\nThat UPI ID is no longer saved.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        text = (
            f"{EMOJI_DETAILS}  **NAME:** `{item['name']}`\n"
            f"{EMOJI_DETAILS}  **UPI ID:** `{item['upi_id']}`"
        )
        await send_v2(interaction, text, color=ACCENT_GOLD, ephemeral=True)


class ListUpiView(discord.ui.LayoutView):
    def __init__(self, entries: list[dict[str, str]]) -> None:
        super().__init__(timeout=300)
        row = discord.ui.ActionRow()
        row.add_item(UpiSelect(entries))
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    f"## {EMOJI_WALLET}  All Saved UPI IDs\n\n"
                    f"{EMOJI_DETAILS}  Click The Drop-down Below To View All UPI IDs\n\n"
                    f"-# {EMOJI_WARNING}  Use /set_upi to save any UPI ID."
                ),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=ACCENT_GOLD,
            )
        )


# ---------------------------------------------------------------------------
# /process exchange flow
# ---------------------------------------------------------------------------

def build_payment_options() -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []
    for index, item in enumerate(load_wallets_sync()):
        if len(options) >= 25:
            break
        options.append(
            discord.SelectOption(
                label=f"{item['name']} — LTC"[:100],
                description=item["address"][:100],
                value=f"ltc|{index}",
                emoji=PARTIAL_CRYPTO,
            )
        )
    for index, item in enumerate(load_upi_sync()):
        if len(options) >= 25:
            break
        options.append(
            discord.SelectOption(
                label=f"{item['name']} — UPI"[:100],
                description=item["upi_id"][:100],
                value=f"upi|{index}",
                emoji=PARTIAL_INR,
            )
        )
    return options


def resolve_payment_choice(value: str) -> Optional[dict[str, str]]:
    kind, _, raw_index = value.partition("|")
    try:
        index = int(raw_index)
    except ValueError:
        return None
    if kind == "ltc":
        wallets = load_wallets_sync()
        if 0 <= index < len(wallets):
            item = wallets[index]
            return {
                "kind": "LTC",
                "name": item["name"],
                "details": item["address"],
            }
    if kind == "upi":
        entries = load_upi_sync()
        if 0 <= index < len(entries):
            item = entries[index]
            return {
                "kind": "UPI",
                "name": item["name"],
                "details": item["upi_id"],
            }
    return None


def tos_text(server_name: str) -> str:
    return (
        f"# {EMOJI_THUNDER} {server_name} EXCHANGE — TERMS OF SERVICE\n\n"
        f"**> {EMOJI_DOT_PLAIN} Payment Method: You must tell us which payment app/method you will be using before opening a deal.\n"
        f"> {EMOJI_DOT_PLAIN} Refund Policy: Once a transaction has started, no refunds will be provided.\n"
        f"> {EMOJI_DOT_PLAIN} Third-Party Payments: Not accepted under any circumstances.\n"
        f"> {EMOJI_DOT_PLAIN} Payment Proof: A valid payment screenshot (SS) is required. Transactions without proof will not be accepted.\n"
        f"> {EMOJI_DOT_PLAIN} Patience: Please be patient while your exchange is being processed.\n"
        f"> {EMOJI_SA_TICK} By opening a deal with {server_name} Exchange, you acknowledge that you have read, understood, and agreed to these Terms of Service.**"
    )


def payment_details_text(amount: str, wallet_block: str) -> str:
    return (
        f"### {EMOJI_NOTE} Payment Details\n\n"
        "Send Payment here, send exact payment value:\n"
        f"> **Sending Amount:** {amount}\n\n"
        f"{wallet_block}"
    )


class ProcessPaymentSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(
            placeholder="Choose a wallet or UPI…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_administrator(interaction):
            await send_permission_denied(interaction)
            return
        if interaction.channel is None or not is_ticket_channel(interaction.channel):
            await send_v2(
                interaction,
                "## ❌ Ticket Only\n\nThis selection only works inside a ticket.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        choice = resolve_payment_choice(self.values[0])
        if choice is None:
            await send_v2(
                interaction,
                "## ❌ Not Found\n\nThat payment method is no longer saved.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        wallet_block = (
            f"**{choice['name']} — {choice['kind']}**\n"
            f"`{choice['details']}`"
        )
        await upsert_process_session(
            interaction.channel.id,
            admin_id=interaction.user.id,
            selected_kind=choice["kind"],
            selected_name=choice["name"],
            selected_details=choice["details"],
            selected_block=wallet_block,
            stage="tos",
        )
        server_name = guild_display_name(interaction)
        await interaction.response.send_message(
            view=NoticeView("Payment method selected for this ticket.", color=ACCENT_OK),
            ephemeral=True,
        )
        await interaction.channel.send(view=TosView(tos_text(server_name)))


class ProcessSelectView(discord.ui.LayoutView):
    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(timeout=300)
        row = discord.ui.ActionRow()
        row.add_item(ProcessPaymentSelect(options))
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("Kindly Choose any Wallet Or App Payment"),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=ACCENT_GOLD,
            )
        )


class TosYesButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Yes, I accept TOS",
            style=discord.ButtonStyle.success,
            custom_id=PROCESS_TOS_YES_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_tos_yes(interaction)


class TosNoButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="No, I don't accept TOS",
            style=discord.ButtonStyle.danger,
            custom_id=PROCESS_TOS_NO_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_tos_no(interaction)


class TosView(discord.ui.LayoutView):
    def __init__(self, text: str) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(TosYesButton())
        row.add_item(TosNoButton())
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=ACCENT_GOLD,
            )
        )


class PersistentTosView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(TosYesButton())
        row.add_item(TosNoButton())
        self.add_item(row)


class PaidButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="I have sent the payment",
            style=discord.ButtonStyle.success,
            custom_id=PROCESS_PAID_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_payment_sent(interaction)


class CopyDetailsButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Copy details",
            style=discord.ButtonStyle.secondary,
            custom_id=PROCESS_COPY_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_copy_details(interaction)


class PaymentInstructView(discord.ui.LayoutView):
    def __init__(self, text: str) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(PaidButton())
        row.add_item(CopyDetailsButton())
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=ACCENT_GOLD,
            )
        )


class PersistentPaymentInstructView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(PaidButton())
        row.add_item(CopyDetailsButton())
        self.add_item(row)


class OpenDetailsButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Enter your details",
            style=discord.ButtonStyle.primary,
            custom_id=PROCESS_DETAILS_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        session = await get_process_session(interaction.channel.id) if interaction.channel else None
        owner_id = session.get("owner_id") if session else None
        if owner_id and interaction.user.id != int(owner_id) and not is_administrator(interaction):
            await send_v2(
                interaction,
                "## ❌ Permission Denied\n\nOnly the ticket owner can submit details.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(UserPayoutModal())


class DetailsPromptView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(OpenDetailsButton())
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("Kindly Put your details here"),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=ACCENT_GOLD,
            )
        )


class PersistentDetailsPromptView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(OpenDetailsButton())
        self.add_item(row)


class UserPayoutModal(discord.ui.Modal, title="Your Details"):
    payment_address = discord.ui.Label(
        text="Your Payment Address?",
        component=discord.ui.TextInput(
            placeholder="Wallet / UPI / account details",
            required=True,
            min_length=1,
            max_length=200,
            style=discord.TextStyle.short,
        ),
    )
    payment_app = discord.ui.Label(
        text="Payment App Name?",
        component=discord.ui.TextInput(
            placeholder="e.g. PhonePe, GPay, Trust Wallet",
            required=True,
            min_length=1,
            max_length=80,
            style=discord.TextStyle.short,
        ),
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.channel is None or not is_ticket_channel(interaction.channel):
            await send_v2(
                interaction,
                "## ❌ Ticket Only\n\nSubmit this inside your ticket.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        address = str(self.payment_address.component.value or "").strip()
        app_name = str(self.payment_app.component.value or "").strip()
        session = await get_process_session(interaction.channel.id) or {}
        owner_id = session.get("owner_id")
        if owner_id and interaction.user.id != int(owner_id) and not is_administrator(interaction):
            await send_v2(
                interaction,
                "## ❌ Permission Denied\n\nOnly the ticket owner can submit details.",
                color=ACCENT_ERR,
                ephemeral=True,
            )
            return

        admin_id = session.get("admin_id")
        get_display = session.get("get_display") or "—"
        admin_mention = f"<@{admin_id}>" if admin_id else "Admin"
        text = (
            f"{interaction.user.mention} Have been verified\n"
            f"{admin_mention} should confirm the payment he send\n\n"
            "**Details:-**\n"
            f"`{address}`\n"
            f"`{app_name}`\n"
            f"`{get_display}`"
        )
        await upsert_process_session(
            interaction.channel.id,
            user_address=address,
            user_app=app_name,
            stage="awaiting_admin_confirm",
        )
        await interaction.response.send_message(
            view=NoticeView("Details submitted. Staff has been notified.", color=ACCENT_OK),
            ephemeral=True,
        )
        await interaction.channel.send(
            view=NoticeView(text, color=ACCENT_GOLD),
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Payout modal error: %s", error)
        await send_v2(
            interaction,
            "## ❌ Something Went Wrong\n\nPlease try again.",
            color=ACCENT_ERR,
            ephemeral=True,
        )


async def handle_tos_yes(interaction: discord.Interaction) -> None:
    if interaction.channel is None or not is_ticket_channel(interaction.channel):
        await send_v2(
            interaction,
            "## ❌ Ticket Only\n\nThis button only works inside a ticket.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    session = await get_process_session(interaction.channel.id)
    if not session or not session.get("selected_details"):
        await send_v2(
            interaction,
            "## ❌ No Session\n\nAn admin must run `/process` and choose a payment method first.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    owner_id = session.get("owner_id")
    if owner_id and interaction.user.id != int(owner_id) and not is_administrator(interaction):
        await send_v2(
            interaction,
            "## ❌ Permission Denied\n\nOnly the ticket owner can accept the TOS.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    await upsert_process_session(interaction.channel.id, stage="awaiting_payment")
    amount = session.get("pay_display") or "—"
    wallet_block = session.get("selected_block") or f"`{session.get('selected_details')}`"
    await interaction.response.send_message(
        view=PaymentInstructView(payment_details_text(str(amount), str(wallet_block)))
    )


async def handle_tos_no(interaction: discord.Interaction) -> None:
    if interaction.channel is None or not is_ticket_channel(interaction.channel):
        await send_v2(
            interaction,
            "## ❌ Ticket Only\n\nThis button only works inside a ticket.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    session = await get_process_session(interaction.channel.id) or {}
    owner_id = session.get("owner_id")
    if owner_id and interaction.user.id != int(owner_id) and not is_administrator(interaction):
        await send_v2(
            interaction,
            "## ❌ Permission Denied\n\nOnly the ticket owner can decline the TOS.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    channel = interaction.channel
    await send_v2(
        interaction,
        "## Ticket Closed\n\nTOS was not accepted. This ticket will be deleted.",
        color=ACCENT_ERR,
    )
    await delete_process_session(channel.id)
    await asyncio.sleep(3)
    try:
        await channel.delete(reason="User declined TOS")
    except discord.HTTPException as exc:
        log.warning("Could not delete ticket after TOS decline: %s", exc)


async def handle_copy_details(interaction: discord.Interaction) -> None:
    if interaction.channel is None:
        return
    session = await get_process_session(interaction.channel.id) or {}
    details = session.get("selected_details")
    if not details:
        await send_v2(
            interaction,
            "## ❌ No Details\n\nNo payment method is selected for this ticket.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    await interaction.response.send_message(f"`{details}`")


async def handle_payment_sent(interaction: discord.Interaction) -> None:
    if interaction.channel is None or not is_ticket_channel(interaction.channel):
        await send_v2(
            interaction,
            "## ❌ Ticket Only\n\nThis button only works inside a ticket.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    session = await get_process_session(interaction.channel.id) or {}
    owner_id = session.get("owner_id")
    if owner_id and interaction.user.id != int(owner_id) and not is_administrator(interaction):
        await send_v2(
            interaction,
            "## ❌ Permission Denied\n\nOnly the ticket owner can mark payment as sent.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    await upsert_process_session(interaction.channel.id, stage="awaiting_screenshot")
    await send_v2(interaction, "Kindly Send the payment screenshot.")


async def maybe_handle_screenshot(message: discord.Message) -> None:
    if message.author.bot or not is_ticket_channel(message.channel):
        return
    session = await get_process_session(message.channel.id)
    if not session or session.get("stage") != "awaiting_screenshot":
        return
    owner_id = session.get("owner_id")
    if owner_id and message.author.id != int(owner_id):
        return

    has_image = False
    for attachment in message.attachments:
        content_type = (attachment.content_type or "").lower()
        name = (attachment.filename or "").lower()
        if content_type.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            has_image = True
            break
    if not has_image:
        return

    await upsert_process_session(message.channel.id, stage="awaiting_user_details")
    await message.channel.send(view=DetailsPromptView())


# ---------------------------------------------------------------------------
# Help menu
# ---------------------------------------------------------------------------

def help_home_text(bot_name: str) -> str:
    return (
        f"## {bot_name} Help Menu\n\n"
        f"{bot_name} is a smart bot who can help you in many things, it's mainly made for "
        "exchange servers and moderation and made by leak X HUB. Kindly select anything "
        "from the drop-down to view further"
    )


def help_prefix_text(bot_name: str) -> str:
    return (
        f"## {bot_name} Prefix Commands\n\n"
        f"{EMOJI_DOT} `.i2c [amount]` — INR → crypto\n"
        f"{EMOJI_DOT} `.c2i [amount]` — crypto → INR\n"
        f"{EMOJI_DOT} `.ping` — Bot latency in ms\n"
        f"{EMOJI_DOT} `.help` — Help menu\n\n"
        f"{EMOJI_DOT} `.kick [@user] [reason]` — Kick a member · **Kick Members**\n"
        f"{EMOJI_DOT} `.ban [@user] [reason]` — Ban a member · **Ban Members**\n"
        f"{EMOJI_DOT} `.unban [@user]` — Unban a user · **Ban Members**\n"
        f"{EMOJI_DOT} `.mute [@user] [duration] [reason]` — Timeout a member · **Timeout Members**\n"
        f"{EMOJI_DOT} `.lock [#channel]` — Lock a channel · **Manage Channels**\n"
        f"{EMOJI_DOT} `.unlock [#channel]` — Unlock a channel · **Manage Channels**\n"
        f"{EMOJI_DOT} `.hide [#channel]` — Hide a channel · **Manage Channels**\n"
        f"{EMOJI_DOT} `.unhide [#channel]` — Unhide a channel · **Manage Channels**"
    )


def help_slash_text(bot_name: str) -> str:
    return (
        f"## {bot_name} Slash Commands\n\n"
        f"{EMOJI_DOT} `/help` — Help menu\n"
        f"{EMOJI_DOT} `/kick` `/ban` `/unban` `/mute` — Moderation · matching Discord perms\n"
        f"{EMOJI_DOT} `/lock` `/unlock` `/hide` `/unhide` — Channel tools · **Manage Channels**\n\n"
        f"{EMOJI_DOT} `/seti2c` `/setc2i` — Set exchange rates · **Administrator**\n"
        f"{EMOJI_DOT} `/sendcalc` — Post calculator panel · **Administrator**\n"
        f"{EMOJI_DOT} `/send_ticket` `/tixstaff` `/tixcatogery` `/process` — Tickets · **Administrator**\n"
        f"{EMOJI_DOT} `/set_addy` `/list_addy` `/send_addy` — LTC wallets · **Administrator**\n"
        f"{EMOJI_DOT} `/set_upi` `/list_upi` `/send_upi` — UPI IDs · **Administrator**\n"
        f"{EMOJI_DOT} `/set prefix` — Change prefix · **Administrator**\n"
        f"{EMOJI_DOT} `/gstart` — Start a giveaway · **Administrator**"
    )


def help_credits_text(bot_name: str) -> str:
    return (
        f"## {bot_name} — CREDITS\n\n"
        "«Smart Exchange & Moderation Bot»\n\n"
        "Developed By\n"
        "\"Leak X HUB\"\n\n"
        "Main Developer\n"
        "\"zyro_57\"\n\n"
        "Version\n"
        "\"1.0.0\"\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "© 2026 Leak X HUB — All Rights Reserved"
    )


class HelpSelect(discord.ui.Select):
    def __init__(self, bot_name: str) -> None:
        self.bot_name = bot_name
        super().__init__(
            placeholder="Select a help section…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Prefix Commands",
                    value="prefix",
                    emoji=PARTIAL_DEV,
                ),
                discord.SelectOption(
                    label="Slash Commands",
                    value="slash",
                    emoji=PARTIAL_SLASH,
                ),
                discord.SelectOption(
                    label="Credits",
                    value="credits",
                    emoji=PARTIAL_STAR,
                ),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        choice = self.values[0]
        if choice == "prefix":
            text = help_prefix_text(self.bot_name)
        elif choice == "slash":
            text = help_slash_text(self.bot_name)
        else:
            text = help_credits_text(self.bot_name)
        await interaction.response.edit_message(view=HelpView(self.bot_name, text))


class HelpView(discord.ui.LayoutView):
    def __init__(self, bot_name: str, text: Optional[str] = None) -> None:
        super().__init__(timeout=300)
        row = discord.ui.ActionRow()
        row.add_item(HelpSelect(bot_name))
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text or help_home_text(bot_name)),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=ACCENT_GOLD,
            )
        )


def current_bot_name() -> str:
    if bot.user is not None:
        return bot.user.display_name
    return "Exchange Bot"


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

async def resolve_prefix(bot: commands.Bot, message: discord.Message) -> list[str]:
    settings = load_settings_sync()
    prefix = str(settings.get("prefix") or ".")
    return commands.when_mentioned_or(prefix)(bot, message)


class ExchangeBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.all()
        super().__init__(command_prefix=resolve_prefix, intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        self.add_view(PersistentCalculatorView())
        self.add_view(PersistentTicketPanelView())
        self.add_view(PersistentCloseTicketView())
        self.add_view(PersistentTosView())
        self.add_view(PersistentPaymentInstructView())
        self.add_view(PersistentDetailsPromptView())

        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %s slash command(s) to guild %s.", len(synced), GUILD_ID)

        existing_global = await self.tree.fetch_commands(guild=None)
        if existing_global:
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            log.info("Removed %s leftover global command(s).", len(existing_global))

        self.loop.create_task(giveaway_watcher(self))

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="INR ↔ Crypto rates",
            )
        )

    async def on_message(self, message: discord.Message) -> None:
        if not message.author.bot:
            await maybe_handle_screenshot(message)
        await self.process_commands(message)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == (self.user.id if self.user else 0):
            return
        if FALCON_EMOJI_ID is None or payload.emoji.id != FALCON_EMOJI_ID:
            return
        await add_giveaway_entry(payload.message_id, payload.user_id)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if FALCON_EMOJI_ID is None or payload.emoji.id != FALCON_EMOJI_ID:
            return
        await remove_giveaway_entry(payload.message_id, payload.user_id)


bot = ExchangeBot()


async def send_prefix_v2(
    ctx: commands.Context,
    text: str,
    *,
    color: int = ACCENT_GOLD,
) -> None:
    await ctx.send(view=NoticeView(text, color=color))


@bot.command(name="help")
async def prefix_help(ctx: commands.Context) -> None:
    await ctx.send(view=HelpView(current_bot_name()))


@bot.command(name="ping")
async def prefix_ping(ctx: commands.Context) -> None:
    latency_ms = round(bot.latency * 1000)
    await send_prefix_v2(
        ctx,
        f"{EMOJI_DOT} Pong\n{EMOJI_DOT} Bot ping: `{latency_ms}ms`",
    )


@bot.tree.command(name="help", description="Show the help menu.")
async def slash_help(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(view=HelpView(current_bot_name()))


@bot.command(name="i2c")
async def prefix_i2c(ctx: commands.Context, amount: str | None = None) -> None:
    """INR you have → crypto you get."""
    if amount is None:
        prefix = load_settings_sync().get("prefix") or "."
        await send_prefix_v2(
            ctx,
            f"## ❌ Missing Amount\n\nUsage: `{prefix}i2c <inr_amount>`\nExample: `{prefix}i2c 290`",
            color=ACCENT_ERR,
        )
        return

    try:
        inr_amount = parse_positive_decimal(amount)
    except ValueError:
        await send_prefix_v2(
            ctx,
            "## ❌ Invalid Amount\n\nPlease enter a valid amount greater than 0.",
            color=ACCENT_ERR,
        )
        return

    rate = await get_rate("i2c")
    if rate is None:
        await send_prefix_v2(
            ctx,
            "## ❌ Rate Not Configured\n\nThe I2C rate has not been configured yet.",
            color=ACCENT_ERR,
        )
        return

    crypto = (inr_amount / rate).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    text = (
        "## 💱 I2C Calculation\n\n"
        f"**I2C Rate**\n`{format_rate_number(rate)} INR = $1`\n\n"
        f"**INR You Have**\n{format_inr(inr_amount)}\n\n"
        f"**Crypto You Will Get**\n`${format_crypto(crypto)}`"
    )
    await send_prefix_v2(ctx, text)


@bot.command(name="c2i")
async def prefix_c2i(ctx: commands.Context, amount: str | None = None) -> None:
    """Crypto you have → INR you get."""
    if amount is None:
        prefix = load_settings_sync().get("prefix") or "."
        await send_prefix_v2(
            ctx,
            f"## ❌ Missing Amount\n\nUsage: `{prefix}c2i <crypto_amount>`\nExample: `{prefix}c2i 3`",
            color=ACCENT_ERR,
        )
        return

    try:
        crypto_amount = parse_positive_decimal(amount)
    except ValueError:
        await send_prefix_v2(
            ctx,
            "## ❌ Invalid Amount\n\nPlease enter a valid amount greater than 0.",
            color=ACCENT_ERR,
        )
        return

    rate = await get_rate("c2i")
    if rate is None:
        await send_prefix_v2(
            ctx,
            "## ❌ Rate Not Configured\n\nThe C2I rate has not been configured yet.",
            color=ACCENT_ERR,
        )
        return

    inr = (crypto_amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = (
        "## 💱 C2I Calculation\n\n"
        f"**C2I Rate**\n`{format_rate_number(rate)} INR = $1`\n\n"
        f"**Crypto You Have**\n`${format_crypto(crypto_amount)}`\n\n"
        f"**INR You Will Get**\n{format_inr(inr)}"
    )
    await send_prefix_v2(ctx, text)


def _reason_text(reason: Optional[str]) -> str:
    cleaned = (reason or "").strip()
    return cleaned if cleaned else "none"


def _can_moderate(actor: discord.Member, target: discord.Member, bot_member: discord.Member) -> Optional[str]:
    if actor.id == target.id:
        return "You cannot moderate yourself."
    if target.id == actor.guild.owner_id:
        return "You cannot moderate the server owner."
    if actor.id != actor.guild.owner_id and actor.top_role <= target.top_role:
        return "That member has an equal or higher role than you."
    if bot_member.top_role <= target.top_role:
        return "My role is not high enough to moderate that member."
    return None


def _lock_text(channel: discord.abc.GuildChannel, actor: discord.abc.User, action: str) -> str:
    verb = {
        "locked": "Locked",
        "unlocked": "Unlocked",
        "hidden": "Hidden",
        "unhidden": "Unhidden",
    }[action]
    return (
        f"{EMOJI_DOT} Successfully {verb}: {channel.mention}\n"
        f"{EMOJI_DOT} {verb} By: {actor.mention}"
    )


@bot.tree.command(name="gstart", description="Start a giveaway. Administrator only.")
@app_commands.describe(
    time="Duration such as 10s, 5m, 1d, 1w",
    winners="How many winners",
    item="Prize name",
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def gstart(interaction: discord.Interaction, time: str, winners: int, item: str) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await send_v2(
            interaction,
            "## ❌ Channel Error\n\nRun this in a text channel.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    try:
        seconds, _label = parse_duration(time)
    except ValueError:
        await send_v2(
            interaction,
            "## ❌ Invalid Time\n\nUse a duration like `10s`, `5m`, `1d`, or `1w`.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    if winners < 1:
        await send_v2(
            interaction,
            "## ❌ Invalid Winners\n\nWinners must be at least 1.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    prize = item.strip()
    if not prize:
        await send_v2(
            interaction,
            "## ❌ Invalid Item\n\nPlease provide a prize name.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    end_ts = int(time_module_now() + seconds)
    text = giveaway_panel_text(prize, winners, end_ts, interaction.user)
    await interaction.response.send_message(view=GiveawayPanel(text))
    message = await interaction.original_response()
    try:
        await message.add_reaction(PARTIAL_FALCON)
    except discord.HTTPException as exc:
        log.warning("Could not add giveaway reaction: %s", exc)
    await add_giveaway(
        {
            "message_id": message.id,
            "channel_id": message.channel.id,
            "guild_id": interaction.guild_id,
            "item": prize,
            "winners": winners,
            "end_ts": end_ts,
            "host_id": interaction.user.id,
            "ended": False,
            "entries": [],
        }
    )


def time_module_now() -> int:
    return int(time.time())


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
@commands.guild_only()
async def prefix_kick(ctx: commands.Context, member: discord.Member, *, reason: str | None = None) -> None:
    assert isinstance(ctx.author, discord.Member) and ctx.guild and ctx.guild.me
    problem = _can_moderate(ctx.author, member, ctx.guild.me)
    if problem:
        await send_prefix_v2(ctx, f"## ❌ Cannot Kick\n\n{problem}", color=ACCENT_ERR)
        return
    why = _reason_text(reason)
    await member.kick(reason=f"{why} | by {ctx.author}")
    await send_prefix_v2(
        ctx,
        f"{EMOJI_DOT} Successfully Kicked: `{member}`\n"
        f"{EMOJI_DOT} Kicked By: `{ctx.author}`\n"
        f"{EMOJI_DOT} Reason: `{why}`",
    )


@bot.tree.command(name="kick", description="Kick a member. Requires Kick Members.")
@app_commands.describe(user="Member to kick", reason="Reason")
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.guild_only()
async def slash_kick(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str | None = None,
) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await send_v2(interaction, "## ❌ Server Only", color=ACCENT_ERR, ephemeral=True)
        return
    me = interaction.guild.me
    if me is None:
        return
    problem = _can_moderate(interaction.user, user, me)
    if problem:
        await send_v2(interaction, f"## ❌ Cannot Kick\n\n{problem}", color=ACCENT_ERR, ephemeral=True)
        return
    why = _reason_text(reason)
    await user.kick(reason=f"{why} | by {interaction.user}")
    await send_v2(
        interaction,
        f"{EMOJI_DOT} Successfully Kicked: `{user}`\n"
        f"{EMOJI_DOT} Kicked By: `{interaction.user}`\n"
        f"{EMOJI_DOT} Reason: `{why}`",
    )


@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
@commands.guild_only()
async def prefix_ban(ctx: commands.Context, member: discord.Member, *, reason: str | None = None) -> None:
    assert isinstance(ctx.author, discord.Member) and ctx.guild and ctx.guild.me
    problem = _can_moderate(ctx.author, member, ctx.guild.me)
    if problem:
        await send_prefix_v2(ctx, f"## ❌ Cannot Ban\n\n{problem}", color=ACCENT_ERR)
        return
    why = _reason_text(reason)
    await member.ban(reason=f"{why} | by {ctx.author}", delete_message_days=0)
    await send_prefix_v2(
        ctx,
        f"{EMOJI_DOT} Successfully Banned: `{member}`\n"
        f"{EMOJI_DOT} Banned By: `{ctx.author}`\n"
        f"{EMOJI_DOT} Reason: `{why}`",
    )


@bot.tree.command(name="ban", description="Ban a member. Requires Ban Members.")
@app_commands.describe(user="Member to ban", reason="Reason")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.guild_only()
async def slash_ban(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str | None = None,
) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await send_v2(interaction, "## ❌ Server Only", color=ACCENT_ERR, ephemeral=True)
        return
    me = interaction.guild.me
    if me is None:
        return
    problem = _can_moderate(interaction.user, user, me)
    if problem:
        await send_v2(interaction, f"## ❌ Cannot Ban\n\n{problem}", color=ACCENT_ERR, ephemeral=True)
        return
    why = _reason_text(reason)
    await user.ban(reason=f"{why} | by {interaction.user}", delete_message_days=0)
    await send_v2(
        interaction,
        f"{EMOJI_DOT} Successfully Banned: `{user}`\n"
        f"{EMOJI_DOT} Banned By: `{interaction.user}`\n"
        f"{EMOJI_DOT} Reason: `{why}`",
    )


@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
@commands.guild_only()
async def prefix_unban(ctx: commands.Context, user: str) -> None:
    assert ctx.guild
    target = None
    user_id = _parse_snowflake(user.replace("<@", "").replace(">", "").replace("!", ""))
    async for ban in ctx.guild.bans(limit=None):
        if user_id and ban.user.id == user_id:
            target = ban.user
            break
        if user.lower() in {ban.user.name.lower(), str(ban.user).lower()}:
            target = ban.user
            break
    if target is None:
        await send_prefix_v2(ctx, "## ❌ Not Found\n\nThat user is not banned, or use their ID.", color=ACCENT_ERR)
        return
    await ctx.guild.unban(target, reason=f"Unbanned by {ctx.author}")
    await send_prefix_v2(
        ctx,
        f"{EMOJI_DOT} Successfully Unbanned: `{target}`\n"
        f"{EMOJI_DOT} Unbanned By: `{ctx.author}`\n"
        f"{EMOJI_DOT} Reason: `none`",
    )


@bot.tree.command(name="unban", description="Unban a user. Requires Ban Members.")
@app_commands.describe(user="User to unban")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.guild_only()
async def slash_unban(interaction: discord.Interaction, user: discord.User) -> None:
    if interaction.guild is None:
        await send_v2(interaction, "## ❌ Server Only", color=ACCENT_ERR, ephemeral=True)
        return
    try:
        await interaction.guild.unban(user, reason=f"Unbanned by {interaction.user}")
    except discord.NotFound:
        await send_v2(interaction, "## ❌ Not Found\n\nThat user is not banned.", color=ACCENT_ERR, ephemeral=True)
        return
    await send_v2(
        interaction,
        f"{EMOJI_DOT} Successfully Unbanned: `{user}`\n"
        f"{EMOJI_DOT} Unbanned By: `{interaction.user}`\n"
        f"{EMOJI_DOT} Reason: `none`",
    )


@bot.command(name="mute")
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
@commands.guild_only()
async def prefix_mute(
    ctx: commands.Context,
    member: discord.Member,
    duration: str,
    *,
    reason: str | None = None,
) -> None:
    assert isinstance(ctx.author, discord.Member) and ctx.guild and ctx.guild.me
    problem = _can_moderate(ctx.author, member, ctx.guild.me)
    if problem:
        await send_prefix_v2(ctx, f"## ❌ Cannot Mute\n\n{problem}", color=ACCENT_ERR)
        return
    try:
        seconds, label = parse_duration(duration)
    except ValueError:
        await send_prefix_v2(ctx, "## ❌ Invalid Duration\n\nUse `10s`, `5m`, `1d`, or `1w`.", color=ACCENT_ERR)
        return
    seconds = min(seconds, 28 * 86400)
    why = _reason_text(reason)
    await member.timeout(timedelta(seconds=seconds), reason=f"{why} | by {ctx.author}")
    await send_prefix_v2(
        ctx,
        f"{EMOJI_DOT} Successfully Muted: `{member}`\n"
        f"{EMOJI_DOT} Muted By: `{ctx.author}`\n"
        f"{EMOJI_DOT} Duration: `{label}`\n"
        f"{EMOJI_DOT} Reason: `{why}`",
    )


@bot.tree.command(name="mute", description="Timeout a member. Requires Timeout Members.")
@app_commands.describe(user="Member to mute", duration="Duration such as 10m or 1d", reason="Reason")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.guild_only()
async def slash_mute(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str,
    reason: str | None = None,
) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await send_v2(interaction, "## ❌ Server Only", color=ACCENT_ERR, ephemeral=True)
        return
    me = interaction.guild.me
    if me is None:
        return
    problem = _can_moderate(interaction.user, user, me)
    if problem:
        await send_v2(interaction, f"## ❌ Cannot Mute\n\n{problem}", color=ACCENT_ERR, ephemeral=True)
        return
    try:
        seconds, label = parse_duration(duration)
    except ValueError:
        await send_v2(interaction, "## ❌ Invalid Duration\n\nUse `10s`, `5m`, `1d`, or `1w`.", color=ACCENT_ERR, ephemeral=True)
        return
    seconds = min(seconds, 28 * 86400)
    why = _reason_text(reason)
    await user.timeout(timedelta(seconds=seconds), reason=f"{why} | by {interaction.user}")
    await send_v2(
        interaction,
        f"{EMOJI_DOT} Successfully Muted: `{user}`\n"
        f"{EMOJI_DOT} Muted By: `{interaction.user}`\n"
        f"{EMOJI_DOT} Duration: `{label}`\n"
        f"{EMOJI_DOT} Reason: `{why}`",
    )


async def _set_channel_flag(
    channel: discord.TextChannel,
    *,
    send_messages: Optional[bool] = None,
    view_channel: Optional[bool] = None,
) -> None:
    overwrite = channel.overwrites_for(channel.guild.default_role)
    if send_messages is not None:
        overwrite.send_messages = send_messages
    if view_channel is not None:
        overwrite.view_channel = view_channel
    await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)


@bot.command(name="lock")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
@commands.guild_only()
async def prefix_lock(ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
    target = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
    if target is None:
        await send_prefix_v2(ctx, "## ❌ No Channel", color=ACCENT_ERR)
        return
    await _set_channel_flag(target, send_messages=False)
    await send_prefix_v2(ctx, _lock_text(target, ctx.author, "locked"))


@bot.tree.command(name="lock", description="Lock a channel. Requires Manage Channels.")
@app_commands.describe(channel="Channel to lock")
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.guild_only()
async def slash_lock(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    await _set_channel_flag(channel, send_messages=False)
    await send_v2(interaction, _lock_text(channel, interaction.user, "locked"))


@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
@commands.guild_only()
async def prefix_unlock(ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
    target = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
    if target is None:
        await send_prefix_v2(ctx, "## ❌ No Channel", color=ACCENT_ERR)
        return
    await _set_channel_flag(target, send_messages=True)
    await send_prefix_v2(ctx, _lock_text(target, ctx.author, "unlocked"))


@bot.tree.command(name="unlock", description="Unlock a channel. Requires Manage Channels.")
@app_commands.describe(channel="Channel to unlock")
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.guild_only()
async def slash_unlock(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    await _set_channel_flag(channel, send_messages=True)
    await send_v2(interaction, _lock_text(channel, interaction.user, "unlocked"))


@bot.command(name="hide")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
@commands.guild_only()
async def prefix_hide(ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
    target = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
    if target is None:
        await send_prefix_v2(ctx, "## ❌ No Channel", color=ACCENT_ERR)
        return
    await _set_channel_flag(target, view_channel=False)
    await send_prefix_v2(ctx, _lock_text(target, ctx.author, "hidden"))


@bot.tree.command(name="hide", description="Hide a channel. Requires Manage Channels.")
@app_commands.describe(channel="Channel to hide")
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.guild_only()
async def slash_hide(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    await _set_channel_flag(channel, view_channel=False)
    await send_v2(interaction, _lock_text(channel, interaction.user, "hidden"))


@bot.command(name="unhide")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
@commands.guild_only()
async def prefix_unhide(ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
    target = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
    if target is None:
        await send_prefix_v2(ctx, "## ❌ No Channel", color=ACCENT_ERR)
        return
    await _set_channel_flag(target, view_channel=True)
    await send_prefix_v2(ctx, _lock_text(target, ctx.author, "unhidden"))


@bot.tree.command(name="unhide", description="Unhide a channel. Requires Manage Channels.")
@app_commands.describe(channel="Channel to unhide")
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.guild_only()
async def slash_unhide(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    await _set_channel_flag(channel, view_channel=True)
    await send_v2(interaction, _lock_text(channel, interaction.user, "unhidden"))


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, (commands.MissingPermissions, commands.BotMissingPermissions)):
        await send_prefix_v2(
            ctx,
            "## ❌ Permission Denied\n\nYou or the bot is missing the required permission.",
            color=ACCENT_ERR,
        )
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await send_prefix_v2(ctx, f"## ❌ Missing Argument\n\nMissing `{error.param.name}`.", color=ACCENT_ERR)
        return
    if isinstance(error, commands.BadArgument):
        await send_prefix_v2(ctx, "## ❌ Invalid Argument\n\nCheck the user or channel you provided.", color=ACCENT_ERR)
        return
    log.exception("Prefix command error: %s", error)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def is_administrator(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


async def send_permission_denied(interaction: discord.Interaction) -> None:
    await send_v2(
        interaction,
        "## ❌ Permission Denied\n\nYou must have Administrator permissions to use this command.",
        color=ACCENT_ERR,
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="seti2c",
    description="Set the INR to Crypto/USD rate (INR per $1). Administrator only.",
)
@app_commands.describe(inr_per_dollar="INR PER DOLLAR — how many rupees equal $1")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def seti2c(interaction: discord.Interaction, inr_per_dollar: float) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    try:
        rate = parse_positive_decimal(str(inr_per_dollar))
    except ValueError:
        await send_v2(
            interaction,
            "## ❌ Invalid Rate\n\nPlease enter a valid rate greater than 0. Decimals are allowed.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    await set_rate("i2c", rate)
    await send_v2(
        interaction,
        "## ✅ I2C Rate Updated\n\n"
        f"**New I2C Rate**\n`{format_rate_number(rate)} INR = $1`\n\n"
        f"**Updated by**\n{interaction.user.mention}",
        color=ACCENT_OK,
    )


@bot.tree.command(
    name="setc2i",
    description="Set the Crypto/USD to INR rate (INR per $1). Administrator only.",
)
@app_commands.describe(dollar_per_inr="DOLLAR PER INR — enter the INR value of $1 for C2I")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def setc2i(interaction: discord.Interaction, dollar_per_inr: float) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    try:
        rate = parse_positive_decimal(str(dollar_per_inr))
    except ValueError:
        await send_v2(
            interaction,
            "## ❌ Invalid Rate\n\nPlease enter a valid rate greater than 0. Decimals are allowed.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    await set_rate("c2i", rate)
    await send_v2(
        interaction,
        "## ✅ C2I Rate Updated\n\n"
        f"**New C2I Rate**\n`{format_rate_number(rate)} INR = $1`\n\n"
        f"**Updated by**\n{interaction.user.mention}",
        color=ACCENT_OK,
    )


@bot.tree.command(
    name="sendcalc",
    description="Post the exchange calculator panel. Administrator only.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def sendcalc(interaction: discord.Interaction) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    i2c = await get_rate("i2c")
    c2i = await get_rate("c2i")
    server_name = guild_display_name(interaction)
    await interaction.response.send_message(
        view=CalculatorPanel(server_name, i2c, c2i)
    )


@bot.tree.command(
    name="send_ticket",
    description="Post the exchange ticket panel in a channel. Administrator only.",
)
@app_commands.describe(channel="Channel where the ticket panel will be sent")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def send_ticket(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    server_name = guild_display_name(interaction)
    try:
        await channel.send(view=TicketPanel(server_name))
    except discord.Forbidden:
        await send_v2(
            interaction,
            f"## ❌ Missing Permissions\n\nI cannot send messages in {channel.mention}.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    await send_v2(
        interaction,
        f"## ✅ Ticket Panel Sent\n\nThe panel was posted in {channel.mention}.",
        color=ACCENT_OK,
        ephemeral=True,
    )


@bot.tree.command(
    name="tixstaff",
    description="Set the staff role pinged on new tickets. Administrator only.",
)
@app_commands.describe(role="Staff role that can see and handle tickets")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def tixstaff(interaction: discord.Interaction, role: discord.Role) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    await update_settings(staff_role_id=role.id)
    await send_v2(
        interaction,
        f"## ✅ Ticket Staff Updated\n\nStaff role set to {role.mention}.",
        color=ACCENT_OK,
    )


@bot.tree.command(
    name="tixcatogery",
    description="Set the category where tickets will be created. Administrator only.",
)
@app_commands.describe(category="Category where private ticket channels will open")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def tixcatogery(
    interaction: discord.Interaction,
    category: discord.CategoryChannel,
) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    await update_settings(ticket_category_id=category.id)
    await send_v2(
        interaction,
        f"## ✅ Ticket Category Updated\n\nTickets will open under **{category.name}**.",
        color=ACCENT_OK,
    )


@bot.tree.command(
    name="set_addy",
    description="Save an LTC address. Administrator only.",
)
@app_commands.describe(
    ltc_address="LTC Address",
    addy_owner_name="Addy Owner Name",
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def set_addy(
    interaction: discord.Interaction,
    ltc_address: str,
    addy_owner_name: str,
) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    address = ltc_address.strip()
    name = addy_owner_name.strip()
    if not address or not name:
        await send_v2(
            interaction,
            "## ❌ Invalid Details\n\nPlease provide both an LTC address and an owner name.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    await save_wallet(name, address)
    text = (
        f"## {EMOJI_WALLET}  LTC Addy Saved\n\n"
        f"{EMOJI_DETAILS} __LTC Addy Details__:-\n\n"
        f"{EMOJI_DOT_ALT}  **LTC Addy** {EMOJI_ARROW_ADDY} `{address}`\n\n"
        f"{EMOJI_DOT_ALT}  **Addy Owner Name** {EMOJI_ARROW_NAME} `{name}`\n\n"
        f"-# {EMOJI_WARNING} __Use /list_addy To view all saved LTC Addy's.__"
    )
    await send_v2(interaction, text, color=ACCENT_OK)


@bot.tree.command(
    name="list_addy",
    description="View all saved LTC addresses. Administrator only.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def list_addy(interaction: discord.Interaction) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    wallets = await get_wallets()
    if not wallets:
        await send_v2(
            interaction,
            f"## {EMOJI_WALLET}  All Saved LTC Addy\n\n"
            "No LTC addys are saved yet.\n\n"
            f"-# {EMOJI_WARNING}  Use /set_addy to save any LTC Addy.",
            color=ACCENT_GOLD,
            ephemeral=True,
        )
        return

    await interaction.response.send_message(view=ListAddyView(wallets), ephemeral=True)


@bot.tree.command(
    name="set_upi",
    description="Save a UPI ID. Administrator only.",
)
@app_commands.describe(
    upi_id="UPI ID",
    owner_name="UPI Owner Name",
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def set_upi(
    interaction: discord.Interaction,
    upi_id: str,
    owner_name: str,
) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    upi = upi_id.strip()
    name = owner_name.strip()
    if not upi or not name:
        await send_v2(
            interaction,
            "## ❌ Invalid Details\n\nPlease provide both a UPI ID and an owner name.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    await save_upi(name, upi)
    text = (
        f"## {EMOJI_WALLET}  UPI ID Saved\n\n"
        f"{EMOJI_DETAILS} __UPI Details__:-\n\n"
        f"{EMOJI_DOT_ALT}  **UPI ID** {EMOJI_ARROW_ADDY} `{upi}`\n\n"
        f"{EMOJI_DOT_ALT}  **Owner Name** {EMOJI_ARROW_NAME} `{name}`\n\n"
        f"-# {EMOJI_WARNING} __Use /list_upi To view all saved UPI IDs.__"
    )
    await send_v2(interaction, text, color=ACCENT_OK)


@bot.tree.command(
    name="list_upi",
    description="View all saved UPI IDs. Administrator only.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def list_upi(interaction: discord.Interaction) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    entries = await get_upi_entries()
    if not entries:
        await send_v2(
            interaction,
            f"## {EMOJI_WALLET}  All Saved UPI IDs\n\n"
            "No UPI IDs are saved yet.\n\n"
            f"-# {EMOJI_WARNING}  Use /set_upi to save any UPI ID.",
            color=ACCENT_GOLD,
            ephemeral=True,
        )
        return

    await interaction.response.send_message(view=ListUpiView(entries), ephemeral=True)


@bot.tree.command(
    name="process",
    description="Start the exchange process in this ticket. Administrator only.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def process_cmd(interaction: discord.Interaction) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return
    if interaction.channel is None or not is_ticket_channel(interaction.channel):
        await send_v2(
            interaction,
            "## ❌ Ticket Only\n\n`/process` can only be used inside a ticket channel.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    session = await get_process_session(interaction.channel.id)
    if session is None:
        owner_id = owner_id_from_topic(getattr(interaction.channel, "topic", None))
        await upsert_process_session(
            interaction.channel.id,
            owner_id=owner_id,
            stage="selecting",
        )

    options = build_payment_options()
    if not options:
        await send_v2(
            interaction,
            "## ❌ No Payment Methods\n\nSave at least one LTC addy with `/set_addy` or one UPI ID with `/set_upi`.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        view=ProcessSelectView(options),
        ephemeral=True,
    )


@bot.tree.command(
    name="send_addy",
    description="Post a saved LTC address by owner name. Administrator only.",
)
@app_commands.describe(name="Owner name saved with /set_addy")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def send_addy(interaction: discord.Interaction, name: str) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    query = name.strip()
    wallets = await get_wallets()
    match = next((item for item in wallets if item["name"].lower() == query.lower()), None)
    if match is None:
        await send_v2(
            interaction,
            f"## ❌ Not Found\n\nNo LTC addy saved under `{query}`.\nUse `/set_addy` or `/list_addy`.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    text = (
        f"{EMOJI_DETAILS}  **NAME:** `{match['name']}`\n"
        f"{EMOJI_DETAILS}  **LTC Address:** `{match['address']}`"
    )
    await send_v2(interaction, text, color=ACCENT_GOLD)


@bot.tree.command(
    name="send_upi",
    description="Post a saved UPI ID by owner name. Administrator only.",
)
@app_commands.describe(name="Owner name saved with /set_upi")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def send_upi(interaction: discord.Interaction, name: str) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    query = name.strip()
    entries = await get_upi_entries()
    match = next((item for item in entries if item["name"].lower() == query.lower()), None)
    if match is None:
        await send_v2(
            interaction,
            f"## ❌ Not Found\n\nNo UPI ID saved under `{query}`.\nUse `/set_upi` or `/list_upi`.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    text = (
        f"{EMOJI_DETAILS}  **NAME:** `{match['name']}`\n"
        f"{EMOJI_DETAILS}  **UPI ID:** `{match['upi_id']}`"
    )
    await send_v2(interaction, text, color=ACCENT_GOLD)


set_group = app_commands.Group(
    name="set",
    description="Bot settings",
    default_permissions=discord.Permissions(administrator=True),
    guild_only=True,
)


@set_group.command(name="prefix", description="Change the bot text-command prefix. Administrator only.")
@app_commands.describe(new_prefix="New prefix, for example . or ! or ?")
async def set_prefix(interaction: discord.Interaction, new_prefix: str) -> None:
    if not is_administrator(interaction):
        await send_permission_denied(interaction)
        return

    prefix = new_prefix.strip()
    if not prefix:
        await send_v2(
            interaction,
            "## ❌ Invalid Prefix\n\nPrefix cannot be empty.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    if len(prefix) > 5:
        await send_v2(
            interaction,
            "## ❌ Invalid Prefix\n\nPrefix must be 1 to 5 characters.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return
    if any(ch.isspace() for ch in prefix):
        await send_v2(
            interaction,
            "## ❌ Invalid Prefix\n\nPrefix cannot contain spaces.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    await update_settings(prefix=prefix)
    await send_v2(
        interaction,
        f"## ✅ Prefix Updated\n\nNew prefix: `{prefix}`\nExample: `{prefix}help`",
        color=ACCENT_OK,
    )


bot.tree.add_command(set_group)


# ---------------------------------------------------------------------------
# App-command errors
# ---------------------------------------------------------------------------

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    original = error.original if isinstance(error, app_commands.CommandInvokeError) else error

    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        await send_permission_denied(interaction)
        return

    if isinstance(error, app_commands.NoPrivateMessage):
        await send_v2(
            interaction,
            "## ❌ Server Only\n\nThis command can only be used inside a Discord server.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
        return

    log.exception("Slash command error: %s", original)
    try:
        await send_v2(
            interaction,
            "## ❌ Something Went Wrong\n\nAn unexpected error occurred. Please try again.",
            color=ACCENT_ERR,
            ephemeral=True,
        )
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not DISCORD_TOKEN or DISCORD_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise SystemExit(
            "Bot token is missing. Open config.json and set the \"token\" field."
        )
    if not RATES_FILE.exists():
        save_rates_sync(_default_rates())
    if not SETTINGS_FILE.exists():
        save_settings_sync(_default_settings())
    if not WALLETS_FILE.exists():
        save_wallets_sync([])
    if not UPI_FILE.exists():
        save_upi_sync([])
    if not PROCESS_FILE.exists():
        save_process_sync({})
    if not GIVEAWAYS_FILE.exists():
        save_giveaways_sync([])
    if not EMOJIS_FILE.exists():
        load_emojis()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
