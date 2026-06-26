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
    print("yt-dlp not installed – YouTube music disabled.")

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
        logger.info(f"Searching JioSaavan for: {query}")
        async with aiohttp.ClientSession() as session:
            # Search API
            search_url = f"{API_BASE_URL}/search?query={query}"
            logger.info(f"JioSaavan API URL: {search_url}")
            
            async with session.get(search_url) as resp:
                logger.info(f"JioSaavan response status: {resp.status}")
                
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"JioSaavan response data: {json.dumps(data)[:200]}")
                    
                    # Check for songs
                    if data and 'data' in data:
                        songs = data['data'].get('songs', data['data'].get('results', []))
                        if songs and len(songs) > 0:
                            song = songs[0] if isinstance(songs, list) else songs
                            song_name = song.get('name', song.get('title', 'Unknown'))
                            song_url = song.get('url', song.get('permalink_url', ''))
                            
                            logger.info(f"Found song on JioSaavan: {song_name}")
                            
                            # Try to get download URL
                            if song_url:
                                download_url = f"{API_BASE_URL}/download?url={song_url}"
                                async with session.get(download_url) as dl_resp:
                                    if dl_resp.status == 200:
                                        dl_data = await dl_resp.json()
                                        audio_url = dl_data.get('url', dl_data.get('download_url', ''))
                                        if audio_url:
                                            return {
                                                'title': song_name,
                                                'url': audio_url,
                                                'source': 'JioSaavan'
                                            }
                            
                            # Return song URL if download failed
                            return {
                                'title': song_name,
                                'url': song_url,
                                'source': 'JioSaavan'
                            }
    except Exception as e:
        logger.error(f"JioSaavan API error: {e}")
    return None

# ---- YouTube Music Search ----
async def search_youtube_audio(query):
    """Search music using YouTube"""
    if not YTDLP_AVAILABLE:
        logger.info("yt-dlp not available, skipping YouTube search")
        return None
    
    try:
        logger.info(f"Searching YouTube for: {query}")
        
        # Create temp directory for downloads
        temp_dir = tempfile.mkdtemp()
        original_dir = os.getcwd()
        os.chdir(temp_dir)
        
        ydl_opts = {
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "quiet": True,
            "no_warnings": True,
            "outtmpl": "%(title)s.%(ext)s",
            "default_search": "ytsearch1",
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # First search without downloading
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            
            if info and "entries" in info and len(info["entries"]) > 0:
                entry = info["entries"][0]
                title = entry.get("title", "song")
                duration = entry.get("duration", 0)
                url = entry.get("webpage_url", "")
                
                logger.info(f"Found on YouTube: {title}")
                
                # Download the audio
                info = ydl.extract_info(url, download=True)
                
                # Find the downloaded file
                for f in os.listdir("."):
                    if f.endswith(".mp3"):
                        file_path = os.path.abspath(f)
                        logger.info(f"YouTube download complete: {file_path}")
                        
                        os.chdir(original_dir)
                        return {
                            'title': title,
                            'path': file_path,
                            'duration': duration,
                            'source': 'YouTube'
                        }
                
        os.chdir(original_dir)
        
    except Exception as e:
        logger.error(f"YouTube search error: {e}")
        try:
            os.chdir(original_dir)
        except:
            pass
    
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
    elif query.data == "music_info":
        msg = "🎵 **Music Search:**\nUse 'find song_name' or 'search song_name' to get instant audio files!"
    elif query.data == "ai_info":
        msg = "🤖 **AI Chat:**\nMention 'adumusic' to chat with AI. I can help with anything using Gemini AI!"
    elif query.data == "set_video_info":
        if user_id == ADMIN_ID:
            msg = "🎬 **Set Background Video:**\n1. Reply to a video with /setvideo\n2. Or upload video and use command"
        else:
            msg = "🎬 **Set Background Video:**\nOnly admin can set the background video.\nContact @Xricx0 for access."
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
    
    if user.id != ADMIN_ID:
        await update.message.reply_text("❌ Only bot admin can set the video.")
        return
    
    if update.message.reply_to_message and update.message.reply_to_message.video:
        file_id = update.message.reply_to_message.video.file_id
        set_admin_video(chat.id, file_id)
        await update.message.reply_text(
            "✅ Background video updated successfully!",
            reply_markup=get_premium_menu()
        )
    else:
        await update.message.reply_text(
            "📹 Reply to a video with /setvideo to set it as background.",
            reply_markup=get_premium_menu()
        )

async def delete_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
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
    
    await update.message.delete()
    await update.message.reply_text(f"🗑 Deleted {count} messages successfully!")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    if not text:
        return
    
    text_lower = text.lower()
    logger.info(f"Received message: {text}")
    
    # SONG SEARCH - Keywords: find, search, play
    song_search_pattern = r'(?i)^(find|search|play)\s+(.+)'
    match = re.match(song_search_pattern, text)
    
    if match:
        command = match.group(1).lower()
        song_query = match.group(2).strip()
        
        logger.info(f"Song search requested: command={command}, query={song_query}")
        
        if song_query:
            # Send searching message
            status_msg = await update.message.reply_text(
                f"🔍 Searching for '{song_query}'...",
                reply_markup=get_premium_menu()
            )
            
            # Try JioSaavan first
            logger.info("Trying JioSaavan API...")
            result = await search_jiosaavan(song_query)
            
            if result and 'url' in result:
                logger.info(f"JioSaavan result: {result}")
                try:
                    # Try to send as audio file
                    await update.message.reply_audio(
                        audio=result['url'],
                        title=result['title'],
                        performer="adumusic",
                        caption=f"🎵 {result['title']}\n📡 Source: JioSaavan",
                        reply_markup=get_premium_menu()
                    )
                    await status_msg.delete()
                    return
                except Exception as e:
                    logger.error(f"Failed to send JioSaavan audio: {e}")
                    # If can't send as audio, send as link
                    await status_msg.edit_text(
                        f"🎵 **{result['title']}**\n📡 Source: JioSaavan\n\n🔗 [Click to listen]({result['url']})",
                        parse_mode='Markdown',
                        reply_markup=get_premium_menu(),
                        disable_web_page_preview=False
                    )
                    return
            
            # Fallback to YouTube
            logger.info("Trying YouTube...")
            result = await search_youtube_audio(song_query)
            
            if result and 'path' in result and os.path.exists(result['path']):
                logger.info(f"YouTube result: {result}")
                try:
                    with open(result['path'], "rb") as f:
                        await update.message.reply_audio(
                            audio=f,
                            title=result['title'],
                            performer="adumusic",
                            caption=f"🎵 {result['title']}\n📡 Source: YouTube",
                            reply_markup=get_premium_menu()
                        )
                    os.unlink(result['path'])
                    await status_msg.delete()
                    return
                except Exception as e:
                    logger.error(f"Failed to send YouTube audio: {e}")
            
            # If both failed
            await status_msg.edit_text(
                f"❌ Sorry, couldn't find '{song_query}'.\nPlease try:\n• Different spelling\n• Artist name + song name\n• Only the song name",
                reply_markup=get_premium_menu()
            )
        return
    
    # AI CHAT - Only respond when "adumusic" is mentioned
    is_mentioned = "adumusic" in text_lower
    
    if is_mentioned or update.effective_chat.type == "private":
        clean_text = re.sub(r'(?i)adumusic\s*', '', text).strip()
        if not clean_text:
            clean_text = "Hello"
        
        logger.info(f"AI request: {clean_text}")
        response = await get_gemini_response(clean_text)
        
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
    application.add_handler(CommandHandler("del", delete_messages))
    
    # Callback query handler
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
    
    logger.info("adumusic bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
