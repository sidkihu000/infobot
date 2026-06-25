import telebot
from telebot import types
import os
import re
import json
import sqlite3
import logging
import tempfile
import time
from io import BytesIO
from PIL import Image, ImageFilter, ImageDraw
import requests
import subprocess

# ==================== CONFIG ====================
BOT_TOKEN = "6935043231:AAFSnPWsC8ti9j3npYHFQZU8wABrN5knfDU"
ADMIN_IDS = [2119464081]

COOKIES_FILE = "cookies.txt"   # YouTube login cookies

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('musicbot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ==================== GEMINI AI ====================
GEMINI_API_KEY = "AIzaSyAXERqkAEErXF7-4qSlap6tO9QSSmJmpf0"
USE_AI = False
if GEMINI_API_KEY:
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        USE_AI = True
        logger.info("Gemini AI enabled (new google-genai).")
    except ImportError:
        try:
            import google.generativeai as genai_old
            genai_old.configure(api_key=GEMINI_API_KEY)
            model_old = genai_old.GenerativeModel('gemini-1.5-flash')
            USE_AI = True
            logger.info("Gemini AI enabled (legacy library).")
        except ImportError:
            logger.warning("No Gemini library – AI disabled.")
else:
    logger.info("No Gemini key – simple replies.")

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
RATE_LIMIT = 1

def is_limited(user_id):
    now = time.time()
    if user_id in user_last_request and now - user_last_request[user_id] < RATE_LIMIT:
        return True
    user_last_request[user_id] = now
    return False

# ==================== AUDIO DOWNLOAD (FIXED FORMAT) ====================
def download_audio(url):
    with tempfile.TemporaryDirectory() as tmpdir:
        outtmpl = os.path.join(tmpdir, '%(title)s.%(ext)s')
        cmd = [
            'yt-dlp',
            '-f', 'bestaudio',          # Removed [ext=m4a] to avoid missing format error
            '--extract-audio',
            '--audio-format', 'm4a',
            '--output', outtmpl,
            '--print', 'title',
            '--print', 'uploader',
            '--print', 'duration',
            '--no-playlist',
            url
        ]
        if os.path.isfile(COOKIES_FILE):
            cmd.insert(1, '--cookies')
            cmd.insert(2, COOKIES_FILE)
            logger.info("Using cookies.txt for yt-dlp.")
        else:
            logger.warning("cookies.txt not found – may hit bot detection.")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            error_msg = result.stderr.strip().split('\n')[-1] if result.stderr else "Unknown yt-dlp error"
            raise Exception(f"yt-dlp failed: {error_msg}")
        lines = result.stdout.strip().split('\n')
        title = lines[-3] if len(lines) >= 3 else "Unknown"
        uploader = lines[-2] if len(lines) >= 2 else "Unknown"
        duration = int(lines[-1]) if lines[-1].isdigit() else 0
        files = os.listdir(tmpdir)
        audio_file = next((f for f in files if f.endswith('.m4a')), None)
        if not audio_file:
            raise Exception("No audio file created")
        with open(os.path.join(tmpdir, audio_file), 'rb') as f:
            audio_data = f.read()
        return audio_data, title, uploader, duration

def create_glass_thumbnail(thumb_url):
    try:
        resp = requests.get(thumb_url, timeout=5)
        img = Image.open(BytesIO(resp.content)).convert('RGB')
        img = img.resize((320, 320), Image.LANCZOS)
        blurred = img.filter(ImageFilter.GaussianBlur(radius=12))
        overlay = Image.new('RGBA', (320, 320), (255, 255, 255, 80))
        glass = Image.alpha_composite(blurred.convert('RGBA'), overlay)
        draw = ImageDraw.Draw(glass)
        draw.rounded_rectangle([(2,2),(317,317)], outline=(255,255,255,100), width=3, radius=20)
        bio = BytesIO()
        glass.save(bio, 'PNG')
        bio.seek(0)
        return bio
    except Exception as e:
        logger.error(f"Thumbnail error: {e}")
        return None

def search_songs(query, limit=5):
    cmd = ['yt-dlp', f'ytsearch{limit}:{query}', '--flat-playlist', '--dump-json', '--no-warnings']
    if os.path.isfile(COOKIES_FILE):
        cmd.insert(1, '--cookies')
        cmd.insert(2, COOKIES_FILE)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        entries = []
        for line in result.stdout.strip().split('\n'):
            if line:
                data = json.loads(line)
                vid = data.get('id')
                url = f'https://youtu.be/{vid}' if vid else data.get('webpage_url', '')
                title = data.get('title', 'Unknown')
                uploader = data.get('uploader', data.get('channel', 'Unknown'))
                duration = data.get('duration', 0) or 0
                thumb = (data.get('thumbnails', [{}])[-1].get('url', '') if data.get('thumbnails') else '')
                entries.append({
                    'title': title,
                    'uploader': uploader,
                    'url': url,
                    'duration': duration,
                    'thumb': thumb
                })
        return entries
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

def send_audio(chat_id, url, reply_to=None):
    cached = get_cached_file(url)
    if cached:
        file_id, title, performer, duration = cached
        try:
            bot.send_audio(chat_id, file_id, caption=f"🎵 {title} - {performer}", reply_to_message_id=reply_to)
            return
        except:
            pass

    try:
        audio_data, title, uploader, duration = download_audio(url)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Download failed: {error_msg}")
        bot.send_message(chat_id, f"❌ Failed to download.\n\n`{error_msg}`", parse_mode='Markdown')
        return

    thumb_url = ""
    if 'youtu' in url:
        vid = url.split('v=')[-1].split('&')[0] if 'v=' in url else url.split('/')[-1]
        thumb_url = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    thumb_io = create_glass_thumbnail(thumb_url) if thumb_url else None

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
    if msg.audio:
        cache_file(url, msg.audio.file_id, title, uploader, duration)
    logger.info(f"Sent audio: {title}")

# ==================== AI CHAT ====================
def ai_chat(user_name, user_message):
    if not USE_AI:
        return None
    prompt = f"""You are a friendly Hindi/English music assistant bot.
User name: {user_name}
User says: {user_message}
Reply naturally in Hinglish or English, keep it short.
If the user asks for a song, say something like "Main aapke liye gaana dhundh raha hoon!".
Now reply:"""
    try:
        if 'client' in globals():
            response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
            return response.text.strip()
        else:
            response = model_old.generate_content(prompt)
            return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return None

def simple_reply(user_name, text):
    text_lower = text.lower()
    if any(kw in text_lower for kw in ['song', 'gaana', 'gana', 'play', 'music', 'geet']):
        song = re.sub(r'(play|search|song|gaana|gana|music|geet)', '', text, flags=re.IGNORECASE).strip()
        if song:
            return f"🎵 Main aapke liye **{song}** dhundh raha hoon !!"
        return "🎵 Aapko kaunsa gaana chahiye?"
    return f"Hello {user_name}, main aapki kya madad kar sakta hoon? 🎶"

# ==================== INLINE QUERIES ====================
@bot.inline_handler(lambda query: len(query.query) > 0)
def inline_search(inline_query):
    if is_limited(inline_query.from_user.id):
        return
    q = inline_query.query
    entries = search_songs(q, 5)
    results = []
    for idx, entry in enumerate(entries):
        duration_str = f"{entry['duration']//60}:{entry['duration']%60:02d}"
        results.append(types.InlineQueryResultArticle(
            id=str(idx),
            title=entry['title'],
            description=f"{entry['uploader']} • {duration_str}",
            input_message_content=types.InputTextMessageContent(
                f"🔍 {entry['title']} - {entry['uploader']}"
            ),
            thumb_url=entry['thumb'],
            thumb_width=48, thumb_height=48
        ))
    bot.answer_inline_query(inline_query.id, results, cache_time=5)

@bot.chosen_inline_handler(func=lambda chosen_inline_result: True)
def chosen_inline(chosen_result):
    query = chosen_result.query
    entries = search_songs(query, 1)
    if entries:
        send_audio(chosen_result.from_user.id, entries[0]['url'])

# ==================== COMMANDS ====================
@bot.message_handler(commands=['start'])
def start(message):
    first_name = message.from_user.first_name
    video_id = get_setting('video_file_id')
    if video_id:
        bot.send_video(message.chat.id, video_id, caption="🎬 Welcome to Music Bot!")

    greeting = ai_chat(first_name, "/start") if USE_AI else simple_reply(first_name, "hello")
    if not greeting:
        greeting = f"Hey {first_name}, how can I help you? 🎵"

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔍 Search", switch_inline_query_current_chat=""),
        types.InlineKeyboardButton("❓ Help", callback_data='help')
    )
    bot.send_message(message.chat.id, greeting, reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(commands=['search'])
def search_cmd(message):
    query = message.text.split(' ', 1)[1] if len(message.text.split()) > 1 else ''
    if not query:
        bot.reply_to(message, "Usage: /search <song name>")
        return
    entries = search_songs(query, 5)
    if not entries:
        bot.reply_to(message, "No results.")
        return
    markup = types.InlineKeyboardMarkup()
    for entry in entries:
        markup.add(types.InlineKeyboardButton(
            f"{entry['title'][:50]}", callback_data=f"play:{entry['url']}"
        ))
    reply_text = ai_chat(message.from_user.first_name, query) if USE_AI else simple_reply(message.from_user.first_name, query)
    if reply_text:
        bot.reply_to(message, reply_text)
    bot.send_message(message.chat.id, "🎵 Search results:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('play:'))
def play_callback(call):
    if is_limited(call.from_user.id):
        bot.answer_callback_query(call.id, "Too fast!")
        return
    url = call.data.split('play:')[1]
    bot.answer_callback_query(call.id, "Downloading...")
    send_audio(call.message.chat.id, url, reply_to=call.message.message_id)

@bot.message_handler(func=lambda m: True, content_types=['text'])
def handle_text(message):
    if message.text.startswith('/'):
        return
    user_id = message.from_user.id
    if is_limited(user_id):
        return

    first_name = message.from_user.first_name
    text = message.text.strip()
    ai_reply = ai_chat(first_name, text) if USE_AI else None

    song_kw = ['song', 'gaana', 'gana', 'play', 'music', 'geet', 'bajao', 'suno']
    if any(kw in text.lower() for kw in song_kw):
        cleaned = re.sub('|'.join(song_kw), '', text, flags=re.IGNORECASE).strip()
        if not cleaned:
            cleaned = text
        reply = ai_reply or f"🎵 Main aapke liye **{cleaned}** dhundh raha hoon !!"
        bot.reply_to(message, reply, parse_mode='Markdown')
        entries = search_songs(cleaned, 1)
        if entries:
            send_audio(message.chat.id, entries[0]['url'], reply_to=message.message_id)
        else:
            bot.reply_to(message, "❌ Koi result nahi mila.")
    else:
        if ai_reply:
            bot.reply_to(message, ai_reply)
        else:
            bot.reply_to(message, simple_reply(first_name, text))

@bot.message_handler(commands=['set_video'])
def set_video(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "⛔ Admin only.")
        return
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
        "• Type a song name (e.g., `play dil diyan gallan`)\n"
        "• Use inline search: `@botusername song`\n"
        "• /search <song> – list results\n"
        "• /set_video – admin only, set welcome video\n"
        "• I speak Hindi & English 😊",
        parse_mode='Markdown')

# ==================== MAIN ====================
if __name__ == '__main__':
    init_db()
    logger.info("Music bot started.")
    bot.remove_webhook()
    time.sleep(1)
    bot.infinity_polling()
