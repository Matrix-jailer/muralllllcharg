import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List
from functools import partial

import aiohttp
import aiosqlite
try:
    from bs4 import BeautifulSoup  # Optional; if missing, BIN details will be minimal
except Exception:  # pragma: no cover
    BeautifulSoup = None

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

logging.basicConfig(level=logging.INFO)

# =====================
# Environment / Settings
# =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x]
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "")  # without @ or with, both ok
NEW_USER_CHANNEL_ID = int(os.getenv("NEW_USER_CHANNEL_ID", "0") or 0)
CHECK_RESULTS_CHANNEL_ID = int(os.getenv("CHECK_RESULTS_CHANNEL_ID", "0") or 0)
FREE_REG_CREDITS = int(os.getenv("FREE_REG_CREDITS", "10") or 10)

DB_PATH = os.getenv("DB_PATH", "bot.db")
BASE_CC_API = "https://hazunamadadacharg.onrender.com/ccngate/"

# =====================
# FSM
# =====================
class Flow(StatesGroup):
    in_commands = State()
    in_gate_ccn = State()
    in_gate_mccn = State()

processing_users: Dict[int, bool] = {}

# =====================
# Pre-populate existing users data
# =====================
EXISTING_USERS_DATA = [
    {"tg_id": 6822528184, "username": "Utopiacorner49494", "full_name": "UwU", "credits": 10, "joined_at": "2025-09-12"},
    {"tg_id": 35984590, "username": "Samangh88", "full_name": "Saman", "credits": 10, "joined_at": "2025-09-12"}
]

# =====================
# DB Helpers
# =====================
async def open_db():
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            username TEXT,
            full_name TEXT,
            credits INTEGER DEFAULT 0,
            banned_until TEXT,
            joined_at TEXT,
            is_admin INTEGER DEFAULT 0
        );
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    await db.commit()
    
    # Pre-populate existing users if they don't exist
    await populate_existing_users(db)
    
    return db

async def populate_existing_users(db):
    """Pre-populate database with existing users to preserve data across restarts"""
    for user_data in EXISTING_USERS_DATA:
        existing = await get_user(db, user_data["tg_id"])
        if not existing:
            is_admin = 1 if user_data["tg_id"] in ADMIN_USER_IDS else 0
            joined_at = f"{user_data['joined_at']}T00:00:00+00:00"
            await db.execute(
                "INSERT OR IGNORE INTO users(tg_id, username, full_name, credits, banned_until, joined_at, is_admin) VALUES(?,?,?,?,?,?,?)",
                (user_data["tg_id"], user_data["username"], user_data["full_name"], user_data["credits"], None, joined_at, is_admin),
            )
    await db.commit()

async def get_user(db, tg_id: int):
    c = await db.execute(
        "SELECT tg_id, username, full_name, credits, banned_until, joined_at, is_admin FROM users WHERE tg_id=?",
        (tg_id,),
    )
    r = await c.fetchone()
    if not r:
        return None
    return {
        "tg_id": r[0],
        "username": r[1],
        "full_name": r[2],
        "credits": r[3],
        "banned_until": r[4],
        "joined_at": r[5],
        "is_admin": bool(r[6]),
    }

async def ensure_user(db, u: types.User):
    ex = await get_user(db, u.id)
    if ex:
        return ex
    joined = datetime.now(timezone.utc).isoformat()
    is_admin = 1 if u.id in ADMIN_USER_IDS else 0
    await db.execute(
        "INSERT INTO users(tg_id, username, full_name, credits, banned_until, joined_at, is_admin) VALUES(?,?,?,?,?,?,?)",
        (u.id, u.username or "", u.full_name, FREE_REG_CREDITS, None, joined, is_admin),
    )
    await db.commit()
    return await get_user(db, u.id)

async def add_credits(db, tg_id: int, amount: int):
    await db.execute("UPDATE users SET credits=COALESCE(credits,0)+? WHERE tg_id=?", (amount, tg_id))
    await db.commit()

async def deduct_credits(db, tg_id: int, amount: int):
    await db.execute(
        "UPDATE users SET credits=COALESCE(credits,0)-? WHERE tg_id=? AND credits>=?",
        (amount, tg_id, amount),
    )
    await db.commit()

async def set_ban(db, tg_id: int, until: Optional[datetime]):
    await db.execute(
        "UPDATE users SET banned_until=? WHERE tg_id=?",
        (until.isoformat() if until else None, tg_id),
    )
    await db.commit()

async def is_maintenance(db) -> bool:
    c = await db.execute("SELECT value FROM settings WHERE key='maintenance'")
    r = await c.fetchone()
    return (r and r[0] == "1")

async def set_maintenance(db, on: bool):
    await db.execute(
        "INSERT INTO settings(key,value) VALUES('maintenance',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("1" if on else "0",),
    )
    await db.commit()

# =====================
# UI: Keyboards
# =====================

# Start keyboard varies based on registration
#   - If not registered: only Register
#   - If registered: Commands + Close

def kb_start(registered: bool):
    b = InlineKeyboardBuilder()
    if not registered:
        b.button(text="📝 Register", callback_data="reg")
    else:
        b.button(text="🧭 Commands", callback_data="commands")
        b.button(text="✖️ Close", callback_data="close")
        b.adjust(2)
    return b.as_markup()

def kb_commands():
    b = InlineKeyboardBuilder()
    b.button(text="🛠️ Gates", callback_data="gate")
    b.button(text="💰 Credits", callback_data="credits")
    b.button(text="✖️ Close", callback_data="close")
    b.adjust(3)
    return b.as_markup()

def kb_gate():
    b = InlineKeyboardBuilder()
    b.button(text="⚡ CCN", callback_data="ccn")
    b.button(text="📦 MASS CCN", callback_data="mccn")
    b.button(text="⬅️ Back", callback_data="back_to_commands")
    b.adjust(2, 1)
    return b.as_markup()

def kb_back():
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data="back_to_commands")
    return b.as_markup()

def kb_contact_back():
    b = InlineKeyboardBuilder()
    url = f"https://t.me/{OWNER_USERNAME.lstrip('@')}" if OWNER_USERNAME else "https://t.me/"
    b.button(text="📨 Contact Owner", url=url)
    b.button(text="🏠 Back to Menu", callback_data="back_to_menu")
    b.adjust(1, 1)
    return b.as_markup()

# =====================
# Helpers
# =====================

def mention(user: types.User) -> str:
    name = user.full_name or "User"
    return f"<a href=\"tg://user?id={user.id}\">{name}</a>"

async def fetch_json(url: str):
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=60) as r:
            return await r.json(content_type=None)

async def fetch_text(url: str) -> str:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=30) as r:
            return await r.text()

async def edit_or_answer(message: types.Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await message.answer(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            pass

async def delete_if_processing(message: types.Message):
    if processing_users.get(message.from_user.id):
        try:
            await message.delete()
        except Exception:
            pass

async def bin_details(bin6: str) -> Dict[str, str]:
    try:
        if not BeautifulSoup:
            return {}
        html = await fetch_text(f"https://bincheck.io/details/{bin6}")
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr")
        res: Dict[str, str] = {}
        for row in rows:
            cols = row.find_all("td")
            if len(cols) == 2:
                res[cols[0].get_text(strip=True)] = cols[1].get_text(strip=True)
        # Post-process to normalize Country field
        if "Country" not in res:
            for k, v in list(res.items()):
                lk = k.lower().strip()
                if "country" in lk and ("name" in lk or "iso" in lk or k.strip() == "Country"):
                    country = (v or "").strip()
                    country = re.sub(r"\s+", " ", country)
                    res["Country"] = country
                    break
        return res
    except Exception:
        return {}

def format_bin_block(bin6: str, info: Dict[str, str]) -> str:
    brand = info.get("Card Brand", "N/A")
    ctype = info.get("Card Type", "N/A")
    lvl = info.get("Card Level", "N/A")
    bank = info.get("Issuer Name / Bank", "N/A")
    country = info.get("Country", "N/A")
    return (
        "\n━━━━━━━━━━━━━\n"
        "🔗 <b>BIN DETAILS</b>\n"
        f"• <b>Bin</b> ⌁ ({bin6})\n"
        f"• <b>Info</b> ⌁ {brand} - {ctype} - {lvl}\n"
        f"• <b>Bank</b> ⌁ {bank}\n"
        f"• <b>Country</b> ⌁ {country}\n"
        "━━━━━━━━━━━━━"
    )

cc_re = re.compile(r"^/ccn\s+([\d|]+)")
mass_re = re.compile(r"^/mccn\s+(.+)", re.S)

async def ensure_not_banned(db, user) -> Optional[str]:
    if user and user.get("banned_until"):
        try:
            until = datetime.fromisoformat(user["banned_until"]) if user["banned_until"] else None
            if not until:
                return None
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if until > now:
                left = until - now
                return f"⛔ You are banned. Time left: {str(left).split('.')[0]}"
        except Exception:
            return "⛔ You are banned."
    return None

async def ensure_not_maintenance(db, user_id: int, is_admin: bool) -> Optional[str]:
    if is_admin:
        return None
    if await is_maintenance(db):
        return "🛠️ Bot is under maintenance. Please try again later."
    return None

async def start_message_text(u: types.User, registered: bool, credits: Optional[int]) -> str:
    base = (
        "<b>XOXO CCN | Version - 1.5</b>\n"
        "━━━━━━━━━━━━━\n"
        f"Hello, <b>{u.first_name}</b>! How can I help you today? ✨\n\n"
        f"👤 <b>User ID</b> ⌁ <code>{u.id}</code>\n"
        "🤖 <b>BOT Status</b> ⌁ <b>UP</b> ✅\n\n"
    )
    if not registered:
        base += (
            "📝 <b>Registration Required</b>\n"
            "Tap <b>Register</b> to receive free credits! 🎁\n"
        )
    else:
        cred_text = "∞" if credits is None else str(credits)
        base += (
            f"💰 <b>Credits</b> ⌁ {cred_text}\n\n"
            "🔗 Explore: Use the buttons below to discover all features.\n"
        )
    return base

async def ccn_gate_info() -> str:
    return (
        "⚡ <b>CCN AUTH GATE</b>\n"
        "━━━━━━━━━━━━━\n"
        "• <b>What it does</b> ⌁ Validates if cards are live CCN.\n"
        "• <b>How to use</b> ⌁ Send: <code>/ccn cc|mm|yyyy|cvv</code>\n"
        "• <b>Status</b> ⌁ Active ✅\n"
        "━━━━━━━━━━━━━"
    )

async def mccn_gate_info() -> str:
    # Updated per user request
    return (
        "📦 <b>MASS CCN GATE</b>\n"
        "━━━━━━━━━━━━━\n"
        "• <b>What it does</b> ⌁ Mass CCN checking\n"
        "• <b>How to use</b> ⌁ Send: <code>/mccn</code> cards\n"
        "• <b>Limit</b> ⌁ Max 5\n"
        "• <b>Status</b> ⌁ Active ✅\n"
        "━━━━━━━━━━━━━"
    )

# =====================
# Validation & Classification
# =====================

def luhn_valid(card_number: str) -> bool:
    if not card_number.isdigit():
        return False
    if not (12 <= len(card_number) <= 19):  # Typical PAN lengths (Amex=15, Visa up to 19)
        return False
    total = 0
    reverse_digits = card_number[::-1]
    for i, d in enumerate(reverse_digits):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0

async def parse_cc(cc: str) -> Optional[str]:
    parts = re.split(r"[|]", cc.strip())
    if len(parts) != 4:
        return None
    c, m, y, cvv = parts
    c, m, y, cvv = c.strip(), m.strip(), y.strip(), cvv.strip()
    if not c.isdigit() or not m.isdigit() or not y.isdigit() or not cvv.isdigit():
        return None
    if not luhn_valid(c):
        return None
    mi = int(m)
    if mi < 1 or mi > 12:
        return None
    if len(y) == 2:
        y = "20" + y
    return f"{c}|{m}|{y}|{cvv}"

def classify_head(status: str, message: str) -> str:
    s = (status or "").lower()
    upper_msg = (message or "").upper()
    # Not supported / card type errors
    if any(k in upper_msg for k in [
        "error", "UNSUPPORTED", "CARD TYPE NOT SUPPORTED", "CARD NOT SUPPORTED", "ONLY VISA", "ONLY MASTERCARD"
    ]):
        return "⚠️ <b>Error</b>"
    # 3DS / OTP detection
    if any(k in upper_msg for k in ["3DS", "3D", "OTP", "ONE TIME PASSWORD", "REDIRECT", "3-D"]):
        return "⚠️ <b>3D Card</b>"
    # Approved-ish outcomes
    success_keywords = [
        "SUCCESS", "charged", "CHARGED", "AUTHORIZED", "AUTHORISED",
        "CAPTURED", "CHARGED", "PAYMENT SUCCESS", "TRANSACTION APPROVED",
        "live ccn", "live", "LIVE", "VALID"
    ]
    if s in ("charged") or any(k in upper_msg for k in success_keywords):
        return "🔥 <b>CHARGED</b>"
    if "live" in upper_msg:
        return "✅ <b>LIVE CCN</b>"
    # Default
    return "❌ <b>DECLINED</b>"

async def animate_processing(bot: Bot, chat_id: int, message_id: int, base: str, stop: asyncio.Event):
    dots = [".", "..", "..."]
    i = 0
    while not stop.is_set():
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"{base}\n🔄 Processing{dots[i % 3]}", parse_mode=ParseMode.HTML)
        except Exception:
            # Ignore edit conflicts or race conditions while animating
            pass
        i += 1
        await asyncio.sleep(0.6)

# =====================
# NEW: Refresh Checking Command
# =====================
async def cmd_refresh_checking(message: types.Message, state: FSMContext, db):
    """Reset processing state for user - accessible to all users"""
    user_id = message.from_user.id
    
    # Clear processing state
    processing_users.pop(user_id, None)
    
    # Clear FSM state
    try:
        await state.clear()
    except Exception:
        pass
    
    # Delete the command message
    try:
        await message.delete()
    except Exception:
        pass
    
    # Show main menu
    existing = await get_user(db, user_id)
    registered = bool(existing)
    credits = None if (existing and existing.get("is_admin")) else (existing.get("credits", 0) if existing else None)
    
    await message.answer(
        await start_message_text(message.from_user, registered=registered, credits=credits),
        reply_markup=kb_start(registered=registered),
        parse_mode=ParseMode.HTML,
    )

# =====================
# Gates
# =====================
async def do_ccn(message: types.Message, state: FSMContext, db, bot: Bot):
    # Unregistered users: clear and show menu with Register only
    existing = await get_user(db, message.from_user.id)
    if not existing:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            await start_message_text(message.from_user, registered=False, credits=None),
            reply_markup=kb_start(registered=False),
            parse_mode=ParseMode.HTML,
        )
        return

    if await is_maintenance(db) and message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("🛠️ Bot is under maintenance. Please try again later.")
        return

    if (await state.get_state()) != Flow.in_gate_ccn.state:
        try:
            await message.delete()
        except Exception:
            pass
        # Replace with menu
        registered = True
        credits = None if existing.get("is_admin") else existing.get("credits", 0)
        await message.answer(
            await start_message_text(message.from_user, registered=registered, credits=credits),
            reply_markup=kb_start(registered=registered),
            parse_mode=ParseMode.HTML,
        )
        return

    m = cc_re.match(message.text or "")
    if not m:
        try:
            await message.delete()
        except Exception:
            pass
        return

    # Ban check
    ban_msg = await ensure_not_banned(db, existing)
    if ban_msg:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(ban_msg)
        return

    user = existing
    if not user.get("is_admin") and user.get("credits", 0) < 1:
        await insufficient(message)
        return

    if processing_users.get(message.from_user.id):
        try:
            await message.delete()
        except Exception:
            pass
        return

    processing_users[message.from_user.id] = True
    full = await parse_cc(m.group(1))
    if not full:
        processing_users.pop(message.from_user.id, None)
        try:
            await message.delete()
        except Exception:
            pass
        return

    cnum = full.split("|")[0]
    bin6 = cnum[:6]
    info = await bin_details(bin6)

    base = f"💳 <code>{full}</code>" + format_bin_block(bin6, info)
    msg = await message.answer(base, parse_mode=ParseMode.HTML)
    stop = asyncio.Event()
    _ = asyncio.create_task(animate_processing(bot, message.chat.id, msg.message_id, base, stop))

    try:
        res = await fetch_json(BASE_CC_API + full)
        if isinstance(res, list) and res:
            r = res[0]
            status = r.get("status", "")
            emsg = r.get("message", "Result")
            head = classify_head(status, emsg)
            text = (
                f"{head}\n"
                "━━━━━━━━━━━━━\n"
                f"💳 <code>{full}</code>\n"
                f"╰┈➤ <b>{emsg}</b>"
                + format_bin_block(bin6, info)
                + f"\n🆔 <b>Checked by:</b> {mention(message.from_user)}"
            )
            # Stop animation before final edit to avoid race
            stop.set()
            await asyncio.sleep(0.2)
            try:
                await bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=text, parse_mode=ParseMode.HTML)
            except Exception:
                # Fallback: send as a new message so user never gets stuck
                await message.answer(text, parse_mode=ParseMode.HTML)
            if CHECK_RESULTS_CHANNEL_ID:
                try:
                    await bot.send_message(CHECK_RESULTS_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            if not user.get("is_admin"):
                await deduct_credits(db, message.from_user.id, 1)
        else:
            # Unknown response; stop animation and notify
            stop.set()
            await asyncio.sleep(0.2)
            fallback = base + "\n<b>Unable to process the card at the moment.</b>"
            try:
                await bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=fallback, parse_mode=ParseMode.HTML)
            except Exception:
                await message.answer(fallback, parse_mode=ParseMode.HTML)
    finally:
        stop.set()
        await asyncio.sleep(0.1)
        processing_users.pop(message.from_user.id, None)
        await message.answer(await ccn_gate_info(), reply_markup=kb_back(), parse_mode=ParseMode.HTML)

async def do_mccn(message: types.Message, state: FSMContext, db, bot: Bot):
    existing = await get_user(db, message.from_user.id)
    if not existing:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            await start_message_text(message.from_user, registered=False, credits=None),
            reply_markup=kb_start(registered=False),
            parse_mode=ParseMode.HTML,
        )
        return

    if await is_maintenance(db) and message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("🛠️ Bot is under maintenance. Please try again later.")
        return

    if (await state.get_state()) != Flow.in_gate_mccn.state:
        try:
            await message.delete()
        except Exception:
            pass
        registered = True
        credits = None if existing.get("is_admin") else existing.get("credits", 0)
        await message.answer(
            await start_message_text(message.from_user, registered=registered, credits=credits),
            reply_markup=kb_start(registered=registered),
            parse_mode=ParseMode.HTML,
        )
        return

    m = mass_re.match(message.text or "")
    if not m:
        try:
            await message.delete()
        except Exception:
            pass
        return

    # Ban check
    ban_msg = await ensure_not_banned(db, existing)
    if ban_msg:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(ban_msg)
        return

    if processing_users.get(message.from_user.id):
        try:
            await message.delete()
        except Exception:
            pass
        return

    user = existing
    credits = 9999 if user.get("is_admin") else user.get("credits", 0)
    raw = m.group(1).replace("\n", " ").split()

    cards: List[str] = []
    for s in raw:
        p = await parse_cc(s)
        if p:
            cards.append(p)
        if len(cards) >= 5:
            break

    # Enforce 2-5 valid cards input; if less than 2, delete user's message
    if len(cards) < 2:
        try:
            await message.delete()
        except Exception:
            pass
        return

    can = min(len(cards), credits)
    if can == 0:
        await insufficient(message)
        return
    cards = cards[:can]

    processing_users[message.from_user.id] = True

    # BIN info (unique)
#    uniq_bins: Dict[str, Dict[str, str]] = {}
#    for c in cards:
#        b6 = c.split('|')[0][:6]
#        if b6 not in uniq_bins:
#            uniq_bins[b6] = await bin_details(b6)

    base = "\n".join([f"💳 <code>{c}</code>" for c in cards])
#    for b, info in uniq_bins.items():
#        base += format_bin_block(b, info)

    msg = await message.answer(base, parse_mode=ParseMode.HTML)
    stop = asyncio.Event()
    _ = asyncio.create_task(animate_processing(bot, message.chat.id, msg.message_id, base, stop))

    try:
        out: List[str] = []
        for c in cards:
            try:
                res = await fetch_json(BASE_CC_API + c)
            except Exception:
                res = None
            if isinstance(res, list) and res:
                r = res[0]
                emsg = r.get("message", "Result")
                status = r.get("status", "")
                head = classify_head(status, emsg)
                out.append(
                    f"{head}\n💳 <code>{c}</code>\n╰┈➤ <b>{emsg}</b>\n━━━━━━━━━━━━━"
                )
                if not user.get("is_admin"):
                    await deduct_credits(db, message.from_user.id, 1)
            else:
                out.append(f"❌ <b>Declined</b>\n💳 <code>{c}</code>\n╰┈➤ <b>Unable to process</b>\n━━━━━━━━━━━━━")
        final = "\n".join(out) + f"\n🆔 <b>Checked by:</b> {mention(message.from_user)}"
        # Stop animation before final edit to avoid race
        stop.set()
        await asyncio.sleep(0.2)
        try:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text=final, parse_mode=ParseMode.HTML)
        except Exception:
            await message.answer(final, parse_mode=ParseMode.HTML)
        if CHECK_RESULTS_CHANNEL_ID:
            try:
                await bot.send_message(CHECK_RESULTS_CHANNEL_ID, final, parse_mode=ParseMode.HTML)
            except Exception:
                pass
    finally:
        stop.set()
        await asyncio.sleep(0.1)
        processing_users.pop(message.from_user.id, None)
        await message.answer(await mccn_gate_info(), reply_markup=kb_back(), parse_mode=ParseMode.HTML)

# Delete stray messages in gate states
async def delete_other(message: types.Message):
    try:
        await message.delete()
    except Exception:
        pass

# =====================
# Admin commands
# =====================
async def admin_only(message: types.Message) -> bool:
    return message.from_user.id in ADMIN_USER_IDS

async def cmd_add_credits(message: types.Message, db, bot: Bot):
    if not await admin_only(message):
        return
    try:
        _, uid, amt = message.text.split()
        uid_i = int(uid)
        amt_i = int(amt)
        await add_credits(db, uid_i, amt_i)
        user = await get_user(db, uid_i)
        username = f"@{user['username']}" if user and user.get("username") else "-"
        credits = user.get("credits") if user else "?"
        today = datetime.now(timezone.utc).date()
        admin_txt = (
            "✨ <b>Credits Updated</b>\n"
            "━━━━━━━━━━━━━\n"
            f"👤 User: <a href=\"tg://user?id={uid_i}\">{uid_i}</a> ({username})\n"
            f"➕ Added: <b>{amt_i}</b>\n"
            f"💰 Balance: <b>{credits}</b>\n"
            f"📅 Date: {today}\n"
        )
        await message.answer(admin_txt, parse_mode=ParseMode.HTML)
        try:
            user_txt = (
                "💳 <b>Credits Added</b>\n"
                "━━━━━━━━━━━━━\n"
                f"➕ Amount: <b>{amt_i}</b>\n"
                f"💰 New Balance: <b>{credits}</b>\n"
                f"📅 Date: {today}\n"
                "Thank you for using the bot! ✨"
            )
            await bot.send_message(uid_i, user_txt, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    except Exception:
        await message.answer("Usage: /addusercredits <user_id> <amount>")

async def cmd_deduct_credits(message: types.Message, db, bot: Bot):
    if not await admin_only(message):
        return
    try:
        _, uid, amt = message.text.split()
        uid_i = int(uid)
        amt_i = int(amt)
        await deduct_credits(db, uid_i, amt_i)
        user = await get_user(db, uid_i)
        username = f"@{user['username']}" if user and user.get("username") else "-"
        credits = user.get("credits") if user else "?"
        today = datetime.now(timezone.utc).date()
        admin_txt = (
            "⚠️ <b>Credits Deducted</b>\n"
            "━━━━━━━━━━━━━\n"
            f"👤 User: <a href=\"tg://user?id={uid_i}\">{uid_i}</a> ({username})\n"
            f"➖ Deducted: <b>{amt_i}</b>\n"
            f"💰 Balance: <b>{credits}</b>\n"
            f"📅 Date: {today}\n"
        )
        await message.answer(admin_txt, parse_mode=ParseMode.HTML)
        try:
            user_txt = (
                "⚠️ <b>Credits Deducted</b>\n"
                "━━━━━━━━━━━━━\n"
                f"➖ Amount: <b>{amt_i}</b>\n"
                f"💰 New Balance: <b>{credits}</b>\n"
                f"📅 Date: {today}\n"
            )
            await bot.send_message(uid_i, user_txt, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    except Exception:
        await message.answer("Usage: /deductusercredit <user_id> <amount>")

async def cmd_ban(message: types.Message, db):
    if not await admin_only(message):
        return
    try:
        _, uid, duration = message.text.split()
        base = datetime.now(timezone.utc)
        if duration.endswith("h"):
            until = base + timedelta(hours=int(re.sub(r"\D", "", duration) or 1))
        elif duration.endswith("d") or duration.endswith("day"):
            until = base + timedelta(days=int(re.sub(r"\D", "", duration) or 1))
        else:
            until = datetime.max.replace(tzinfo=timezone.utc)
        await set_ban(db, int(uid), until)
        until_text = "∞" if until.year >= 9999 else until.strftime("%Y-%m-%d %H:%M")
        await message.answer(f"🔒 User <a href=\"tg://user?id={uid}\">{uid}</a> banned until {until_text}", parse_mode=ParseMode.HTML)
    except Exception:
        await message.answer("Usage: /banuseraccess <user_id> <1h|1d|2day|unlimited>")

async def cmd_unban(message: types.Message, db):
    if not await admin_only(message):
        return
    try:
        _, uid = message.text.split()
        await set_ban(db, int(uid), None)
        url = f"tg://user?id={uid}"
        await message.answer(f"✅ <a href=\"{url}\">User</a> unbanned.", parse_mode=ParseMode.HTML)
    except Exception:
        await message.answer("Usage: /unbanuseraccess <user_id>")

async def cmd_show_users(message: types.Message, db):
    if not await admin_only(message):
        return
    c = await db.execute("SELECT tg_id, username, credits, joined_at FROM users ORDER BY joined_at DESC LIMIT 200")
    rows = await c.fetchall()
    header = "👥 <b>User List</b>\n━━━━━━━━━━━━━\n"
    lines = []
    for idx, r in enumerate(rows, 1):
        uid, uname, credits, joined = r
        handle = f"@{uname}" if uname else "-"
        joined_date = (joined[:10] if joined else "")
        lines.append(f"{idx:>2}. <code>{uid}</code> | {handle} | 💰 {credits} | 📅 {joined_date}")
    text = header + "\n".join(lines)
    await message.answer(text[:4000], parse_mode=ParseMode.HTML)

async def cmd_freeze(message: types.Message, db):
    if not await admin_only(message):
        return
    await set_maintenance(db, True)
    await message.answer("🛠️ Bot usage frozen for maintenance.")

async def cmd_unfreeze(message: types.Message, db):
    if not await admin_only(message):
        return
    await set_maintenance(db, False)
    await message.answer("✅ Bot is live again.")

async def cmd_broadcast(message: types.Message, db, bot: Bot):
    if not await admin_only(message):
        return
    try:
        content = message.text.split(" ", 1)[1]
    except Exception:
        await message.answer("Usage: /broadcastmessage <text> (use \\n for new lines)")
        return
    content = content.replace("\\n", "\n")
    c = await db.execute("SELECT tg_id FROM users")
    rows = await c.fetchall()
    sent = 0
    for (uid,) in rows:
        try:
            await bot.send_message(uid, content)
        except Exception:
            pass
        sent += 1
        await asyncio.sleep(0.03)
    await message.answer(f"Broadcast sent to {sent} users")

# =====================
# Handlers: Start & Callbacks
# =====================

async def insufficient(message: types.Message):
    try:
        await message.answer("⚠️ <b>Insufficient credits.</b> \n<b>Please contact the owner to top up.</b>", reply_markup=kb_contact_back())
    except Exception:
        pass

async def on_start(message: types.Message, state: FSMContext, db, bot: Bot):
    if processing_users.get(message.from_user.id):
        try:
            await message.delete()
        except Exception:
            pass
        return
    try:
        await state.clear()
    except Exception:
        pass
    existing = await get_user(db, message.from_user.id)
    registered = bool(existing)
    credits = None if (existing and existing.get("is_admin")) else (existing.get("credits", 0) if existing else None)
    await message.answer(
        await start_message_text(message.from_user, registered=registered, credits=credits),
        reply_markup=kb_start(registered=registered),
        parse_mode=ParseMode.HTML,
    )

async def cb_close(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Closed", show_alert=False)

async def cb_back_menu(callback: types.CallbackQuery, state: FSMContext, db):
    try:
        await state.clear()
    except Exception:
        pass
    existing = await get_user(db, callback.from_user.id)
    registered = bool(existing)
    credits = None if (existing and existing.get("is_admin")) else (existing.get("credits", 0) if existing else None)
    await edit_or_answer(callback.message, await start_message_text(callback.from_user, registered=registered, credits=credits), reply_markup=kb_start(registered=registered))
    await callback.answer()

async def cb_reg(callback: types.CallbackQuery, state: FSMContext, db, bot: Bot):
    existing = await get_user(db, callback.from_user.id)
    if existing:
        await callback.answer("You are already registered.", show_alert=True)
    else:
        u = await ensure_user(db, callback.from_user)
        await edit_or_answer(callback.message, "🎉 <b>Registration successful!</b> \nFree credits added to your account.", reply_markup=kb_start(registered=True))
        if NEW_USER_CHANNEL_ID:
            try:
                uid = u["tg_id"]
                handle = f"@{u['username']}" if u.get("username") else "-"
                joined = (u.get("joined_at") or "")[:10]
                credits = u.get("credits", 0)
                text = (
                    "🆕 <b>New User Registered</b>\n"
                    "━━━━━━━━━━━━━\n"
                    f"🆔 User ID: <code>{uid}</code>\n"
                    f"👤 Name: {mention(callback.from_user)}\n"
                    f"💬 Username: {handle}\n"
                    f"📅 Join Date: {joined}\n"
                    f"💰 Credits: <b>{credits}</b>\n"
                )
                await bot.send_message(NEW_USER_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        await callback.answer()

async def cb_commands(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(Flow.in_commands)
    await edit_or_answer(callback.message, "<b>━━━━━━━━━━━━━\n🧭 Available sections: Gates | Credits\n━━━━━━━━━━━━━</b>", reply_markup=kb_commands())
    await callback.answer()

async def cb_gate(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(Flow.in_commands)
    await edit_or_answer(callback.message, "<b>━━━━━━━━━━━━━\n💳 Choose a gate: Single | Mass\n━━━━━━━━━━━━━</b>", reply_markup=kb_gate())
    await callback.answer()

async def cb_credits(callback: types.CallbackQuery, db):
    existing = await get_user(db, callback.from_user.id)
    credits = "∞" if (existing and existing.get("is_admin")) else str(existing.get("credits", 0) if existing else 0)
    await edit_or_answer(callback.message, f"💰 <b>Credits</b> ⌁ {credits}", reply_markup=kb_back())
    await callback.answer()

async def cb_ccn(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(Flow.in_gate_ccn)
    await edit_or_answer(callback.message, await ccn_gate_info(), reply_markup=kb_back())
    await callback.answer()

async def cb_mccn(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(Flow.in_gate_mccn)
    await edit_or_answer(callback.message, await mccn_gate_info(), reply_markup=kb_back())
    await callback.answer()

# =====================
# App wiring
# =====================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env missing")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    db = await open_db()

    # Message: /start
    dp.message.register(partial(on_start, db=db, bot=bot), CommandStart())

    # NEW: /refreshchecking command - accessible to all users
    dp.message.register(partial(cmd_refresh_checking, db=db), F.text == "/refreshchecking")

    # Callback queries
    dp.callback_query.register(cb_close, F.data == "close")
    dp.callback_query.register(partial(cb_back_menu, db=db), F.data == "back_to_menu")
    dp.callback_query.register(partial(cb_reg, db=db, bot=bot), F.data == "reg")
    dp.callback_query.register(cb_commands, F.data == "commands")
    dp.callback_query.register(cb_gate, F.data == "gate")
    dp.callback_query.register(partial(cb_credits, db=db), F.data == "credits")
    dp.callback_query.register(cb_ccn, F.data == "ccn")
    dp.callback_query.register(cb_mccn, F.data == "mccn")
    dp.callback_query.register(cb_commands, F.data == "back_to_commands")

    # Gates
    dp.message.register(partial(do_ccn, db=db, bot=bot), Flow.in_gate_ccn, F.text.startswith("/ccn"))
    dp.message.register(partial(do_mccn, db=db, bot=bot), Flow.in_gate_mccn, F.text.startswith("/mccn"))
    dp.message.register(delete_other, Flow.in_gate_ccn)
    dp.message.register(delete_other, Flow.in_gate_mccn)

    # Admin
    dp.message.register(partial(cmd_add_credits, db=db, bot=bot), F.text.startswith("/addusercredits"))
    dp.message.register(partial(cmd_deduct_credits, db=db, bot=bot), F.text.startswith("/deductusercredit"))
    dp.message.register(partial(cmd_ban, db=db), F.text.startswith("/banuseraccess"))
    dp.message.register(partial(cmd_unban, db=db), F.text.startswith("/unbanuseraccess"))
    dp.message.register(partial(cmd_show_users, db=db), F.text.startswith("/showuserlist"))
    dp.message.register(partial(cmd_freeze, db=db), F.text.startswith("/freezebotusage"))
    dp.message.register(partial(cmd_unfreeze, db=db), F.text.startswith("/unfreezebotusage"))
    dp.message.register(partial(cmd_broadcast, db=db, bot=bot), F.text.startswith("/broadcastmessage"))

    # Message: guard during processing (register last so it doesn't block other handlers)
    dp.message.register(delete_if_processing)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
