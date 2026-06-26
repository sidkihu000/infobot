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
import aiohttp

# ---- Configuration ----
TOKEN = os.getenv("BOT_TOKEN", "6935043231:AAFSnPWsC8ti9j3npYHFQZU8wABrN5knfDU")
OWNER_ID = int(os.getenv("OWNER_ID", "2119464081"))
ADMIN_ID = 2119464081
GEMINI_API_KEY = "AQ.Ab8RN6KBW9XnJZTH2LP0-s39-BPJHZKVQGrJw42vpGwELUftZA"
API_BASE_URL = "https://jiosavanapiryden.vercel.app/api"
SUPPORT_LINK = "https://t.me/Xricx0"
CHANNEL_LINK = ""
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
    print("yt-dlp not installed – YouTube music disabled, using JioSaavan API.")

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

# ---- JioSaavan API Music Search ----
async def search_jiosaavan(query):
    """Search music using JioSaavan API"""
    try:
        async with aiohttp.ClientSession() as session:
            # Search API
            search_url = f"{API_BASE_URL}/search?query={query}"
            async with session.get(search_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    # Try to get first song result
                    if data and 'songs' in data and len(data['songs']) > 0:
                        song = data['songs'][0]
                        song_name = song.get('name', 'Unknown')
                        song_url = song.get('url', '')
                        
                        # Get download URL
                        if song_url:
                            download_url = f"{API_BASE_URL}/download?url={song_url}"
                            async with session.get(download_url) as dl_resp:
                                if dl_resp.status == 200:
                                    dl_data = await dl_resp.json()
                                    if dl_data and 'url' in dl_data:
                                        return {
                                            'title': song_name,
                                            'url': dl_data['url'],
                                            'source': 'JioSaavan'
                                        }
                    
                    # Try albums
                    elif data and 'albums' in data and len(data['albums']) > 0:
                        album = data['albums'][0]
                        return {
                            'title': album.get('name', 'Unknown'),
                            'url': album.get('url', ''),
                            'source': 'JioSaavan',
                            'type': 'album'
                        }
    except Exception as e:
        logger.error(f"JioSaavan API error: {e}")
    return None

# ---- YouTube Music Search ----
async def search_youtube_audio(query):
    """Search music using YouTube"""
    if not YTDLP_AVAILABLE:
        return None
    
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
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=True)
            if info and "entries" in info and len(info["entries"]) > 0:
                entry = info["entries"][0]
                title = entry.get("title", "song")
                duration = entry.get("duration", 0)
                
                # Find downloaded file
                for f in os.listdir("."):
                    if f.endswith(".mp3") and title[:20].replace(" ", "_") in f.replace(" ", "_"):
                        return {
                            'title': title,
                            'path': os.path.abspath(f),
                            'duration': duration,
                            'source': 'YouTube'
                        }
    except Exception as e:
        logger.error(f"YouTube search error: {e}")
    return None

# ---- Main Music Search Function ----
async def search_music(query):
    """Search music with fallback chain"""
    # Try JioSaavan first
    result = await search_jiosaavan(query)
    if result and 'url' in result:
        return result
    
    # Fallback to YouTube
    youtube_result = await search_youtube_audio(query)
    if youtube_result:
        return youtube_result
    
    return None

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
            InlineKeyboardButton("🎵 Music", callback_data="music_info")
        ],
        [
            InlineKeyboardButton("🤖 AI Chat", callback_data="ai_info"),
            InlineKeyboardButton("🎬 Set Video", callback_data="set_video_info")
        ],
        [
            InlineKeyboardButton("ℹ️ Support", url=SUPPORT_LINK),
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
    msg += f"Current Mode: {mode.upper()}\n"
    msg += f"Music Source: JioSaavan + YouTube\n\n"
    msg += "**Commands:**\n"
    msg += "• Use 'find song_name' or 'search song_name'\n"
    msg += "• Mention 'adumusic' to chat with AI\n"
    msg += "• /switch - Toggle AI/Music mode\n"
    msg += "• /animate @name - Create name video\n"
    msg += "• Reply to video + /setvideo - Set bg video\n"
    
    if video:
        await update.message.reply_video(
            video,
            caption=msg,
            reply_markup=get_premium_menu(),
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            msg,
            reply_markup=get_premium_menu(),
            parse_mode='Markdown'
        )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "premium_info":
        msg = "💎 **Premium Features:**\n• Unlimited songs\n• HD audio quality\n• Priority support\n• No ads\n\nPrice: $5/month"
        await query.edit_message_text(msg, reply_markup=get_premium_menu(), parse_mode='Markdown')
    
    elif query.data == "music_info":
        msg = "🎵 **Music Search:**\nUse 'find song_name' or 'search song_name' to get instant audio files!"
        await query.edit_message_text(msg, reply_markup=get_premium_menu(), parse_mode='Markdown')
    
    elif query.data == "ai_info":
        msg = "🤖 **AI Chat:**\nMention 'adumusic' to chat with AI. I can help with anything using Gemini AI!"
        await query.edit_message_text(msg, reply_markup=get_premium_menu(), parse_mode='Markdown')
    
    elif query.data == "set_video_info":
        if user_id == ADMIN_ID:
            msg = "🎬 **Set Background Video:**\n1. Reply to a video with /setvideo\n2. Or upload video and use command\n\nThis will be used for name animations!"
        else:
            msg = "🎬 **Set Background Video:**\nOnly admin can set the background video.\nContact @Xricx0 for access."
        await query.edit_message_text(msg, reply_markup=get_premium_menu(), parse_mode='Markdown')
    
    elif query.data == "help_info":
        msg = "❓ **Help:**\nJust type 'find song_name' or mention 'adumusic' for AI responses."
        await query.edit_message_text(msg, reply_markup=get_premium_menu(), parse_mode='Markdown')
    
    elif query.data == "upgrade":
        msg = "⭐ **Upgrade to Premium:**\nContact @Xricx0 to upgrade!"
        await query.edit_message_text(msg, reply_markup=get_premium_menu(), parse_mode='Markdown')

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
    
    # Only admin can set video
    if user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only bot admin can set the video.")
        return
    
    if update.message.reply_to_message and update.message.reply_to_message.video:
        file_id = update.message.reply_to_message.video.file_id
        set_admin_video(chat.id, file_id)
        await update.message.reply_text(
            "✅ Background video updated successfully!\nUse /animate @name to create animations.",
            reply_markup=get_premium_menu()
        )
    else:
        await update.message.reply_text(
            "📹 **Set Background Video:**\n\n1. Upload a video\n2. Reply to it with /setvideo\n\nThis video will be used for name animations.",
            reply_markup=get_premium_menu()
        )

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
        await update.message.reply_text(
            "Admin has not set a background video.\nUse /setvideo to set one.",
            reply_markup=get_premium_menu()
        )
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
                await update.message.reply_video(
                    video=f,
                    caption=f"🎬 Animated for {name}\n🎧 _Powered by adumusic_",
                    reply_markup=get_premium_menu(),
                    parse_mode='Markdown'
                )
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
    
    # Only admin can delete
    if user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only admin can delete messages.")
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
    
    text_lower = text.lower()
    
    # DIRECT SONG SEARCH - Keywords: find, search
    # This pattern matches: "find song_name", "search song_name", "find artist song", etc.
    song_search_pattern = r'(?i)^(find|search)\s+(.+)'
    match = re.match(song_search_pattern, text)
    
    if match:
        song_query = match.group(2).strip()
        
        if song_query:
            # Send audio directly without any text message
            try:
                # First try JioSaavan for quick results
                jiosaavan_result = await search_jiosaavan(song_query)
                
                if jiosaavan_result and 'url' in jiosaavan_result:
                    # JioSaavan - send as audio link
                    await update.message.reply_audio(
                        audio=jiosaavan_result['url'],
                        title=jiosaavan_result['title'],
                        performer="adumusic",
                        reply_markup=get_premium_menu()
                    )
                    return
                
                # Fallback to YouTube
                youtube_result = await search_youtube_audio(song_query)
                if youtube_result and 'path' in youtube_result and os.path.exists(youtube_result['path']):
                    with open(youtube_result['path'], "rb") as f:
                        await update.message.reply_audio(
                            audio=f,
                            title=youtube_result['title'],
                            performer="adumusic",
                            reply_markup=get_premium_menu()
                        )
                    os.unlink(youtube_result['path'])
                    return
                
                # If both fail, send a subtle error message
                await update.message.reply_text(
                    f"❌ Could not find '{song_query}'",
                    reply_markup=get_premium_menu()
                )
                
            except Exception as e:
                logger.error(f"Audio send error: {e}")
                await update.message.reply_text(
                    "❌ Error processing your request",
                    reply_markup=get_premium_menu()
                )
        return
    
    # AI CHAT - Only respond when "adumusic" is mentioned
    is_mentioned = "adumusic" in text_lower
    
    if is_mentioned or update.effective_chat.type == "private":
        # Remove "adumusic" mention for cleaner AI prompt
        clean_text = re.sub(r'(?i)adumusic\s*', '', text).strip()
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
    
    logger.info("adumusic bot is starting with instant audio delivery...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
