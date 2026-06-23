"""
Bot Factory — مصنع بوتات الحماية
Manages protection sub-bots as separate subprocess instances.
"""
import os
import sys
import json
import re
import logging
import subprocess
import httpx
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, ChatMember,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler, filters, ContextTypes,
)
from telegram.error import TelegramError

logging.basicConfig(
    format="%(asctime)s [factory] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(name)

# ── Constants ──────────────────────────────────────────────────────────────────
OWNER_ID   = int(os.environ.get("OWNER_ID", "0"))
DATA_FILE  = Path(file).parent / "factory_data.json"
BOTS_DIR   = Path(file).parent / "bots_data"
BOT_SCRIPT = Path(file).parent / "protection_bot.py"
BOTS_DIR.mkdir(exist_ok=True)

PLANS = {
    "free":  {"name": "مجاني 🆓",   "bots": 1,  "stars": 0},
    "basic": {"name": "أساسي ⭐",   "bots": 3,  "stars": 50},
    "pro":   {"name": "احترافي 💎", "bots": 10, "stars": 200},
}

# ── In-memory process registry ────────────────────────────────────────────────
_procs: dict[str, subprocess.Popen] = {}

# ══════════════════════════════════════════════════════════════════════════════
# DATA HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def load() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {}

def save(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_user(data: dict, uid: str) -> dict:
    if uid not in data:
        data[uid] = {"plan": "free", "bots": {}}
    if "bots" not in data[uid]:
        data[uid]["bots"] = {}
    return data[uid]

def get_config(data: dict) -> dict:
    if "_config" not in data:
        data["_config"] = {
            "dev_username":  "",
            "dev_telegram":  str(OWNER_ID),
            "contact_msg":   "للتواصل مع المطور: اضغط الزر أدناه 👇",
            "dev_channels":  [],   # [{"title": "...", "username": "@..."}]
        }
    return data["_config"]

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

def proc_key(uid: str, bot_name: str) -> str:
    return f"{uid}::{bot_name}"

def is_running(uid: str, bot_name: str) -> bool:
    proc = _procs.get(proc_key(uid, bot_name))
    return proc is not None and proc.poll() is None

def data_file_for(uid: str, bot_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", f"{uid}_{bot_name}")
    return str(BOTS_DIR / f"{safe}.json")

def write_factory_config(uid: str, name: str, channels: list, custom_buttons: list) -> None:
    df   = Path(data_file_for(uid, name))
    blob = json.loads(df.read_text(encoding="utf-8")) if df.exists() else {}
    blob["_factory_config"] = {"channels": channels, "custom_buttons": custom_buttons}
    df.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")

def get_bot_field(user: dict, name: str, field: str, default=None):
    return user["bots"].get(name, {}).get(field, default)

def mask_token(token: str) -> str:
    if len(token) < 20:
        return token
    return token[:8] + "***" + token[-6:]

# ══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTION GATE
# ══════════════════════════════════════════════════════════════════════════════

async def check_subscription(bot, user_id: int, channels: list) -> tuple[bool, list]:
    """
    Returns (all_ok, list_of_unsubscribed_channel_dicts).
    Calls Telegram getChatMember for each configured channel.
    If the bot can't reach a channel, it assumes the user is subscribed

(so a mis-configured private channel doesn't lock everyone out).
    """
    unsubscribed = []
    for ch in channels:
        username = ch.get("username", "").strip()
        if not username:
            continue
        try:
            member = await bot.get_chat_member(chat_id=username, user_id=user_id)
            if member.status in (
                ChatMember.LEFT, ChatMember.BANNED,
                "left", "kicked", "restricted",
            ):
                unsubscribed.append(ch)
        except TelegramError:
            # Can't check → skip (don't block user)
            pass
    return len(unsubscribed) == 0, unsubscribed

def sub_required_kb(channels: list) -> InlineKeyboardMarkup:
    """Keyboard shown when user has not subscribed yet."""
    rows = []
    for ch in channels:
        uname = ch.get("username", "")
        title = ch.get("title", uname)
        if uname:
            rows.append([InlineKeyboardButton(
                f"📢 اشترك في {title}",
                url=f"https://t.me/{uname.lstrip('@')}"
            )])
    rows.append([InlineKeyboardButton("✅ تحققت من الاشتراك", callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)

# Callbacks that bypass subscription check (gate would otherwise loop)
_NO_SUB_CHECK = frozenset({"check_sub", "noop"})

async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Check mandatory channel subscription.
    Returns True if user may proceed, False if blocked (message already sent/edited).
    Owner is always allowed through.
    """
    user_id = update.effective_user.id
    if is_owner(user_id):
        return True

    data     = load()
    cfg      = get_config(data)
    channels = cfg.get("dev_channels", [])
    if not channels:
        return True

    ok, unsub = await check_subscription(ctx.bot, user_id, channels)
    if ok:
        return True

    msg = (
        "⛔ *عذراً، يجب عليك الاشتراك في قنواتنا أولاً لاستخدام البوت.*\n\n"
        "اشترك ثم اضغط ✅ تحققت من الاشتراك."
    )
    kb = sub_required_kb(unsub)

    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
        except TelegramError:
            await q.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    return False

# ══════════════════════════════════════════════════════════════════════════════
# SUBPROCESS MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def start_proc(uid: str, bot_name: str, token: str) -> None:
    key = proc_key(uid, bot_name)
    if is_running(uid, bot_name):
        return
    proc = subprocess.Popen(
        [sys.executable, str(BOT_SCRIPT),
         "--token",    token,
         "--data-file", data_file_for(uid, bot_name),
         "--name",     bot_name,
         "--owner-id", uid],          # sub-bot owner = factory user
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _procs[key] = proc
    logger.info(f"Started «{bot_name}» uid={uid} pid={proc.pid}")

def stop_proc(uid: str, bot_name: str) -> None:
    key  = proc_key(uid, bot_name)
    proc = _procs.pop(key, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    logger.info(f"Stopped «{bot_name}» uid={uid}")

def restore_running_bots() -> None:
    for uid, user in load().items():
        if uid.startswith("_"):
            continue
        for name, info in user.get("bots", {}).items():
            if info.get("running") and info.get("token"):
                try:
                    start_proc(uid, name, info["token"])
                except Exception as e:
                    logger.error(f"Restore failed «{name}»: {e}")

async def verify_token(token: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        if r.status_code == 200 and r.json().get("ok"):
            return r.json()["result"]
    except Exception:
        pass
    return None

# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════════════════

def main_kb(user_id: int, cfg: dict | None = None) -> InlineKeyboardMarkup:
    """Main menu — updated layout as per requirements."""
    cfg = cfg or {}
    # Determine "قناة البوت" URL
    dev_channels = cfg.get("dev_channels", [])
    ch_url = None
    if dev_channels:
        uname = dev_channels[0].get("username", "")
        if uname:
            ch_url = f"https://t.me/{uname.lstrip('@')}"

    rows = [
        [InlineKeyboardButton("➕ إنشاء بوت",          callback_data="create_bot"),
         InlineKeyboardButton("⚙️ قائمة بوتاتي",      callback_data="my_bots")],
        [InlineKeyboardButton("📞 تواصل مع المطور",    callback_data="contact")],
    ]
    if ch_url:
        rows.append([InlineKeyboardButton("📢 قناة البوت", url=ch_url)])
    else:
        rows.append([InlineKeyboardButton("📢 قناة البوت", callback_data="our_channels")])

    if is_owner(user_id):
        rows.append([InlineKeyboardButton("⚙️ لوحة المطور 👑", callback_data="dev_main")])
    return InlineKeyboardMarkup(rows)

def my_bots_kb(user: dict, uid: str) -> InlineKeyboardMarkup:
    rows = []
    for name in user["bots"]:
        status = "🟢" if is_running(uid, name) else "🔴"
        rows.append([
            InlineKeyboardButton(f"{status} {name}", callback_data=f"bot_panel::{name}"),
            InlineKeyboardButton("⚙️ إعدادات",        callback_data=f"bot_panel::{name}"),
            InlineKeyboardButton("🗑 حذف",             callback_data=f"delete_confirm::{name}"),
        ])
    rows += [
        [InlineKeyboardButton("➕ إنشاء بوت جديد",    callback_data="create_bot")],
        [InlineKeyboardButton("🔙 رجوع",               callback_data="main")],
    ]
    retur
