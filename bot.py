"""ALI AI - Ultimate Multimodal AI Assistant with Image Generation (Imagen 3), Vision, Audio, & Text.
Created by ZEEYO.
"""
import asyncio
import logging
import os
import sqlite3

from google import genai
from google.genai import types
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
    "You are ALI AI, a powerful, highly intelligent, and futuristic multimodal AI assistant created by ZEEYO. "
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


async def handle_multimodal_response(uid: int, contents, status_msg: Update, update: Update, stored_query: str) -> None:
    if not client:
        await status_msg.edit_text("AI client is not initialized.")
        return
        
    user_model_choice = get_user_model(uid)
    candidate_models = MODEL_MAP.get(user_model_choice, ["gemini-3.5-flash-lite", "gemini-2.5-flash-lite"])
    
    lang = get_user_lang(uid)
    lang_instruction = f" Respond in {'Uzbek' if lang == 'uz' else 'English'}."
    
    config = {
        "system_instruction": SYSTEM_PROMPT + lang_instruction,
        "max_output_tokens": MAX_TOKENS,
        "temperature": 0.7,
    }
    
    success = False
    last_error = None
    final_text = ""
    
    for model_name in candidate_models:
        try:
            log.info("Attempting multimodal generation with model: %s", model_name)
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=contents,
                    config=config,
                ),
                timeout=30.0
            )
            final_text = response.text.strip() if response and response.text else ""
            if final_text:
                success = True
                break
        except Exception as e:
            log.warning("Multimodal model %s failed: %s. Trying fallback...", model_name, e)
            last_error = e
            continue
            
    if success and final_text:
        save_turn(uid, stored_query, final_text)
        formatted_final = f"[ ALI AI ]\n━━━━━━━━━━━━━━\n{final_text}"
        try:
            await status_msg.edit_text(formatted_final[:4000])
        except Exception:
            await update.message.reply_text(formatted_final[:4000])
            
        for i in range(4000, len(formatted_final), 4000):
            await update.message.reply_text(formatted_final[i : i + 4000])
    else:
        await status_msg.edit_text("Sorry, I couldn't process that media. Please try again.")


async def handle_image_generation(prompt: str, status_msg: Update, update: Update) -> None:
    if not client:
        await status_msg.edit_text("AI client is not initialized.")
        return
        
    try:
        await status_msg.edit_text("[ ALI AI ] ➔ Generating AI Image with Imagen 3...")
        result = await asyncio.to_thread(
            client.models.generate_images,
            model='imagen-3.0-generate-002',
            prompt=prompt,
            config=dict(
                number_of_images=1,
                output_mime_type="image/jpeg",
                aspect_ratio="1:1",
            )
        )
        if result and result.generated_images:
            image_bytes = result.generated_images[0].image.image_bytes
            await update.message.reply_photo(photo=image_bytes, caption=f"[ ALI AI ] Generated: {prompt}")
            await status_msg.delete()
        else:
            await status_msg.edit_text("Sorry, I couldn't generate that image. Please try another prompt.")
    except Exception as e:
        log.exception("Image generation error: %s", e)
        await status_msg.edit_text(f"⚠️ Image Generation Error: {e}")


# ---------- Commands ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    
    keyboard = [
        [
            InlineKeyboardButton("Generate Image", callback_data="feature_image_gen"),
            InlineKeyboardButton("Change Model", callback_data="model_menu"),
        ],
        [
            InlineKeyboardButton("Language", callback_data="lang_menu"),
            InlineKeyboardButton("Clear History", callback_data="clear"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "[ ALI AI — Ultimate Multimodal Assistant ]\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "CREATED BY ZEEYO\n\n"
        "Welcome! I analyze images/GIFs, read documents, listen to voice notes, and generate AI images (Imagen 3).\n\n"
        "• Send any photo or GIF to analyze it\n"
        "• Send a voice note to talk to me\n"
        "• Use /image <prompt> to generate AI images\n"
        "• Type any message for instant answers."
    )
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    help_text = (
        "[ ALI AI — User Guide ]\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "• /start - Main menu\n"
        "• /image <prompt> - Generate AI images (Imagen 3)\n"
        "• /settings - View configuration\n"
        "• /model - Switch model\n"
        "• /language - Switch language\n"
        "• /clear - Wipe chat memory\n\n"
        "Send photos, GIFs, voice messages, or text anytime!"
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
        "• Capabilities: Text, Vision, Voice, Documents, Image Generation (Imagen 3)."
    )
    await update.message.reply_text(about_text)


async def image_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    prompt = " ".join(ctx.args) if ctx.args else ""
    if not prompt:
        await update.message.reply_text("Usage: /image <prompt> (e.g., /image a futuristic cyberpunk city)")
        return
        
    status_msg = await update.message.reply_text("[ ALI AI ] ➔ Initializing Image Generator...")
    await handle_image_generation(prompt, status_msg, update)


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
    if data == "feature_image_gen":
        await query.message.reply_text(
            "🎨 *AI Image Generation (Imagen 3)*\n\nType `/image <your prompt>` to generate stunning AI images instantly!",
            parse_mode="Markdown"
        )
    elif data == "settings":
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
                InlineKeyboardButton("Generate Image", callback_data="feature_image_gen"),
                InlineKeyboardButton("Change Model", callback_data="model_menu"),
            ],
            [
                InlineKeyboardButton("Language", callback_data="lang_menu"),
                InlineKeyboardButton("Clear History", callback_data="clear"),
            ],
        ]
        await query.edit_message_text(
            "[ ALI AI — Ultimate Multimodal Assistant ]\nCREATED BY ZEEYO\n\nChoose an option:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ---------- Message Handlers (Text, Image/GIF, Voice, Document) ----------
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


async def on_image_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    uid = update.effective_user.id
    
    lock = locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await update.message.reply_text("One moment, I'm still processing your previous message.")
        return

    async with lock:
        await update.effective_chat.send_action(ChatAction.TYPING)
        status_msg = await update.message.reply_text("[ ALI AI ] ➔ Analyzing Image / GIF...")
        
        try:
            msg = update.message
            file_obj = None
            mime_type = "image/jpeg"
            
            if msg.photo:
                file_obj = await msg.photo[-1].get_file()
                mime_type = "image/jpeg"
            elif msg.animation:
                file_obj = await msg.animation.get_file()
                mime_type = msg.animation.mime_type or "image/gif"
            elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
                file_obj = await msg.document.get_file()
                mime_type = msg.document.mime_type
            elif msg.document and msg.document.file_name and any(msg.document.file_name.lower().endswith(ext) for ext in [".gif", ".png", ".jpg", ".jpeg", ".webp"]):
                file_obj = await msg.document.get_file()
                name = msg.document.file_name.lower()
                if name.endswith(".gif"):
                    mime_type = "image/gif"
                elif name.endswith(".png"):
                    mime_type = "image/png"
                elif name.endswith(".webp"):
                    mime_type = "image/webp"
                else:
                    mime_type = "image/jpeg"
            else:
                await status_msg.edit_text("Please send a valid image or GIF.")
                return
                
            media_bytes = bytes(await file_obj.download_as_bytearray())
            caption = msg.caption or "Describe this image or animated GIF in detail and answer any questions."
            
            media_part = types.Part.from_bytes(data=media_bytes, mime_type=mime_type)
            contents = [media_part, caption]
            
            await handle_multimodal_response(uid, contents, status_msg, update, f"[Image/GIF sent] {caption}")
        except Exception:
            log.exception("Error in image/GIF handler")
            await status_msg.edit_text("Sorry, I couldn't process that image or GIF. Please try again.")


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    uid = update.effective_user.id
    
    lock = locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await update.message.reply_text("One moment, I'm still processing your previous message.")
        return

    async with lock:
        await update.effective_chat.send_action(ChatAction.RECORD_VOICE)
        status_msg = await update.message.reply_text("[ ALI AI ] ➔ Listening to Voice Note...")
        
        try:
            voice_file = await update.message.voice.get_file()
            voice_bytes = bytes(await voice_file.download_as_bytearray())
            
            audio_part = types.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg")
            contents = [audio_part, "Please listen to this voice message, transcribe or understand what was said, and respond helpfully."]
            
            await handle_multimodal_response(uid, contents, status_msg, update, "[Voice message sent]")
        except Exception:
            log.exception("Error in voice handler")
            await status_msg.edit_text("Sorry, I couldn't process that voice message. Please try again.")


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    uid = update.effective_user.id
    doc = update.message.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("That file is too large (20 MB max).")
        return

    lock = locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await update.message.reply_text("One moment, I'm still processing your previous message.")
        return

    async with lock:
        await update.effective_chat.send_action(ChatAction.TYPING)
        status_msg = await update.message.reply_text("[ ALI AI ] ➔ Analyzing Document...")
        
        try:
            file_obj = await doc.get_file()
            raw = bytes(await file_obj.download_as_bytearray())
            caption = update.message.caption or "Summarize this document and extract key points."
            name = (doc.file_name or "file").lower()
            
            if doc.mime_type == "application/pdf" or name.endswith(".pdf"):
                doc_part = types.Part.from_bytes(data=raw, mime_type="application/pdf")
                contents = [doc_part, caption]
            else:
                try:
                    file_text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    await status_msg.edit_text("I can read PDFs and UTF-8 text/code files only.")
                    return
                contents = f"Document '{doc.file_name}':\n\n{file_text[:100_000]}\n\n{caption}"
                
            await handle_multimodal_response(uid, contents, status_msg, update, f"[Document sent: {doc.file_name}] {caption}")
        except Exception:
            log.exception("Error in document handler")
            await status_msg.edit_text("Sorry, I couldn't process that document. Please try again.")


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
    app.add_handler(CommandHandler("image", image_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.ANIMATION | filters.Document.IMAGE, on_image_media))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.IMAGE, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("ALI AI (created by ZEEYO) is running as ultimate multimodal assistant with Imagen 3...")
    app.run_polling()


if __name__ == "__main__":
    main()
