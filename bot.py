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
# WARNING: Keep your tokens secure. Consider using os.getenv() for both in production.
TOKEN = os.getenv("BOT_TOKEN", "6935043231:AAFSnPWsC8ti9j3npYHFQZU8wABrN5knfDU")
OWNER_ID = int(os.getenv("OWNER_ID", "2119464081"))
GEMINI_API_KEY = "AQ.Ab8RN6KBW9XnJZTH2LP0-s39-BPJHZKVQGrJw42vpGwELUftZA"

DB_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
DB_FILE = os.path.join(DB_PATH, "bot_data.db")

# ---- Configure Gemini ----
genai.configure(api_key=GEMINI_API_KEY)
# Using gemini-1.5-flash or pro for better conversational handling
model = genai.GenerativeModel('gemini-pro') 

# ---- Optional imports ----
try:
    from moviepy.editor import VideoFileClip, TextClip, CompositeVideoClip
    from moviepy.video.fx.all import resize
    MOVIEPY_AVAILABLE = True
except ImportError:
    MOVIEPY_AVAILABLE = False
    print("moviepy not installed - animate feature disabled.")

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False
    print("yt-dlp not installed - music search disabled.")

# ---- Logging ----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---- Database setup ----
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chat_modes (chat_id TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'ai')''')
    c.execute('''CREATE TABLE IF NOT EXISTS admin_videos (chat_id TEXT PRIMARY KEY, file_id TEXT NOT NULL)''')
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
    c.execute("INSERT OR REPLACE INTO chat_modes (chat_id, mode) VALUES (?, ?)", (str(chat_id), mode))
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
    c.execute("INSERT OR REPLACE INTO admin_videos (chat_id, file_id) VALUES (?, ?)", (str(chat_id), file_id))
    conn.commit()
    conn.close()

# ---- YouTube cookies setup ----
def get_youtube_cookies():
    """
    Returns path to cookies file if exists.
    Ensure you place a valid Netscape format cookies.txt in the root directory.
    """
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
    
    cookies = get_youtube_cookies()
    if cookies:
        ydl_opts["cookiefile"] = cookies
        logger.info("Using YouTube cookies for download.")
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if info and "entries" in info and len(info["entries"]) > 0:
                entry = info["entries"][0]
                title = entry.get("title", "song")
                duration = entry.get("duration", 0)
                url = entry.get("webpage_url", "")
                
                # Download the audio
                ydl.extract_info(url, download=True)
                
                # Find the downloaded file
                for f in os.listdir("."):
                    if f.endswith(".mp3") and title[:20].replace("/", "_") in f.replace("/", "_"):
                        return os.path.abspath(f), title, duration
                return None, title, duration
        except Exception as e:
            logger.error(f"Music search error: {e}")
            return None, None, None
    return None, None, None

# ---- Gemini AI integration ----
async def get_gemini_response(question):
    try:
        # Prompt engineered to act as 'adumusic'
        prompt = f"You are a helpful, premium AI chatbot named 'adumusic'. Keep responses concise and engaging.\n\nUser: {question}\n\nadumusic:"
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return "I'm having trouble connecting to my AI core right now. Please try again later."

# ---- Premium inline menu ----
def get_premium_menu():
    """Generates a premium-looking inline keyboard for groups and private chats."""
    keyboard = [
        [
            InlineKeyboardButton("💎 Premium", callback_data="premium_info"),
            InlineKeyboardButton("🎵 Music", callback_data="music_info")
        ],
        [
            InlineKeyboardButton("🤖 AI Features", callback_data="ai_info"),
            InlineKeyboardButton("❓ Help", callback_data="help_info")
        ],
        [
            InlineKeyboardButton("⭐ Upgrade Now", callback_data="upgrade")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ---- Command handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🎵 **adumusic - Premium Bot Experience**\n\n"
        "**How to use me:**\n"
        "• 🗣 **Chat:** Mention `adumusic` in your message to talk to me.\n"
        "• 🎧 **Music:** Type `play [song name]` to download High-Quality audio.\n"
        "• 🎬 **Animate:** `/animate @name` to create a custom video.\n"
    )
    await update.message.reply_text(msg, reply_markup=get_premium_menu(), parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "premium_info":
        msg = "💎 **Premium Features:**\n• Unlimited HQ song downloads\n• Custom AI persona\n• No rate limits\n\n_Experience the best of adumusic._"
    elif query.data == "music_info":
        msg = "🎵 **Music Search Engine:**\nJust say `play [song]` or `find [song]` and I'll securely download it using premium protocols."
    elif query.data == "ai_info":
        msg = "🤖 **AI Intelligence:**\nCall my name! Just include `adumusic` in your message, and I'll respond using advanced Gemini architecture."
    elif query.data == "help_info":
        msg = "❓ **Help Center:**\n• AI: Must say 'adumusic'\n• Music: Must use 'play' or 'search' prefix\n• Issues? Contact the admin."
    elif query.data == "upgrade":
        msg = "⭐ **Upgrade to Premium:**\nContact your local admin to unlock all adumusic features!"
    
    await query.edit_message_text(msg, reply_markup=get_premium_menu(), parse_mode='Markdown')

async def animate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Simplified placeholder for animation logic to keep script focused
    await update.message.reply_text("Animation sequence initiated... (Make sure background is set via /setvideo)")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return
    
    text_lower = text.lower()

    # 1. Check for Song Requests First
    # Matches: play <song>, search <song>, find <song>
    song_pattern = r'(?i)^(?:play|search|find|send me|download)\s+(.+)'
    match = re.match(song_pattern, text)
    
    if match:
        song_query = match.group(1).strip()
        msg = await update.message.reply_text("🔍 *Searching premium databases...*", parse_mode='Markdown')
        
        audio_path, title, duration = await search_and_download_audio(song_query)
        
        if audio_path and os.path.exists(audio_path):
            try:
                caption = f"🎵 **{title}**\n⏱ {duration//60}:{duration%60:02d} | 🔊 Premium Quality\n\n🎧 _Provided by adumusic_"
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
                await msg.edit_text(f"❌ Could not upload file: {e}")
        else:
            await msg.edit_text("❌ Song not found. Try a different search.", reply_markup=get_premium_menu())
        
        # IMPORTANT: Return here so AI does not reply to song requests
        return

    # 2. Check for AI Mentions (if not a song request)
    # The bot will ONLY reply if "adumusic" is in the text, OR if the user is directly replying to the bot.
    is_mentioned = "adumusic" in text_lower
    is_reply_to_bot = (
        update.message.reply_to_message and 
        update.message.reply_to_message.from_user.id == context.bot.id
    )

    if is_mentioned or is_reply_to_bot:
        # Clean the text to avoid confusing the AI with its own name
        clean_text = text_lower.replace("adumusic", "").strip()
        if not clean_text:
            clean_text = "Hello!"
            
        # Optional: Send a typing action while Gemini thinks
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=telegram.constants.ChatAction.TYPING)
        
        response = await get_gemini_response(clean_text)
        
        await update.message.reply_text(
            f"✨ {response}",
            reply_markup=get_premium_menu()
        )

def main():
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("animate", animate))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Catch all text messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
    
    logger.info("adumusic bot is active and listening...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
