import telebot
from telebot import types
import os
import re
import json
import sqlite3
import logging
import tempfile
import threading
import time
from io import BytesIO
from PIL import Image, ImageFilter, ImageDraw
import requests
import subprocess

# ==================== CONFIG ====================
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"          # Replace with your bot token
ADMIN_IDS = [123456789]                    # Replace with admin user IDs

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('musicbot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ==================== BOT INIT ====================
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=4)

# ==================== DATABASE ====================
DB = 'musicbot.db'

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cache
                 (url TEXT PRIMARY KEY, file_id TEXT, title TEXT, performer TEXT, duration INTEGER)''')
    # Default admin video (empty)
    c.execute("INSERT OR IGNORE INTO settings VALUES ('video_file_id', '')")
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_cached_file(url):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT file_id, title, performer, duration FROM cache WHERE url=?", (url,))
    row = c.fetchone()
    conn.close()
    return row

def cache_file(url, file_id, title, performer, duration):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?, ?)",
              (url, file_id, title, performer, duration))
    conn.commit()
    conn.close()

# ==================== RATE LIMITER ====================
user_last_request = {}
RATE_LIMIT = 1  # seconds between requests

def is_limited(user_id):
    now = time.time()
    if user_id in user_last_request and now - user_last_request[user_id] < RATE_LIMIT:
        return True
    user_last_request[user_id] = now
    return False

# ==================== HELPERS ====================
def download_audio(url):
    """Download best audio from YouTube URL, return file path and metadata."""
    with tempfile.TemporaryDirectory() as tmpdir:
        outtmpl = os.path.join(tmpdir, '%(title)s.%(ext)s')
        cmd = [
            'yt-dlp',
            '-f', 'bestaudio[ext=m4a]/bestaudio',
            '--extract-audio',
            '--audio-format', 'm4a',
            '--output', outtmpl,
            '--print', 'title',
            '--print', 'uploader',
            '--print', 'duration',
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise Exception("yt-dlp failed")
        lines = result.stdout.strip().split('\n')
        title = lines[-3] if len(lines) >= 3 else "Unknown"
        uploader = lines[-2] if len(lines) >= 2 else "Unknown"
        duration = int(lines[-1]) if lines[-1].isdigit() else 0
        # find the downloaded file
        files = os.listdir(tmpdir)
        audio_file = next((f for f in files if f.endswith('.m4a')), None)
        if not audio_file:
            raise Exception("No audio file")
        # Read bytes and return
        with open(os.path.join(tmpdir, audio_file), 'rb') as f:
            audio_data = f.read()
        return audio_data, title, uploader, duration

def create_glass_thumbnail(thumb_url):
    """Download thumbnail, apply glass effect, return byte stream."""
    try:
        resp = requests.get(thumb_url, timeout=5)
        img = Image.open(BytesIO(resp.content)).convert('RGB')
        # Resize to 320x320
        img = img.resize((320, 320), Image.LANCZOS)
        # Create blurred background
        blurred = img.filter(ImageFilter.GaussianBlur(radius=12))
        # Create a semi-transparent white overlay
        overlay = Image.new('RGBA', (320, 320), (255, 255, 255, 80))
        # Composite original over blurred, then paste glass overlay
        glass = Image.alpha_composite(blurred.convert('RGBA'), overlay)
        # Add subtle border
        draw = ImageDraw.Draw(glass)
        draw.rounded_rectangle([(2,2),(317,317)], outline=(255,255,255,100), width=3, radius=20)
        # Save to bytes
        bio = BytesIO()
        glass.save(bio, 'PNG')
        bio.seek(0)
        return bio
    except Exception as e:
        logger.error(f"Thumbnail error: {e}")
        return None

# ==================== INLINE QUERIES ====================
@bot.inline_handler(lambda query: len(query.query) > 0)
def inline_search(inline_query):
    if is_limited(inline_query.from_user.id):
        return
    q = inline_query.query
    # Fast search with flat playlist (no full metadata)
    cmd = ['yt-dlp', f'ytsearch5:{q}', '--flat-playlist', '--dump-json', '--no-warnings']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        entries = []
        for line in result.stdout.strip().split('\n'):
            if line:
                data = json.loads(line)
                entries.append(data)
    except Exception as e:
        logger.error(f"Inline search error: {e}")
        return

    results = []
    for idx, entry in enumerate(entries):
        vid = entry.get('id')
        url = f'https://youtu.be/{vid}' if vid else entry.get('webpage_url', '')
        title = entry.get('title', 'Unknown')
        uploader = entry.get('uploader', entry.get('channel', 'Unknown'))
        duration = entry.get('duration', 0) or 0
        thumb = entry.get('thumbnails', [{}])[-1].get('url', '') if entry.get('thumbnails') else ''
        desc = f"{uploader} • {duration//60}:{duration%60:02d}"
        results.append(types.InlineQueryResultArticle(
            id=str(idx),
            title=title,
            description=desc,
            input_message_content=types.InputTextMessageContent(
                f"🔍 {title} - {uploader}"
            ),
            thumb_url=thumb,
            thumb_width=48, thumb_height=48
        ))
    bot.answer_inline_query(inline_query.id, results, cache_time=5)

@bot.chosen_inline_handler(func=lambda chosen_inline_result: True)
def chosen_inline(chosen_result):
    # When user selects a song from inline results, download and send audio
    # We need to parse the message text to extract title and search again? Better: store url in callback_data.
    # Since we can't pass custom data directly in InputTextMessageContent, we'll use a small hack:
    # In the inline results we could use a callback_data via InlineQueryResultAudio? Not allowed.
    # Alternative: when chosen, bot sends a message first, then downloads. But we need url.
    # We'll search again using the title from the text (quick and dirty).
    query = chosen_result.query
    # Re-run search with yt-dlp to get the URL
    cmd = ['yt-dlp', f'ytsearch1:{query}', '--get-url', '--no-warnings']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        url = result.stdout.strip().split('\n')[0]
        if not url:
            return
    except:
        return
    # Download and send
    send_audio(chosen_result.from_user.id, url, reply_to=None)

def send_audio(chat_id, url, reply_to=None):
    """Download audio from URL and send to chat with glass thumbnail."""
    # Check cache first
    cached = get_cached_file(url)
    if cached:
        file_id, title, performer, duration = cached
        try:
            bot.send_audio(chat_id, file_id, caption=f"🎵 {title} - {performer}", reply_to_message_id=reply_to)
            return
        except:
            pass  # re-download if file id expired

    # Download
    try:
        audio_data, title, uploader, duration = download_audio(url)
    except Exception as e:
        logger.error(f"Download failed: {e}")
        bot.send_message(chat_id, "❌ Failed to download the audio.")
        return

    # Get thumbnail
    thumb_url = f"https://i.ytimg.com/vi/{url.split('v=')[-1]}/hqdefault.jpg"
    thumb_io = create_glass_thumbnail(thumb_url)

    # Send audio
    audio_file = BytesIO(audio_data)
    audio_file.name = f"{title}.m4a"
    msg = bot.send_audio(
        chat_id,
        audio_file,
        caption=f"🎵 {title} - {uploader}",
        duration=duration,
        performer=uploader,
        title=title,
        thumb=thumb_io,
        reply_to_message_id=reply_to
    )
    # Cache file_id
    if msg.audio:
        cache_file(url, msg.audio.file_id, title, uploader, duration)
    logger.info(f"Sent audio: {title}")

# ==================== COMMAND HANDLERS ====================
@bot.message_handler(commands=['start'])
def start(message):
    # Send admin's video (if any)
    video_id = get_setting('video_file_id')
    if video_id:
        bot.send_video(message.chat.id, video_id, caption="🎬 Welcome to Music Bot!")
    # Menu
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔍 Search", switch_inline_query_current_chat=""),
        types.InlineKeyboardButton("❓ Help", callback_data='help')
    )
    bot.send_message(
        message.chat.id,
        "🎶 **Music Bot**\n"
        "Use inline search: `@botusername song`\n"
        "Or /search <song name>",
        reply_markup=markup,
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['search'])
def search_cmd(message):
    if is_limited(message.from_user.id):
        return
    query = message.text.split(' ', 1)[1] if len(message.text.split()) > 1 else ''
    if not query:
        bot.reply_to(message, "Usage: /search <song name>")
        return
    # Search and show results as inline buttons
    cmd = ['yt-dlp', f'ytsearch5:{query}', '--flat-playlist', '--dump-json', '--no-warnings']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        entries = []
        for line in result.stdout.strip().split('\n'):
            if line:
                entries.append(json.loads(line))
    except:
        bot.reply_to(message, "❌ Search failed.")
        return

    if not entries:
        bot.reply_to(message, "No results.")
        return

    markup = types.InlineKeyboardMarkup()
    for entry in entries:
        title = entry.get('title', 'Unknown')[:50]
        vid = entry.get('id')
        url = f'https://youtu.be/{vid}' if vid else entry.get('webpage_url')
        if url:
            markup.add(types.InlineKeyboardButton(
                title,
                callback_data=f"play:{url}"
            ))
    bot.reply_to(message, "🎵 Search results:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('play:'))
def play_callback(call):
    if is_limited(call.from_user.id):
        bot.answer_callback_query(call.id, "Too fast!")
        return
    url = call.data.split('play:')[1]
    bot.answer_callback_query(call.id, "Downloading...")
    send_audio(call.message.chat.id, url, reply_to=call.message.message_id)

@bot.message_handler(commands=['set_video'])
def set_video(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "⛔ Admin only.")
        return
    # Check if message has a video reply
    if not message.reply_to_message or not message.reply_to_message.video:
        bot.reply_to(message, "Reply to a video with /set_video to set it.")
        return
    file_id = message.reply_to_message.video.file_id
    set_setting('video_file_id', file_id)
    bot.reply_to(message, "✅ Video frame updated!")

@bot.message_handler(commands=['help'])
def help_cmd(message):
    bot.reply_to(message,
        "📖 **Music Bot Help**\n"
        "• Use inline search: `@botusername song`\n"
        "• /search <song> – list results\n"
        "• /set_video – admin only, set a welcome video\n"
        "• All songs are from YouTube.",
        parse_mode='Markdown')

# ==================== MAIN ====================
if __name__ == '__main__':
    init_db()
    logger.info("Music bot started.")
    bot.infinity_polling()
