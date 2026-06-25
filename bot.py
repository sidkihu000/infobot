import os
import json
import logging
import re
import asyncio
import tempfile
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)

# ---- Optional: for video creation ----
try:
    from moviepy.editor import VideoFileClip, TextClip, CompositeVideoClip, ImageClip
    from moviepy.video.fx.all import resize
    MOVIEPY_AVAILABLE = True
except ImportError:
    MOVIEPY_AVAILABLE = False
    print("moviepy not installed – animate feature disabled.")

# ---- Optional: for music download ----
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False
    print("yt-dlp not installed – music search disabled.")

# ---- Optional: for AI answers ----
try:
    import aiohttp
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False
    print("aiohttp not installed – AI mode disabled.")

# ---- Configuration ----
TOKEN = "6935043231:AAFSnPWsC8ti9j3npYHFQZU8wABrN5knfDU"   # Your bot token
OWNER_ID = 2119464081  # Your Telegram user ID (owner)

# Use a persistent volume if provided by Railway, otherwise local directory
DB_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
DB_FILE = os.path.join(DB_PATH, "bot_data.db")

# ---- Logging ----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---- Database setup ----
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS chat_modes (
            chat_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS admin_videos (
            chat_id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS spam_counts (
            chat_id TEXT,
            user_id TEXT,
            timestamps_json TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS spam_warnings (
            chat_id TEXT,
            user_id TEXT,
            warn_count INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ---- Helper: get/set mode ----
def get_mode(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT mode FROM chat_modes WHERE chat_id = ?", (str(chat_id),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "music"

def set_mode(chat_id, mode):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO chat_modes (chat_id, mode) VALUES (?, ?)",
              (str(chat_id), mode))
    conn.commit()
    conn.close()

# ---- Helper: admin video ----
def get_admin_video(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT file_id FROM admin_videos WHERE chat_id = ?", (str(chat_id),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_admin_video(chat_id, file_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO admin_videos (chat_id, file_id) VALUES (?, ?)",
              (str(chat_id), file_id))
    conn.commit()
    conn.close()

# ---- Spam protection ----
SPAM_LIMIT = 5
SPAM_BAN_DURATION = 60

def is_spam(chat_id, user_id):
    chat_id_str = str(chat_id)
    user_id_str = str(user_id)
    now = datetime.now()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT timestamps_json FROM spam_counts WHERE chat_id = ? AND user_id = ?",
              (chat_id_str, user_id_str))
    row = c.fetchone()
    timestamps = json.loads(row[0]) if row else []
    timestamps = [t for t in timestamps if (now - datetime.fromisoformat(t)).seconds < 10]
    timestamps.append(now.isoformat())
    c.execute("INSERT OR REPLACE INTO spam_counts (chat_id, user_id, timestamps_json) VALUES (?, ?, ?)",
              (chat_id_str, user_id_str, json.dumps(timestamps)))
    if len(timestamps) > SPAM_LIMIT:
        c.execute("SELECT warn_count FROM spam_warnings WHERE chat_id = ? AND user_id = ?",
                  (chat_id_str, user_id_str))
        row_warn = c.fetchone()
        warn_count = row_warn[0] + 1 if row_warn else 1
        c.execute("INSERT OR REPLACE INTO spam_warnings (chat_id, user_id, warn_count) VALUES (?, ?, ?)",
                  (chat_id_str, user_id_str, warn_count))
        conn.commit()
        conn.close()
        if warn_count >= 3:
            return "mute"
        return "warn"
    else:
        conn.commit()
        conn.close()
        return False

# ---- Music search using yt-dlp ----
async def search_and_download_audio(query):
    if not YTDLP_AVAILABLE:
        return None, None
    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "quiet": True,
        "no_warnings": True,
        "extractaudio": True,
        "outtmpl": "%(title)s.%(ext)s",
        "default_search": "ytsearch1",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch1:{query}", download=True)
            if info and "entries" in info and len(info["entries"]) > 0:
                entry = info["entries"][0]
                title = entry.get("title", "song")
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    tmp_path = tmp.name
                ydl_opts["outtmpl"] = tmp_path.replace(".mp3", "")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl2:
                    ydl2.extract_info(f"ytsearch1:{query}", download=True)
                actual_file = tmp_path + ".mp3"
                if os.path.exists(actual_file):
                    return actual_file, title
                for f in os.listdir(tempfile.gettempdir()):
                    if f.endswith(".mp3") and os.path.getctime(os.path.join(tempfile.gettempdir(), f)) > datetime.now().timestamp() - 10:
                        return os.path.join(tempfile.gettempdir(), f), title
                return None, None
        except Exception as e:
            logger.error(f"Music download error: {e}")
            return None, None
    return None, None

# ---- AI answer (placeholder) ----
async def get_ai_answer(question):
    if not AI_AVAILABLE:
        return "AI mode not available (aiohttp missing)."
    # Replace with your own AI endpoint
    return f"🤖 I'm in AI mode! You asked: '{question}'. (Placeholder – add your AI)."

# ---- Create animated name video ----
async def create_name_video(background_video_path, name, output_path):
    if not MOVIEPY_AVAILABLE:
        return False
    try:
        bg = VideoFileClip(background_video_path)
        bg = bg.resize(height=480)
        from moviepy.video.VideoClip import ColorClip
        black_bg = ColorClip(size=(bg.w, 120), color=(0,0,0), duration=bg.duration)
        black_bg = black_bg.set_position((0, bg.h - 120))
        composite = CompositeVideoClip([bg, black_bg])
        txt = TextClip(name, fontsize=70, color='white', stroke_color='blue', stroke_width=2, font='Arial')
        txt = txt.set_duration(bg.duration)
        txt = txt.resize(height=100)
        txt = txt.set_position(('center', bg.h - 60))
        final = CompositeVideoClip([composite, txt])
        final.write_videofile(output_path, fps=24, codec='libx264', audio_codec='aac')
        return True
    except Exception as e:
        logger.error(f"Video creation error: {e}")
        return False

# ---- Command handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = get_mode(chat_id)
    video = get_admin_video(chat_id)
    msg = f"🤖 Bot active! Current mode: {mode.upper()}\n"
    msg += "Commands:\n"
    msg += "/switch – toggle between Music and AI mode\n"
    msg += "/animate @username – create animated name video\n"
    msg += "/del 100 – delete last 100 messages (admin only)\n"
    msg += "In Music mode: send 'search <song>' or 'find <song>'\n"
    if video:
        await update.message.reply_video(video, caption=msg)
    else:
        await update.message.reply_text(msg)

async def switch_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    current = get_mode(chat_id)
    new_mode = "ai" if current == "music" else "music"
    set_mode(chat_id, new_mode)
    await update.message.reply_text(f"🔄 Switched to {new_mode.upper()} mode.")

async def set_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("This command works only in groups.")
        return
    member = await chat.get_member(user.id)
    if not member.is_authenticated or not (member.status in ["administrator", "creator"]):
        await update.message.reply_text("Only admins can set the video.")
        return
    if update.message.reply_to_message and update.message.reply_to_message.video:
        file_id = update.message.reply_to_message.video.file_id
        set_admin_video(chat.id, file_id)
        await update.message.reply_text("✅ Admin video updated.")
    else:
        await update.message.reply_text("Reply to a video message with this command to set it as the admin video.")

async def animate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /animate @username (or just name)")
        return
    name = " ".join(args)
    if name.startswith("@"):
        name = name[1:]
    bg_video = get_admin_video(chat_id)
    if not bg_video:
        await update.message.reply_text("Admin has not set a background video. Ask admin to set one via /setvideo.")
        return
    try:
        file = await context.bot.get_file(bg_video)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_in:
            input_path = tmp_in.name
        await file.download_to_drive(input_path)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_out:
            output_path = tmp_out.name
        success = await create_name_video(input_path, name, output_path)
        if success:
            with open(output_path, "rb") as f:
                await update.message.reply_video(video=f, caption=f"🎬 Animated for {name}")
            os.unlink(input_path)
            os.unlink(output_path)
        else:
            await update.message.reply_text("Failed to create animation. Check moviepy/ffmpeg.")
    except Exception as e:
        logger.error(f"Animate error: {e}")
        await update.message.reply_text("Error creating animation.")

async def delete_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("This works only in groups.")
        return
    member = await chat.get_member(user.id)
    if not member.is_authenticated or not (member.status in ["administrator", "creator"]):
        await update.message.reply_text("Only admins can delete messages.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /del <number> (max 100)")
        return
    try:
        count = int(args[0])
        if count < 1 or count > 100:
            await update.message.reply_text("Number must be between 1 and 100.")
            return
    except ValueError:
        await update.message.reply_text("Invalid number.")
        return
    try:
        await update.message.delete()
        await update.message.reply_text(f"Deleted {count} messages (simulated – only this command was deleted).")
    except Exception as e:
        await update.message.reply_text(f"Could not delete: {e}")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    if not text:
        return
    user_id = update.effective_user.id
    spam_result = is_spam(chat_id, user_id)
    if spam_result == "mute":
        try:
            await context.bot.restrict_chat_member(
                chat_id, user_id,
                permissions=telegram.ChatPermissions(can_send_messages=False),
                until_date=datetime.now() + timedelta(seconds=SPAM_BAN_DURATION)
            )
            await update.message.reply_text(f"User {update.effective_user.first_name} muted for spamming.")
        except Exception as e:
            logger.error(f"Mute error: {e}")
        return
    elif spam_result == "warn":
        await update.message.reply_text("⚠️ Please don't spam.")
        return

    mode = get_mode(chat_id)
    if mode == "music":
        pattern = r'(?i)(search|find)\s+(.+)'
        match = re.match(pattern, text)
        if match:
            query = match.group(2).strip()
            if not query:
                return
            msg = await update.message.reply_text("🔍 Searching for song...")
            audio_path, title = await search_and_download_audio(query)
            if audio_path and os.path.exists(audio_path):
                try:
                    with open(audio_path, "rb") as f:
                        await update.message.reply_audio(audio=f, title=title, performer="Bot")
                    os.unlink(audio_path)
                except Exception as e:
                    await msg.edit_text(f"Error sending audio: {e}")
            else:
                await msg.edit_text("❌ Could not find the song. Please try another.")
        else:
            await update.message.reply_text("Use 'search <song name>' or 'find <song name>' to get audio.")
    else:
        answer = await get_ai_answer(text)
        await update.message.reply_text(answer)

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("switch", switch_mode))
    application.add_handler(CommandHandler("setvideo", set_video))
    application.add_handler(CommandHandler("animate", animate))
    application.add_handler(CommandHandler("del", delete_messages))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
