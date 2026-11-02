"""ALI AI - Lightning-Fast AI Assistant with Atomic Logo Animation & Deliberate Typing.
Created by ZEEYO.
"""
import asyncio
import logging
import os
import sqlite3

from google import genai
from google.genai.errors import ClientError
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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

MODEL_MAP = {
    "ali-3.5-flash-lite": ["gemini-3.5-flash-lite", "gemini-2.5-flash-lite", "gemini-2.5-flash"],
    "ali-3.5-flash": ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-3.6-flash"],
    "ali-2.5-flash": ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-flash-lite"],
    "ali-1.5-flash": ["gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash"],
}
AVAILABLE_MODELS = list(MODEL_MAP.keys())

LANGUAGES = {
    "auto": "Auto-detect (As you type)",
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
    "You are ALI AI, a powerful, highly intelligent, and futuristic AI assistant created by ZEEYO. "
    "You operate inside Telegram. Answer clearly, accurately, and concisely. "
    "When writing code, use clean fenced code blocks. Be honest when unsure instead of guessing."
)

locks: dict[int, asyncio.Lock] = {}

# SQLite database setup with automatic schema migration
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, model TEXT DEFAULT 'ali-3.5-flash-lite', language TEXT DEFAULT 'auto')")
try:
    db.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'auto'")
    db.commit()
except sqlite3.OperationalError:
    pass
try:
    db.execute("ALTER TABLE users ADD COLUMN model TEXT DEFAULT 'ali-3.5-flash-lite'")
    db.commit()
except sqlite3.OperationalError:
    pass

db.execute(
    "CREATE TABLE IF NOT EXISTS messages ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "user_id INTEGER, "
    "role TEXT, "
    "content TEXT)"
)
db.execute("CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id)")
db.commit()


def get_user_model(uid: int) -> str:
    row = db.execute("SELECT model FROM users WHERE user_id=?", (uid,)).fetchone()
    if row and row[0] in MODEL_MAP:
        return row[0]
    return DEFAULT_MODEL


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
    if row and row[0] in LANGUAGES:
        return row[0]
    return "auto"


def set_user_lang(uid: int, lang: str) -> None:
    if lang not in LANGUAGES:
        lang = "auto"
    db.execute(
        "INSERT INTO users(user_id, language) VALUES(?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET language=excluded.language",
        (uid, lang),
    )
    db.commit()


def load_history(uid: int) -> list[dict]:
    rows = db.execute(
        "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (uid, HISTORY_LIMIT),
    ).fetchall()[::-1]
    
    history = []
    for role, content in rows:
        gemini_role = "user" if role == "user" else "model"
        history.append({"role": gemini_role, "parts": [{"text": content}]})
    return history


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


def authorized(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if chat.type not in ["private", "group", "supergroup"]:
        return False
    return not ALLOWED or user.id in ALLOWED


async def handle_text_response(uid: int, user_text: str, status_msg: Update, update: Update) -> None:
    if not client:
        await status_msg.edit_text("AI client is not initialized. Please check your API key in .env.")
        return
        
    user_model_choice = get_user_model(uid)
    candidate_models = MODEL_MAP.get(user_model_choice, ["gemini-3.5-flash-lite", "gemini-2.5-flash-lite"])
    history = load_history(uid)
    
    lang = get_user_lang(uid)
    if lang == "auto":
        lang_instruction = " Reply in the exact same language the user writes in."
    else:
        lang_instruction = f" Always reply in {'Uzbek' if lang == 'uz' else 'English'}."
    
    config = {
        "system_instruction": SYSTEM_PROMPT + lang_instruction,
        "max_output_tokens": MAX_TOKENS,
        "temperature": 0.7,
    }
    
    # Atomic-inspired rotating logo emblem frames
    atomic_frames = [
        "[ ALI AI ] ➔ Rotating Atomic Orbital Rings...",
        "[ ALI AI ] ➔ Pulsing Cyan & Purple Core...",
        "[ ALI AI ] ➔ Energizing Neural Network...",
    ]
    for frame in atomic_frames:
        try:
            await status_msg.edit_text(frame)
            await asyncio.sleep(0.35)
        except Exception:
            pass

    success = False
    last_error = None
    final_text = ""
    
    for model_name in candidate_models:
        try:
            log.info("Attempting streaming chat with model: %s (alias: %s)", model_name, user_model_choice)
            chat = client.chats.create(model=model_name, history=history, config=config)
            stream = await asyncio.to_thread(chat.send_message_stream, user_text)
            
            full_text = ""
            last_update_time = asyncio.get_event_loop().time()
            
            for chunk in stream:
                if chunk.text:
                    full_text += chunk.text
                    current_time = asyncio.get_event_loop().time()
                    if current_time - last_update_time >= 0.45:
                        display_text = f"[ ALI AI ]\n━━━━━━━━━━━━━━\n{full_text}"
                        try:
                            await status_msg.edit_text(display_text[:4000])
                            last_update_time = current_time
                        except Exception:
                            pass
                        await asyncio.sleep(0.1)
                            
            final_text = full_text.strip() or "I'm here! How can I help you today?"
            if final_text:
                success = True
                break
        except asyncio.TimeoutError:
            log.warning("Model %s timed out. Trying fallback...", model_name)
            last_error = asyncio.TimeoutError("Request timed out")
            continue
        except ClientError as ce:
            log.warning("Model %s failed with ClientError: %s. Trying fallback...", model_name, ce)
            last_error = ce
            continue
        except Exception as e:
            log.warning("Model %s failed with exception: %s. Trying fallback...", model_name, e)
            last_error = e
            continue
            
    if success and final_text:
        save_turn(uid, user_text, final_text)
        formatted_final = f"[ ALI AI ]\n━━━━━━━━━━━━━━\n{final_text}"
        try:
            await status_msg.edit_text(formatted_final[:4000])
        except Exception:
            await update.message.reply_text(formatted_final[:4000])
            
        for i in range(4000, len(formatted_final), 4000):
            await update.message.reply_text(formatted_final[i : i + 4000])
    else:
        log.error("All text candidate models failed. Last error: %s", last_error)
        err_str = str(last_error or "Unknown error")
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
            msg = (
                "⚠️ API Quota Exceeded (429)\n\n"
                "You have reached the daily free-tier request limit for your Gemini API key.\n"
                "Please check your plan/billing at Google AI Studio or add a new API key in .env."
            )
        elif "timed out" in err_str.lower():
            msg = "⚠️ Request Timed Out\nThe AI service took too long to respond. Please try again."
        else:
            msg = f"⚠️ AI Service Error: {err_str}"
        try:
            await status_msg.edit_text(msg)
        except Exception:
            await update.message.reply_text(msg)


# ---------- Commands ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    
    keyboard = [
        [
            InlineKeyboardButton("Settings", callback_data="settings"),
            InlineKeyboardButton("Change Model", callback_data="model_menu"),
        ],
        [
            InlineKeyboardButton("Language", callback_data="lang_menu"),
            InlineKeyboardButton("Clear History", callback_data="clear"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "[ ALI AI — Atomic Core Assistant ]\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "CREATED BY ZEEYO\n\n"
        "Welcome. Featuring an atomic-inspired rotating core animation and deliberate typewriter text generation.\n\n"
        "• Atomic Core Logo Animation\n"
        "• Deliberate Slow Typewriter Effect\n"
        "• Auto-Detects Any Language\n\n"
        "Type any message to start chatting."
    )
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    help_text = (
        "[ ALI AI — User Guide ]\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "• /start - Main menu\n"
        "• /help - Help instructions\n"
        "• /about - About creator (ZEEYO)\n"
        "• /settings - View configuration\n"
        "• /model - Switch model\n"
        "• /language - Switch language\n"
        "• /history - Usage statistics\n"
        "• /clear - Wipe chat memory\n\n"
        "Type any question to experience atomic core animations and slow typewriter text."
    )
    await update.message.reply_text(help_text)


async def about_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    about_text = (
        "[ About ALI AI ]\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "• Bot Name: ALI AI\n"
        "• Creator: ZEEYO\n"
        "• Storage: Secure Local SQLite History\n\n"
        "Designed with atomic-inspired geometric vector aesthetics and deliberate typewriter text generation."
    )
    await update.message.reply_text(about_text)


async def clear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    uid = update.effective_user.id
    clear_history(uid)
    await update.message.reply_text("Conversation history wiped successfully.")


async def history_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    uid = update.effective_user.id
    count = get_message_count(uid)
    model = get_user_model(uid)
    lang = get_user_lang(uid)
    
    stats_text = (
        "[ ALI AI — Usage Statistics ]\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Stored Messages in Context: {count}\n"
        f"• Active Model: {model}\n"
        f"• Language Mode: {LANGUAGES.get(lang, lang)}\n"
        f"• Creator: ZEEYO"
    )
    await update.message.reply_text(stats_text)


async def language_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    uid = update.effective_user.id
    curr_lang = get_user_lang(uid)
    
    rows = [
        [InlineKeyboardButton(f"{'[Active] ' if curr_lang == k else ''}{v}", callback_data=f"set_lang:{k}")]
        for k, v in LANGUAGES.items()
    ]
    rows.append([InlineKeyboardButton("Back to Menu", callback_data="main_menu")])
    await update.message.reply_text(
        "Select Language Mode\n(Auto-detect replies in your typed language)",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    uid = update.effective_user.id
    model = get_user_model(uid)
    lang = get_user_lang(uid)
    count = get_message_count(uid)
    
    settings_text = (
        "[ ALI AI — Settings ]\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• Active Model: {model}\n"
        f"• Language Mode: {LANGUAGES.get(lang, lang)}\n"
        f"• Stored Context Messages: {count}\n"
        f"• Creator: ZEEYO"
    )
    keyboard = [
        [InlineKeyboardButton("Change Model", callback_data="model_menu")],
        [InlineKeyboardButton("Change Language", callback_data="lang_menu")],
        [InlineKeyboardButton("Menu", callback_data="main_menu")],
    ]
    await update.message.reply_text(settings_text, reply_markup=InlineKeyboardMarkup(keyboard))


async def model_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    uid = update.effective_user.id
    current_model = get_user_model(uid)
    
    rows = []
    for m in AVAILABLE_MODELS:
        prefix = "[Active] " if m == current_model else ""
        rows.append([InlineKeyboardButton(f"{prefix}{m}", callback_data=f"set_model:{m}")])
    rows.append([InlineKeyboardButton("Back to Menu", callback_data="main_menu")])
    
    await update.message.reply_text(
        f"Select Model\nCurrent active: {current_model}",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ---------- Inline Callback Handlers ----------
async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if ALLOWED and uid not in ALLOWED:
        return
    
    data = query.data
    if data == "settings":
        model = get_user_model(uid)
        lang = get_user_lang(uid)
        await query.message.reply_text(
            f"[ Settings ]\n\n• Model: {model}\n• Language: {LANGUAGES.get(lang, lang)}\n• Creator: ZEEYO"
        )
    elif data == "model_menu":
        current_model = get_user_model(uid)
        rows = [
            [InlineKeyboardButton(f"{'[Active] ' if m == current_model else ''}{m}", callback_data=f"set_model:{m}")]
            for m in AVAILABLE_MODELS
        ]
        rows.append([InlineKeyboardButton("Back", callback_data="main_menu")])
        await query.edit_message_text(
            f"Select Model\nCurrent: {current_model}",
            reply_markup=InlineKeyboardMarkup(rows)
        )
    elif data == "lang_menu":
        curr_lang = get_user_lang(uid)
        rows = [
            [InlineKeyboardButton(f"{'[Active] ' if curr_lang == k else ''}{v}", callback_data=f"set_lang:{k}")]
            for k, v in LANGUAGES.items()
        ]
        rows.append([InlineKeyboardButton("Back", callback_data="main_menu")])
        await query.edit_message_text(
            "Select Language Mode",
            reply_markup=InlineKeyboardMarkup(rows)
        )
    elif data.startswith("set_model:"):
        new_model = data.split(":", 1)[1]
        if new_model in AVAILABLE_MODELS:
            set_user_model(uid, new_model)
            await query.edit_message_text(f"Model successfully updated to: {new_model}")
    elif data.startswith("set_lang:"):
        new_lang = data.split(":", 1)[1]
        if new_lang in LANGUAGES:
            set_user_lang(uid, new_lang)
            await query.edit_message_text(f"Language updated to: {LANGUAGES[new_lang]}")
    elif data == "clear":
        clear_history(uid)
        await query.message.reply_text("Conversation history cleared.")
    elif data == "main_menu":
        keyboard = [
            [
                InlineKeyboardButton("Settings", callback_data="settings"),
                InlineKeyboardButton("Change Model", callback_data="model_menu"),
            ],
            [
                InlineKeyboardButton("Language", callback_data="lang_menu"),
                InlineKeyboardButton("Clear History", callback_data="clear"),
            ],
        ]
        await query.edit_message_text(
            "[ ALI AI — Atomic Core Assistant ]\nCREATED BY ZEEYO\n\nChoose an option:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ---------- Message Handler (Text) ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    uid = update.effective_user.id
    user_text = update.message.text
    if not user_text:
        return

    lock = locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await update.message.reply_text("One moment, I'm still processing your previous message.")
        return

    async with lock:
        await update.effective_chat.send_action(ChatAction.TYPING)
        status_msg = await update.message.reply_text("[ ALI AI ] ➔ Initializing Atomic Core...")
        
        await handle_text_response(uid, user_text, status_msg, update)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error in application", exc_info=ctx.error)


def main() -> None:
    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set in environment or .env file.")
        return
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY is not set in environment or .env file.")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("language", language_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("ALI AI (created by ZEEYO) is running with professional atomic branding & slow typewriter speed...")
    app.run_polling()


if __name__ == "__main__":
    main()
