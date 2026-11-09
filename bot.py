"""ALI AI - multimodal Telegram assistant (text, vision, voice, documents, image generation).
Created by ZEEYO.
"""
import asyncio
import html
import logging
import os
import re
import sqlite3
import uuid
from types import SimpleNamespace

from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

import animation
from animation import Thinking, safe_edit
from formatting import chunk_markdown, md_to_html

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ali_ai")


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DEFAULT_MODEL = "ali-3.5-flash-lite"
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "2048"))
DB_PATH = os.getenv("DB_PATH", "ali.db")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}
STREAM_EDIT_INTERVAL = 1.2  # seconds between live-stream edits (Telegram flood limits)
RESPONSE_TIMEOUT = 120  # seconds per model attempt
CURSOR = " ▌"
STREAM_TAIL = 3800  # live preview shows only the last N chars (Telegram cap is 4096)

MODEL_MAP = {
    "ali-3.5-flash-lite": ["gemini-3.5-flash-lite", "gemini-2.5-flash-lite", "gemini-2.5-flash"],
    "ali-3.5-flash": ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-3.6-flash"],
    "ali-2.5-flash": ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-flash-lite"],
    "ali-1.5-flash": ["gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash"],
}
AVAILABLE_MODELS = list(MODEL_MAP.keys())

LANGUAGES = {
    "auto": "Auto-detect (as you type)",
    "uz": "O'zbekcha",
    "en": "English",
}

client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        log.exception("Failed to initialize AI client")

SYSTEM_PROMPT = (
    "You are ALI AI, a powerful, highly intelligent, and futuristic multimodal AI assistant created by ZEEYO. "
    "You operate inside Telegram. Answer clearly, accurately, and concisely. "
    "Use light Markdown only: **bold**, `inline code`, bullet lists and fenced code blocks. "
    "Do not use tables or LaTeX. Be honest when unsure instead of guessing."
)

locks: dict[int, asyncio.Lock] = {}

# ---------- Database ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, "
    "model TEXT DEFAULT 'ali-3.5-flash-lite', language TEXT DEFAULT 'auto')"
)
for _col, _ddl in (
    ("language", "ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'auto'"),
    ("model", "ALTER TABLE users ADD COLUMN model TEXT DEFAULT 'ali-3.5-flash-lite'"),
):
    try:
        db.execute(_ddl)
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

db.execute(
    "CREATE TABLE IF NOT EXISTS messages ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, content TEXT)"
)
db.execute("CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id)")
db.commit()


def get_user_model(uid: int) -> str:
    row = db.execute("SELECT model FROM users WHERE user_id=?", (uid,)).fetchone()
    return row[0] if row and row[0] in MODEL_MAP else DEFAULT_MODEL


def set_user_model(uid: int, model: str) -> None:
    if model not in MODEL_MAP:
        model = DEFAULT_MODEL
    db.execute(
        "INSERT INTO users(user_id, model) VALUES(?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET model=excluded.model",
        (uid, model),
    )
    db.commit()


def get_user_lang(uid: int) -> str:
    row = db.execute("SELECT language FROM users WHERE user_id=?", (uid,)).fetchone()
    return row[0] if row and row[0] in LANGUAGES else "auto"


def set_user_lang(uid: int, lang: str) -> None:
    if lang not in LANGUAGES:
        lang = "auto"
    db.execute(
        "INSERT INTO users(user_id, language) VALUES(?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET language=excluded.language",
        (uid, lang),
    )
    db.commit()


def load_history(uid: int) -> list[types.Content]:
    rows = db.execute(
        "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (uid, HISTORY_LIMIT),
    ).fetchall()[::-1]
    return [
        types.Content(role="user" if role == "user" else "model", parts=[types.Part(text=content)])
        for role, content in rows
    ]


def save_turn(uid: int, user_text: str, reply: str) -> None:
    db.executemany(
        "INSERT INTO messages(user_id, role, content) VALUES(?,?,?)",
        [(uid, "user", user_text), (uid, "model", reply)],
    )
    db.commit()


def clear_history(uid: int) -> None:
    db.execute("DELETE FROM messages WHERE user_id=?", (uid,))
    db.commit()


def get_message_count(uid: int) -> int:
    row = db.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (uid,)).fetchone()
    return row[0] if row else 0


# ---------- Helpers ----------
def authorized(update: Update) -> bool:
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat or chat.type not in ("private", "group", "supergroup"):
        return False
    return not ALLOWED or user.id in ALLOWED


def addressed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """In groups, only react when mentioned or replied to; always react in private chats."""
    if update.effective_chat.type == "private":
        return True
    msg = update.effective_message
    text = (msg.text or msg.caption or "").lower()
    uname = (ctx.bot.username or "").lower()
    if uname and f"@{uname}" in text:
        return True
    reply = msg.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.id == ctx.bot.id)


def strip_mention(text: str, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    uname = ctx.bot.username or ""
    return re.sub(rf"@{re.escape(uname)}\b", "", text, flags=re.I).strip() if uname else text


def lang_instruction(uid: int) -> str:
    lang = get_user_lang(uid)
    if lang == "auto":
        return " Reply in the exact same language the user writes in."
    return f" Always reply in {'Uzbek' if lang == 'uz' else 'English'}."


def friendly_error(err: Exception | None) -> str:
    """User-facing error text. Never exposes provider names or raw error strings (those go to the log)."""
    low = str(err or "").lower()
    if "429" in low or "resource_exhausted" in low or "quota" in low:
        return "⚠️ <b>I'm a bit overloaded right now.</b> Please try again in a minute."
    if isinstance(err, TimeoutError) or "timed out" in low or "timeout" in low:
        return "⚠️ <b>That took too long.</b> Please try again."
    if "safety" in low or "blocked" in low:
        return "⚠️ I can't help with that request."
    return "⚠️ <b>Something went wrong.</b> Please try again."


NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_final(status_msg, update: Update, text: str, header: str = "") -> None:
    """Replace the animated status message with the formatted answer (chunked if long)."""
    for i, chunk in enumerate(chunk_markdown(text)):
        rendered = md_to_html(chunk)
        if i == 0 and header:
            rendered = f"<b>{html.escape(header)}</b>\n\n{rendered}"
            chunk = f"{header}\n\n{chunk}"
        try:
            if i == 0:
                await status_msg.edit_text(
                    rendered, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW
                )
            else:
                await update.effective_message.reply_text(
                    rendered, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW
                )
        except BadRequest as e:
            if "not modified" in str(e).lower():
                continue
            log.info("HTML render rejected (%s); falling back to plain text", e)
            if i == 0:
                await safe_edit(status_msg, chunk)
            else:
                await update.effective_message.reply_text(chunk)


# ---------- Core AI response (streaming + animation) ----------
async def respond(
    update: Update,
    uid: int,
    parts: list,
    stored_query: str,
    status_msg,
    phrases: list[str] | None = None,
    use_history: bool = True,
    save: bool = True,
    header: str = "",
    extra_system: str = "",
) -> None:
    """Animate while thinking, live-stream the answer, then render the final formatted reply."""
    if not client:
        await safe_edit(status_msg, "AI client is not initialized. Check your API key.")
        return

    candidates = MODEL_MAP.get(get_user_model(uid), MODEL_MAP[DEFAULT_MODEL])
    contents = (load_history(uid) if use_history else []) + [
        types.Content(
            role="user",
            parts=[p if isinstance(p, types.Part) else types.Part(text=p) for p in parts],
        )
    ]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT + lang_instruction(uid) + extra_system,
        max_output_tokens=MAX_TOKENS,
        temperature=0.7,
    )

    header = header[:200]
    thinking = Thinking(status_msg, phrases, header)
    thinking.start()
    full_text, last_error = "", None
    try:
        for model_name in candidates:
            full_text = ""
            try:
                log.info("Streaming with %s", model_name)
                loop = asyncio.get_running_loop()
                last_edit = 0.0
                async with asyncio.timeout(RESPONSE_TIMEOUT):
                    stream = await client.aio.models.generate_content_stream(
                        model=model_name, contents=contents, config=config
                    )
                    async for chunk in stream:
                        if not chunk.text:
                            continue
                        if not full_text:
                            await thinking.stop()  # first token: animation hands over to live text
                        full_text += chunk.text
                        now = loop.time()
                        if now - last_edit >= STREAM_EDIT_INTERVAL:
                            preview = full_text[-STREAM_TAIL:] + CURSOR
                            await safe_edit(status_msg, f"{header}\n\n{preview}" if header else preview)
                            last_edit = loop.time()
                if full_text.strip():
                    break
                last_error = RuntimeError("Empty response (possibly blocked by safety filters)")
            except Exception as e:  # noqa: BLE001 - try the next fallback model
                log.warning("Model %s failed: %s", model_name, e)
                last_error = e
                if full_text.strip():
                    break  # keep partial answer rather than discarding it
    finally:
        await thinking.stop()

    final = full_text.strip()
    if final:
        if save:
            save_turn(uid, stored_query, final)
        await send_final(status_msg, update, final, header)
    else:
        log.error("All models failed. Last error: %s", last_error)
        try:
            await status_msg.edit_text(friendly_error(last_error), parse_mode=ParseMode.HTML)
        except BadRequest:
            pass


IMAGE_MODELS = [
    "gemini-2.5-flash-image",
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-lite-image",
    "gemini-3-pro-image",
]
IMAGE_PROMPT_RULES = (
    "Rewrite the user's request as one detailed English prompt for an image generator. "
    "Keep EVERY subject, count, color, setting, text and style the user asked for exactly; "
    "never add watermarks, logos, captions or signatures. Translate from any language. "
    "Add concrete visual detail (composition, lighting, quality) only where the user left it open. "
    "Output ONLY the prompt."
)


async def _enhance_prompt(prompt: str) -> str:
    """Translate/expand the request so the image model understands it (any language)."""
    if not client:
        return prompt
    for model in MODEL_MAP[DEFAULT_MODEL][:2]:
        try:
            async with asyncio.timeout(15):
                r = await client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=IMAGE_PROMPT_RULES, max_output_tokens=400, temperature=0.4
                    ),
                )
            text = (r.text or "").strip() if r else ""
            if text:
                return text
        except Exception as e:  # noqa: BLE001
            log.warning("Prompt enhancement with %s failed: %s", model, str(e)[:120])
    return prompt


async def _generate_image(prompt: str) -> bytes | None:
    if not client:
        return None
    for model in IMAGE_MODELS:
        try:
            async with asyncio.timeout(60):
                r = await client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
                )
            for part in (r.candidates[0].content.parts if r and r.candidates else []) or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
        except Exception as e:  # noqa: BLE001 - try the next image model
            log.warning("Image model %s failed: %s", model, str(e)[:160])
    return None


async def handle_image_generation(prompt: str, status_msg, update: Update) -> None:
    try:
        async with Thinking(status_msg, animation.IMAGE_PHRASES):
            await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
            image = await _generate_image(await _enhance_prompt(prompt))
        if image:
            await update.effective_message.reply_photo(
                photo=image,
                caption=f"✦ {html.escape(prompt[:900])}",
                parse_mode=ParseMode.HTML,
            )
            await status_msg.delete()
        else:
            await safe_edit(status_msg, "😔 I couldn't create that image right now. Please try again in a moment.")
    except Exception:  # noqa: BLE001
        log.exception("Image generation error")
        await safe_edit(status_msg, "😔 I couldn't create that image right now. Please try again in a moment.")


# ---------- Menus ----------
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎨 Generate Image", callback_data="feature_image_gen"),
                InlineKeyboardButton("🧠 Model", callback_data="model_menu"),
            ],
            [
                InlineKeyboardButton("🌐 Language", callback_data="lang_menu"),
                InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
            ],
            [InlineKeyboardButton("🧹 New chat", callback_data="clear")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="main_menu")]])


WELCOME = (
    "✦ <b>ALI AI</b> — multimodal assistant\n"
    "<i>created by ZEEYO</i>\n\n"
    "• Ask me anything — I stream answers live\n"
    "• Send a <b>photo / GIF</b> and I'll analyze it\n"
    "• Send a <b>voice note</b> and I'll listen and reply\n"
    "• Send a <b>PDF or text file</b> for a summary\n"
    "• <code>/image</code> + prompt to generate pictures\n\n"
    "In groups, mention me or reply to my message."
)


def settings_text(uid: int) -> str:
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"• Model: <code>{get_user_model(uid)}</code>\n"
        f"• Language: {html.escape(LANGUAGES[get_user_lang(uid)])}\n"
        f"• Messages in memory: {get_message_count(uid)}"
    )


def model_markup(uid: int) -> InlineKeyboardMarkup:
    cur = get_user_model(uid)
    rows = [
        [InlineKeyboardButton(f"{'✅ ' if m == cur else ''}{m}", callback_data=f"set_model:{m}")]
        for m in AVAILABLE_MODELS
    ]
    rows.append([InlineKeyboardButton("« Back", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def lang_markup(uid: int) -> InlineKeyboardMarkup:
    cur = get_user_lang(uid)
    rows = [
        [InlineKeyboardButton(f"{'✅ ' if k == cur else ''}{v}", callback_data=f"set_lang:{k}")]
        for k, v in LANGUAGES.items()
    ]
    rows.append([InlineKeyboardButton("« Back", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


async def edit_screen(query, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit the menu message whether it's text or the animated-logo caption."""
    try:
        if query.message.text:
            await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        else:
            await query.edit_message_caption(caption=text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ---------- Commands ----------
async def send_welcome(update: Update) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_keyboard())


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if authorized(update):
        await send_welcome(update)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.effective_message.reply_text(
        "✦ <b>ALI AI — Guide</b>\n\n"
        "/start — main menu\n"
        "/image <code>prompt</code> — generate an image\n"
        "/model — switch model\n"
        "/language — switch language\n"
        "/settings — your configuration\n"
        "/clear — wipe chat memory\n"
        "/about — about ALI AI\n\n"
        "Send text, photos, GIFs, voice notes or documents anytime.",
        parse_mode=ParseMode.HTML,
    )


async def about_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    caption = (
        "✦ <b>ALI AI</b>\n<i>Ultimate multimodal assistant</i>\n\n"
        "• Creator: ZEEYO\n"
        "• Skills: text, vision, voice, documents, image generation"
    )
    await update.effective_message.reply_text(caption, parse_mode=ParseMode.HTML)


async def image_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    prompt = " ".join(ctx.args) if ctx.args else ""
    if not prompt:
        await update.effective_message.reply_text(
            "Usage: <code>/image a futuristic cyberpunk city at night</code>", parse_mode=ParseMode.HTML
        )
        return
    status = await update.effective_message.reply_text("✦ Starting…")
    await handle_image_generation(prompt, status, update)


async def clear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if authorized(update):
        clear_history(update.effective_user.id)
        await update.effective_message.reply_text("🧹 Fresh start — memory cleared.")


async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if authorized(update):
        uid = update.effective_user.id
        await update.effective_message.reply_text(
            settings_text(uid), parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
        )


async def model_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if authorized(update):
        await update.effective_message.reply_text(
            "🧠 <b>Select model</b>", parse_mode=ParseMode.HTML, reply_markup=model_markup(update.effective_user.id)
        )


async def language_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if authorized(update):
        await update.effective_message.reply_text(
            "🌐 <b>Select language</b>", parse_mode=ParseMode.HTML, reply_markup=lang_markup(update.effective_user.id)
        )


# ---------- Inline callbacks ----------
async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if (query.data or "").startswith("inl:"):
        await inline_button(update, ctx)
        return
    await query.answer()
    uid = query.from_user.id
    if ALLOWED and uid not in ALLOWED:
        return
    data = query.data

    if data == "feature_image_gen":
        await query.message.reply_text(
            "🎨 <b>Image generation</b>\nSend <code>/image your idea here</code>", parse_mode=ParseMode.HTML
        )
    elif data == "settings":
        await edit_screen(query, settings_text(uid), back_keyboard())
    elif data == "model_menu":
        await edit_screen(query, "🧠 <b>Select model</b>", model_markup(uid))
    elif data == "lang_menu":
        await edit_screen(query, "🌐 <b>Select language</b>", lang_markup(uid))
    elif data.startswith("set_model:"):
        new_model = data.split(":", 1)[1]
        if new_model in AVAILABLE_MODELS:
            set_user_model(uid, new_model)
            await edit_screen(query, "🧠 <b>Select model</b>", model_markup(uid))
    elif data.startswith("set_lang:"):
        new_lang = data.split(":", 1)[1]
        if new_lang in LANGUAGES:
            set_user_lang(uid, new_lang)
            await edit_screen(query, "🌐 <b>Select language</b>", lang_markup(uid))
    elif data == "clear":
        clear_history(uid)
        await query.message.reply_text("🧹 Fresh start — memory cleared.")
    elif data == "main_menu":
        await edit_screen(query, WELCOME, main_keyboard())


# ---------- Message handlers ----------
async def begin(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str = ChatAction.TYPING):
    """Common gate: auth, group addressing, per-user lock. Returns (uid, lock) or None."""
    if not authorized(update) or not addressed(update, ctx):
        return None
    uid = update.effective_user.id
    lock = locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await update.effective_message.reply_text("⏳ One moment — still working on your previous message.")
        return None
    await update.effective_chat.send_action(action)
    return uid, lock


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    gate = await begin(update, ctx)
    if not gate:
        return
    uid, lock = gate
    user_text = strip_mention(update.effective_message.text or "", ctx)
    if not user_text:
        return
    async with lock:
        status = await update.effective_message.reply_text(f"{animation.SPINNER[0]} Thinking.")
        await respond(update, uid, [user_text], user_text, status)


async def on_image_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    gate = await begin(update, ctx)
    if not gate:
        return
    uid, lock = gate
    msg = update.effective_message
    async with lock:
        status = await msg.reply_text(f"{animation.SPINNER[0]} Looking closely.")
        try:
            mime = "image/jpeg"
            if msg.photo:
                file_obj = await msg.photo[-1].get_file()
            elif msg.animation:
                file_obj = await msg.animation.get_file()
                mime = msg.animation.mime_type or "image/gif"
            elif msg.document:
                file_obj = await msg.document.get_file()
                mime = msg.document.mime_type or "image/jpeg"
            else:
                await safe_edit(status, "Please send a valid image or GIF.")
                return
            data = bytes(await file_obj.download_as_bytearray())
            caption = strip_mention(msg.caption or "", ctx) or "Describe this image in detail and answer any questions about it."
            await respond(
                update, uid, [types.Part.from_bytes(data=data, mime_type=mime), caption],
                f"[Image sent] {caption}", status, animation.LOOK_PHRASES,
            )
        except Exception:
            log.exception("Error in image handler")
            await safe_edit(status, "Sorry, I couldn't process that image. Please try again.")


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    gate = await begin(update, ctx, ChatAction.RECORD_VOICE)
    if not gate:
        return
    uid, lock = gate
    msg = update.effective_message
    async with lock:
        status = await msg.reply_text(f"{animation.SPINNER[0]} Listening.")
        try:
            media = msg.voice or msg.audio
            file_obj = await media.get_file()
            data = bytes(await file_obj.download_as_bytearray())
            mime = "audio/ogg" if msg.voice else (media.mime_type or "audio/mpeg")
            prompt = (
                "Listen to this audio message, understand what was said, and respond helpfully "
                "in the speaker's language. Start with a short italic line showing what you heard."
            )
            await respond(
                update, uid, [types.Part.from_bytes(data=data, mime_type=mime), prompt],
                "[Voice message sent]", status, animation.LISTEN_PHRASES,
            )
        except Exception:
            log.exception("Error in voice handler")
            await safe_edit(status, "Sorry, I couldn't process that audio. Please try again.")


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    doc = msg.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        if authorized(update) and addressed(update, ctx):
            await msg.reply_text("That file is too large (20 MB max).")
        return
    gate = await begin(update, ctx)
    if not gate:
        return
    uid, lock = gate
    async with lock:
        status = await msg.reply_text(f"{animation.SPINNER[0]} Reading.")
        try:
            raw = bytes(await (await doc.get_file()).download_as_bytearray())
            caption = strip_mention(msg.caption or "", ctx) or "Summarize this document and extract the key points."
            name = (doc.file_name or "file").lower()
            if doc.mime_type == "application/pdf" or name.endswith(".pdf"):
                parts = [types.Part.from_bytes(data=raw, mime_type="application/pdf"), caption]
            else:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    await safe_edit(status, "I can read PDFs and UTF-8 text/code files only.")
                    return
                parts = [f"Document '{doc.file_name}':\n\n{text[:100_000]}\n\n{caption}"]
            await respond(
                update, uid, parts, f"[Document: {doc.file_name}] {caption}", status, animation.DOC_PHRASES
            )
        except Exception:
            log.exception("Error in document handler")
            await safe_edit(status, "Sorry, I couldn't process that document. Please try again.")


# ---------- Anywhere access: inline mode, channels, new groups ----------
class InlineTarget:
    """Adapter so respond() can animate/edit an inline-mode message (no chat id, only inline_message_id)."""

    def __init__(self, bot, inline_message_id: str):
        self.bot, self.inline_message_id = bot, inline_message_id

    async def edit_text(self, text: str, **kwargs):
        return await self.bot.edit_message_text(text, inline_message_id=self.inline_message_id, **kwargs)

    async def reply_text(self, *args, **kwargs):  # inline messages can't spill into follow-ups
        log.info("Inline answer overflow dropped")


pending_inline: dict[str, tuple[int, str]] = {}  # result_id -> (user_id, question)
background_tasks: set[asyncio.Task] = set()
INLINE_SYSTEM = " Keep the answer under 2500 characters; it will be posted inside someone else's chat."


def launch_inline(bot, rid: str, inline_message_id: str, uid: int, question: str) -> None:
    async def run():
        target = InlineTarget(bot, inline_message_id)
        async with locks.setdefault(uid, asyncio.Lock()):
            await respond(
                SimpleNamespace(effective_message=target), uid, [question], question, target,
                use_history=False, save=False, header=f"❓ {question}", extra_system=INLINE_SYSTEM,
            )

    task = asyncio.create_task(run())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def on_inline_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`@bot your question` typed in ANY chat (even ones the bot isn't in)."""
    iq = update.inline_query
    uid = iq.from_user.id
    if ALLOWED and uid not in ALLOWED:
        await iq.answer([], cache_time=5, is_personal=True)
        return
    q = iq.query.strip()
    uname = ctx.bot.username
    if not q:
        result = InlineQueryResultArticle(
            id="hint",
            title="✦ ALI AI",
            description="Type your question after my username…",
            input_message_content=InputTextMessageContent(
                f"✦ <b>ALI AI</b> — type <code>@{uname}</code> followed by your question, in any chat.",
                parse_mode=ParseMode.HTML,
            ),
        )
        await iq.answer([result], cache_time=0, is_personal=True)
        return

    q = q[:500]
    rid = uuid.uuid4().hex
    pending_inline[rid] = (uid, q)
    while len(pending_inline) > 500:  # unchosen suggestions pile up; drop the oldest
        pending_inline.pop(next(iter(pending_inline)))
    result = InlineQueryResultArticle(
        id=rid,
        title="✦ Ask ALI AI",
        description=q,
        input_message_content=InputTextMessageContent(
            f"❓ {html.escape(q)}\n\n✦ <i>Thinking…</i>", parse_mode=ParseMode.HTML
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✦ Get answer", callback_data=f"inl:{rid}")]]),
    )
    await iq.answer([result], cache_time=0, is_personal=True)


async def on_chosen_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fires when the user sends the inline result (needs /setinlinefeedback enabled in BotFather)."""
    r = update.chosen_inline_result
    info = pending_inline.pop(r.result_id, None)
    if info and r.inline_message_id:
        launch_inline(ctx.bot, r.result_id, r.inline_message_id, info[0], info[1])


async def inline_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fallback trigger: tapping 'Get answer' works even if inline feedback isn't enabled."""
    query = update.callback_query
    rid = query.data.split(":", 1)[1]
    info = pending_inline.get(rid)
    if not info or not query.inline_message_id:
        await query.answer("Already answering, or this request expired.")
        return
    if info[0] != query.from_user.id:
        await query.answer("Only the person who asked can start this answer.", show_alert=True)
        return
    pending_inline.pop(rid, None)
    await query.answer("Thinking…")
    launch_inline(ctx.bot, rid, query.inline_message_id, info[0], info[1])


async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Answer when a channel post mentions the bot (bot must be a channel admin; only admins can post)."""
    msg = update.channel_post
    text = msg.text or msg.caption or ""
    uname = (ctx.bot.username or "").lower()
    if not uname or f"@{uname}" not in text.lower():
        return
    question = strip_mention(text, ctx)
    if not question:
        return
    lock = locks.setdefault(msg.chat.id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        status = await msg.reply_text(f"{animation.SPINNER[0]} Thinking.")
        await respond(update, msg.chat.id, [question], question, status, use_history=False, save=False)


async def on_added_to_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Introduce ALI when it is added to a group."""
    change = update.my_chat_member
    if change.chat.type not in ("group", "supergroup"):
        return
    was_in = change.old_chat_member.status in ("member", "administrator", "creator")
    now_in = change.new_chat_member.status in ("member", "administrator")
    if now_in and not was_in:
        await ctx.bot.send_message(
            change.chat.id,
            f"✦ <b>ALI AI</b> is here!\n\nMention me — <code>@{ctx.bot.username} your question</code> — "
            "or reply to one of my messages. I can also read photos, voice notes and documents.",
            parse_mode=ParseMode.HTML,
        )


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error in application", exc_info=ctx.error)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Main menu"),
            BotCommand("image", "Generate an image"),
            BotCommand("model", "Switch model"),
            BotCommand("language", "Switch language"),
            BotCommand("settings", "Your settings"),
            BotCommand("clear", "Clear memory"),
            BotCommand("help", "How to use ALI AI"),
            BotCommand("about", "About ALI AI"),
        ]
    )


def main() -> None:
    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set in environment or .env file.")
        return
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY is not set in environment or .env file.")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    # must be registered first: channel posts have no user, so the handlers below would swallow them
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & (filters.TEXT | filters.CAPTION), on_channel_post))
    app.add_handler(InlineQueryHandler(on_inline_query))
    app.add_handler(ChosenInlineResultHandler(on_chosen_inline))
    app.add_handler(ChatMemberHandler(on_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    for name, fn in (
        ("start", start), ("help", help_cmd), ("about", about_cmd), ("settings", settings_cmd),
        ("model", model_cmd), ("language", language_cmd), ("history", settings_cmd),
        ("clear", clear_cmd), ("image", image_cmd),
    ):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.ANIMATION | filters.Document.IMAGE, on_image_media))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.IMAGE, on_document))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("ALI AI (created by ZEEYO) is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
