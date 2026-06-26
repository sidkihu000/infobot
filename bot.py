import os, json, logging, re, asyncio, tempfile, sqlite3
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
import google.generativeai as genai

# ---- Configuration ----
TOKEN = os.getenv("BOT_TOKEN", "6935043231:AAFSnPWsC8ti9j3npYHFQZU8wABrN5knfDU")
OWNER_ID = int(os.getenv("OWNER_ID", "2119464081"))
GEMINI_API_KEY = "AQ.Ab8RN6KBW9XnJZTH2LP0-s39-BPJHZKVQGrJw42vpGwELUftZA"  # Your provided API key
DB_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
DB_FILE = os.path.join(DB_PATH, "bot_data.db")

# ---- Configure Gemini ----
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-pro')

# ---- Optional imports ----
try:
    from moviepy.editor import VideoFileClip, TextClip, CompositeVideoClip, ImageClip
    from moviepy.video.fx.all import resize
    MOVIEPY_AVAILABLE = True
except ImportError:
    MOVIEPY_AVAILABLE = False
    print("moviepy not installed – animate feature disabled.")

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False
    print("yt-dlp not installed – music search disabled.")

try:
    import aiohttp
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False
    print("aiohttp not installed – AI mode disabled.")

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
            mode TEXT NOT NULL DEFAULT 'ai'
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
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_wallets (
            user_id TEXT PRIMARY KEY,
            balance REAL DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ---- Helper functions ----
def get_mode(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT mode FROM chat_modes WHERE chat_id = ?", (str(chat_id),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "ai"

def set_mode(chat_id, mode):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO chat_modes (chat_id, mode) VALUES (?, ?)",
              (str(chat_id), mode))
    conn.commit()
    conn.close()

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

# ---- YouTube cookies setup ----
def get_youtube_cookies():
    """Returns path to cookies file if exists"""
    cookies_path = "cookies.txt"
    if os.path.exists(cookies_path):
        return cookies_path
    return None

async def search_and_download_audio(query):
    if not YTDLP_AVAILABLE:
        return None, None, None
    
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
    
    # Add cookies if available
    cookies = get_youtube_cookies()
    if cookies:
        ydl_opts["cookiefile"] = cookies
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if info and "entries" in info and len(info["entries"]) > 0:
                entry = info["entries"][0]
                title = entry.get("title", "song")
                duration = entry.get("duration", 0)
                url = entry.get("webpage_url", "")
                
                # Download the audio
                info = ydl.extract_info(url, download=True)
                for f in os.listdir("."):
                    if f.endswith(".mp3") and title[:20] in f:
                        return os.path.abspath(f), title, duration
                return None, title, duration
        except Exception as e:
            logger.error(f"Music search error: {e}")
            return None, None, None
    return None, None, None

# ---- Gemini AI integration ----
async def get_gemini_response(question, context=""):
    try:
        if context:
            prompt = f"Context: {context}\n\nUser: {question}\n\nAssistant (adumusic):"
        else:
            prompt = f"User: {question}\n\nAssistant (adumusic):"
        
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return "I'm having trouble thinking right now. Please try again."

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

# ---- Premium inline menu ----
def get_premium_menu():
    keyboard = [
        [
            InlineKeyboardButton("💎 Premium", callback_data="premium_info"),
            InlineKeyboardButton("🎵 Music Search", callback_data="music_info")
        ],
        [
            InlineKeyboardButton("🤖 AI Chat", callback_data="ai_info"),
            InlineKeyboardButton("❓ Help", callback_data="help_info")
        ],
        [
            InlineKeyboardButton("⭐ Upgrade", callback_data="upgrade")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ---- Command handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode = get_mode(chat_id)
    video = get_admin_video(chat_id)
    
    msg = f"🎵 **adumusic - Your Premium Bot**\n\n"
    msg += f"Current Mode: {mode.upper()}\n\n"
    msg += "**Commands:**\n"
    msg += "• Just mention 'adumusic' to chat with me\n"
    msg += "• Send song name for music search\n"
    msg += "• /switch - Toggle AI/Music mode\n"
    msg += "• /animate @name - Create name video\n"
    
    await update.message.reply_text(
        msg,
        reply_markup=get_premium_menu(),
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "premium_info":
        msg = "💎 **Premium Features:**\n• Unlimited songs\n• HD audio quality\n• Priority support\n• No ads\n\nPrice: $5/month"
    elif query.data == "music_info":
        msg = "🎵 **Music Search:**\nJust send me any song name and I'll find it for you!"
    elif query.data == "ai_info":
        msg = "🤖 **AI Chat:**\nMention 'adumusic' to chat with AI. I can help with anything!"
    elif query.data == "help_info":
        msg = "❓ **Help:**\nJust type normally or mention 'adumusic' for AI responses."
    elif query.data == "upgrade":
        msg = "⭐ **Upgrade to Premium:**\nContact @adumusic_admin to upgrade!"
    
    await query.edit_message_text(
        msg,
        reply_markup=get_premium_menu(),
        parse_mode='Markdown'
    )

async def switch_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    current = get_mode(chat_id)
    new_mode = "ai" if current == "music" else "music"
    set_mode(chat_id, new_mode)
    await update.message.reply_text(
        f"🔄 Switched to {new_mode.upper()} mode.",
        reply_markup=get_premium_menu()
    )

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
        await update.message.reply_text(f"🗑 Deleted {count} messages successfully!")
    except Exception as e:
        await update.message.reply_text(f"Could not delete: {e}")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    if not text:
        return
    
    # Check if "adumusic" is mentioned
    text_lower = text.lower()
    is_mentioned = "adumusic" in text_lower
    
    # If it's a group chat, only respond when mentioned
    if update.effective_chat.type in ["group", "supergroup"]:
        if not is_mentioned and not text.startswith('/'):
            return
    
    # Song detection patterns
    song_patterns = [
        r'(?i)(play|send|search|find|song|music)\s+(.+)',
        r'(?i)(i want to listen to|play me|give me)\s+(.+)',
        r'(?i)^(.+)\s+(song|music|audio|track)$'
    ]
    
    is_song_request = False
    song_query = ""
    
    for pattern in song_patterns:
        match = re.match(pattern, text)
        if match:
            # Extract song name
            groups = match.groups()
            if len(groups) > 1:
                song_query = groups[1]
            else:
                song_query = groups[0]
            is_song_request = True
            break
    
    # If it's a song request
    if is_song_request and song_query:
        # Send searching message
        msg = await update.message.reply_text("🔍 Searching for your song...")
        
        audio_path, title, duration = await search_and_download_audio(song_query)
        
        if audio_path and os.path.exists(audio_path):
            try:
                # Send the audio with premium-looking caption
                caption = f"🎵 **{title}**\n⏱ Duration: {duration//60}:{duration%60:02d}\n🔊 Quality: HD\n\n🎧 _Powered by adumusic_"
                
                with open(audio_path, "rb") as f:
                    await update.message.reply_audio(
                        audio=f,
                        title=title,
                        performer="adumusic",
                        caption=caption,
                        parse_mode='Markdown',
                        reply_markup=get_premium_menu()
                    )
                os.unlink(audio_path)
                await msg.delete()
            except Exception as e:
                await msg.edit_text(f"❌ Error sending audio: {e}")
        else:
            await msg.edit_text(
                f"❌ Sorry, couldn't find '{song_query}'. Please try another song.",
                reply_markup=get_premium_menu()
            )
        return
    
    # If not a song request, use Gemini AI
    if is_mentioned or update.effective_chat.type == "private":
        # Remove "adumusic" mention for cleaner AI prompt
        clean_text = text_lower.replace("adumusic", "").strip()
        if not clean_text:
            clean_text = "Hello"
        
        # Get AI response
        response = await get_gemini_response(clean_text)
        
        # Send with premium menu
        await update.message.reply_text(
            f"🤖 {response}",
            reply_markup=get_premium_menu()
        )

def main():
    application = Application.builder().token(TOKEN).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("switch", switch_mode))
    application.add_handler(CommandHandler("setvideo", set_video))
    application.add_handler(CommandHandler("animate", animate))
    application.add_handler(CommandHandler("del", delete_messages))
    
    # Callback query handler for inline buttons
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
    
    logger.info("adumusic bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
