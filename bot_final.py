import logging
import os
import asyncio
import json
import random
import aiohttp
import re
import requests
import base64
import signal
import sys
import time
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.methods import DeleteWebhook
from aiogram.types import InputMediaPhoto, BufferedInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
import html
from mistralai import Mistral
from faceit_api import get_player_text_card, get_multiple_players_text, get_player_stats, clear_cache, get_player_full_card

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8331249759:AAFjxWonHiDbenOnr9lNpdJ7v1Y6UJAJ56w")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "V68jKeWkbgouyImfFx7rHS7RwdwsI0kV")
BOT_USERNAME = "team_spirt2_bot"

MAX_CHUNK = 3900

# Кеш для статистики (50 минут)
faceit_cache = {}
CACHE_DURATION = 3000

# Константы
LAST_POST_FILE = "last_telegram_post.json"
# Каналы для мониторинга (имена без @)
TELEGRAM_CHANNELS = ["newcsgo", "retakenews"]
CHECK_INTERVAL = 60

CHAT_ID = -1003200108763
TARGET_CHAT_ID = CHAT_ID
USERS_FILE = "users.json"
EVENTS_FILE = "events.json"
MEMORY_FILE = "user_memory.json"

MODEL = "mistral-medium-latest"
client = Mistral(api_key=MISTRAL_API_KEY) if MISTRAL_API_KEY else None

TRIGGER_WORDS = ["габен", "хуесос"]
JOKE_TRIGGERS = ["анекдот", "шутка", "рофл", "прикол"]

# ==================== GITHUB СИНХРОНИЗАЦИЯ ====================
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # формат: username/repo
BACKUP_FILES = [USERS_FILE, EVENTS_FILE, MEMORY_FILE, LAST_POST_FILE]

def escape_md(text: str) -> str:
    """
    Экранирует все специальные символы MarkdownV2
    """
    # Список символов, которые нужно экранировать
    return re.sub(r'([_*[\]()~`>#+\-=|{}.!])', r'\\\1', text)

def backup_to_github():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    
    try:
        for filename in BACKUP_FILES:
            if os.path.exists(filename):
                with open(filename, "r", encoding="utf-8") as f:
                    content = f.read()
                
                # Кодируем в base64 для GitHub API
                encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
                
                # Проверяем существует ли файл в репозитории
                url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
                headers = {
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.v3+json"
                }
                
                response = requests.get(url, headers=headers)
                
                if response.status_code == 200:
                    # Файл существует - обновляем
                    sha = response.json()["sha"]
                    data = {
                        "message": f"Backup {filename}",
                        "content": encoded_content,
                        "sha": sha
                    }
                    requests.put(url, headers=headers, json=data)
                else:
                    # Файл не существует - создаем новый
                    data = {
                        "message": f"Initial backup {filename}",
                        "content": encoded_content
                    }
                    requests.put(url, headers=headers, json=data)
                    
        logging.info("✅ Резервная копия создана в GitHub")
        return True
    except Exception as e:
        logging.error(f"❌ Ошибка backup в GitHub: {e}")
        return False

def restore_from_github():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    
    try:
        for filename in BACKUP_FILES:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
            headers = {
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            }
            
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                content = response.json()["content"]
                decoded_content = base64.b64decode(content).decode("utf-8")
                
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(decoded_content)
                    
        logging.info("✅ Данные восстановлены из GitHub")
        return True
    except Exception as e:
        logging.error(f"❌ Ошибка восстановления из GitHub: {e}")
        return False

# ==================== КОНФИГУРАЦИЯ ТЕМ ====================
# Инициализация ID тем (None = не настроено). Используйте /setup_topics чтобы задать.
TOPIC_IDS = {
    "HUMAN_CHAT": 8,
    "BOT_CHAT": 3,
    "NEWS_CHAT": 6,
    "NEWS_RETAKE_CHAT": 1408,
}

# ==================== УТИЛИТЫ ====================
def extract_command(text: str, bot_username: str) -> str:
    """Извлекает чистую команду из текста с упоминанием бота"""
    if not text:
        return ""
    text = re.sub(rf'@{re.escape(bot_username)}\s*', '', text)
    return text.strip()

def escape_markdown_v2(text):
    """Экранирование символов для MarkdownV2"""
    if not text:
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def is_allowed_topic(message: Message) -> bool:
    """Проверяет можно ли боту отвечать в этой теме"""
    topic_id = message.message_thread_id

    # Если темы не настроены — разрешаем отвечать везде
    if not any(v is not None for v in TOPIC_IDS.values()):
        return True

    # Если сообщение в основном чате — запрещаем
    if topic_id is None:
        return False

    # Разрешаем отвечать только в теме бота (BOT_CHAT)
    return topic_id == TOPIC_IDS.get("BOT_CHAT")

def is_news_topic(message: Message) -> bool:
    """Проверяет это тема для новостей"""
    nid = TOPIC_IDS.get("NEWS_CHAT")
    if nid is None:
        return False
    return message.message_thread_id == nid

def normalize_url(url, base_url="https://t.me"):
    if not url:
        return url
    if url.startswith('//'):
        return 'https:' + url
    elif url.startswith('/'):
        return urljoin(base_url, url)
    else:
        return url

def clean_markdown_text(text):
    if not text:
        return text
    text = html.escape(text)
    text = re.sub(r'\[([^\]]*)\]\([^\)]*$', r'\1', text)
    text = re.sub(r'\*\*([^*]*)$', r'\1', text)
    text = re.sub(r'\*([^*]*)$', r'\1', text)
    text = re.sub(r'__([^_]*)$', r'\1', text)
    text = re.sub(r'`([^`]*)$', r'\1', text)
    text = re.sub(r'_{3,}', '___', text)
    return text

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С ФАЙЛАМИ ====================
def load_last_post():
    """Загрузка состояния обработанных постов по каналам (новый формат)
    Возвращает dict: {"channels": {channel: {"last_post_time": ..., "processed_posts": [...]}}}
    """
    try:
        if not os.path.exists(LAST_POST_FILE):
            return {
                "channels": {
                    channel: {"last_post_time": None, "processed_posts": []} for channel in TELEGRAM_CHANNELS
                }
            }
        with open(LAST_POST_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {
                    "channels": {
                        channel: {"last_post_time": None, "processed_posts": []} for channel in TELEGRAM_CHANNELS
                    }
                }
            data = json.loads(content)
            # Конвертация старого формата
            if not isinstance(data, dict) or "channels" not in data:
                old_time = data.get("last_post_time")
                old_posts = data.get("processed_posts", [])
                data = {"channels": {channel: {"last_post_time": old_time, "processed_posts": old_posts} for channel in TELEGRAM_CHANNELS}}
            return data
    except Exception as e:
        logging.error(f"Ошибка загрузки last_post: {e}")
        return {
            "channels": {
                channel: {"last_post_time": None, "processed_posts": []} for channel in TELEGRAM_CHANNELS
            }
        }

def save_users(users_data):
    """Сохранение данных пользователей"""
    try:
        with open(USERS_FILE, "w", encoding='utf-8') as f:
            json.dump(users_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"❌ Ошибка сохранения users: {e}")

def load_users():
    """Загрузка данных пользователей"""
    try:
        if not os.path.exists(USERS_FILE):
            return {}
        with open(USERS_FILE, "r", encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"❌ Ошибка загрузки users: {e}")
        return {}
    except Exception as e:
        logging.error(f"Ошибка загрузки users: {e}")
        return {
            "channels": {
                channel: {
                    "last_post_time": None,
                    "processed_posts": []
                } for channel in TELEGRAM_CHANNELS
            }
        }


def save_last_post(channel: str, post_time, processed_posts):
    """Сохранение состояния обработанных постов для конкретного канала"""
    try:
        data = load_last_post()
        if "channels" not in data:
            data["channels"] = {}
        data["channels"][channel] = {
            "last_post_time": post_time,
            "processed_posts": list(processed_posts)
        }
        with open(LAST_POST_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        asyncio.create_task(async_backup_to_github())
    except Exception as e:
        logging.error(f"❌ Ошибка сохранения last_post для канала {channel}: {e}")


def load_events():
    try:
        if not os.path.exists(EVENTS_FILE):
            return {}
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return json.loads(content) if content else {}
    except Exception as e:
        logging.error(f"Ошибка загрузки events: {e}")
        return {}

def save_events(events):
    try:
        with open(EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
        asyncio.create_task(async_backup_to_github())
    except Exception as e:
        logging.error(f"Ошибка сохранения events: {e}")

def load_memory():
    try:
        if not os.path.exists(MEMORY_FILE):
            return {}
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return json.loads(content) if content else {}
    except Exception as e:
        logging.error(f"Ошибка загрузки memory: {e}")
        return {}

def save_memory(memory):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)
        asyncio.create_task(async_backup_to_github())
    except Exception as e:
        logging.error(f"Ошибка сохранения memory: {e}")

async def async_backup_to_github():
    """Асинхронный backup в GitHub"""
    await asyncio.get_event_loop().run_in_executor(None, backup_to_github)

# ==================== ИНИЦИАЛИЗАЦИЯ ДАННЫХ ====================
cs2_events = load_events()
user_memory = load_memory()

# ==================== КЛАВИАТУРЫ ====================
cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Отмена")]],
    resize_keyboard=True,
    one_time_keyboard=True
)

# ==================== ШУТКИ ====================
JOKES = [
    "Ты в CS2 как экономика России — стабильно 0/15/3.",
    "Когда ты говоришь 'я саппорт' — вся команда держит дым, брат.",
    "Если у тебя не получается стрелять — попробуй выключить монитор, в твоём случае поможет.",
    "Твой пинг в CS2 выше, чем твой IQ, и это о чём-то говорит.",
    "Не переживай, что проиграл катку — ты просто статистику улучшаешь другим!",
    "Ты как тот охранник лоу-таба — стоишь и смотришь, как все проходят мимо.",
    "Твой топ фраггер — это когда ты случайно убил кого-то с гранаты.",
    "Когда ты с гранатой в руке — вся команда готовится к респавну.",
    'Твоя тактика "сломался прицел" работает стабильнее, чем твой мозг.',
    "Твой K/D ratio ниже, чем цена твоего самого дешёвого скина.",
    "Ты тот типчик, который кидает дым в пентхаус, а сам идёт на B и умирает.",
    "— Почему ты проиграл? \n— Да потому что ты один с флешкой в руке стоял, бот.",
    "Твои скилы стрельбы как твои шутки — мимо.",
    "Когда ты заходишь в тиму — все сразу хотят сдаваться.",
    "Ты единственный, кто может проиграть 1х1 с ботом.",
    "Твой аим как твои шансы на свидание — всегда ниже нуля.",
    "Если бы CS2 был работой — тебя бы уволили после пистолетного раунда.",
    "Ты покупаешь AWP чтобы все видели, что ты не просто бедный, но и бесполезный.",
    "Когда ты говоришь 'я знаю все смоки' — команда плачет.",
    "Твой голос в голосовом чате заставляет тиммейтов отключать звук.",
    "Ты как тот баг с текстурой — все на тебя натыкаются и раздражаются.",
    "Твои флешки ослепляют команду чаще, чем врагов.",
    "Ты тот, кто покупает полный набор и умирает первым.",
    "Твой Game Sense как GPS в подземке — не работает.",
    "Когда ты лидер — это как слепой ведёт слепых, но с гранатами.",
    "Ты проигрываешь даже в режиме с ботами... на легкой сложности.",
    "Твой к/д как твои достижения в жизни — отрицательный.",
    "Если бы за смерть давали деньги — ты был бы миллионером.",
    "Ты как тот баг с hitbox — все через тебя проходят.",
    "Когда ты говоришь 'пацаны, я пошёл на А' — вся команда бежит на Б.",
    "Твоя тактика 'бежим рашить' заканчивается быстрее, чем твои отношения.",
    "Ты тот, кто кричит 'кидаю флешку' и ослепляет своих.",
    "Твой скин-кейшн как твоя жизнь — полное разочарование.",
    "Когда ты в тиме — это 4 против 6.",
    "Ты как тот игрок, который смотрит в пол, когда все стреляют.",
    "Ты в CS2 как экономика России — стабильно 0/15/3.",
    "Когда ты говоришь 'я саппорт' — вся команда держит дым, брат.",
    "Если у тебя не получается стрелять — попробуй выключить монитор, в твоём случае поможет.",
    "Твой пинг в CS2 выше, чем твой IQ, и это о чём-то говорит.",
    "Ты как тот охранник лоу-таба — стоишь и смотришь, как все проходят мимо.",
    "Ты взял муху пофоткать, да? Так вот, фотки не получились.",
    "Ты покупаешь AWP, чтобы показать, что у тебя не скилл, а амбиции.",
    "Когда ты кидаешь смок, сервер падает от стыда.",
    "Ты как кейс в CS2 — все надеются на что-то хорошее, но внутри мусор.",
    "Когда ты заходишь в тиммейтам в дискорд — FPS падает у всех.",
    "Твоя флешка ослепила даже комментаторов.",
    "Ты рашишь Б так, будто там халява на скины.",
    "Когда ты берёшь муху — даже враги делают скрин, чтоб не забыть этот момент.",
    "Ты играешь как VAC-бан — неожиданный и неприятный.",
    "Твой aim как тикрейт в CS2 — нестабильный и больной.",
    "Ты как новый патч — только всё ухудшаешь.",
    "Твой микрофон громче, чем твой урон.",
    "Когда ты берёшь AWP, сервер пишет: «Сожалеем».",
    "Ты в тиме для атмосферы, а не для побед.",
    "Твоя граната — идеальный пример дружеского огня.",
    "Ты как баг в игре — все на тебя жалуются, но ничего не меняется.",
    "Твой вклад в победу как мотивация учиться в воскресенье — его нет."
]

# ==================== Faceit СТАТИСТИКА ====================



# ==================== МОНИТОРИНГ КАНАЛА ====================
def create_post_id(post):
    text_hash = hash(post['text_plain'][:100] if post['text_plain'] else "media") % 10000
    return f"{post['time']}_{text_hash}"

async def get_telegram_posts(channel: str):
    """Получает посты из переданного telegram канала (имя без @)"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    url = f"https://t.me/s/{channel}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    logging.warning(f"Не удалось получить страницу {url}: {response.status}")
                    return None

                html = await response.text()
                soup = BeautifulSoup(html, 'html.parser')
                messages = soup.find_all('div', class_='tgme_widget_message')
                posts = []

                for message in messages:
                    try:
                        text_element = message.find('div', class_='tgme_widget_message_text')
                        # Получаем текст как есть, сохраняя эмодзи
                        post_text_plain = text_element.get_text(strip=True, separator='\n') if text_element else ""

                        time_element = message.find('time', class_='time')
                        post_time = time_element['datetime'] if time_element and 'datetime' in time_element.attrs else None

                        post_link = message.find('a', class_='tgme_widget_message_date')
                        post_url = post_link['href'] if post_link and 'href' in post_link.attrs else None

                        # Фото
                        photo_urls = []
                        for photo in message.find_all('a', class_='tgme_widget_message_photo_wrap'):
                            if 'style' in photo.attrs:
                                match = re.search(r"background-image:url\('([^']+)'\)", photo['style'])
                                if match:
                                    photo_urls.append(match.group(1))

                        # Видео
                        video_urls = [video['src'] for video in message.find_all('video', class_='tgme_widget_message_video') if video.get('src')]

                        # Документы
                        documents = []
                        for doc in message.find_all('a', class_='tgme_widget_message_document_wrap'):
                            doc_url = doc.get('href', '')
                            doc_title = doc.find('div', class_='tgme_widget_message_document_title')
                            documents.append({
                                'url': doc_url,
                                'title': doc_title.text.strip() if doc_title else "Документ"
                            })

                        if (post_text_plain or photo_urls or video_urls or documents) and post_time:
                            post_data = {
                                'text_plain': post_text_plain,
                                'time': post_time,
                                'photo_urls': photo_urls,
                                'video_urls': video_urls,
                                'documents': documents,
                                'url': post_url,
                                'is_reply': bool(message.find('a', class_='tgme_widget_message_reply'))
                            }
                            post_data['id'] = create_post_id(post_data)
                            posts.append(post_data)

                    except Exception:
                        continue

                return posts

    except Exception as e:
        logging.error(f"Ошибка парсинга канала {channel}: {e}")
        return None

async def download_media(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://t.me/"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=30) as response:
                return await response.read() if response.status == 200 else None
    except Exception:
        return None

async def send_telegram_post(post, source_channel: str = None):
    try:
        caption = "📢 *Новый пост из канала*\n\n"
        
        if post['text_plain']:  # Используем plain текст для отправки
            text = post['text_plain'][:800] + ("..." if len(post['text_plain']) > 800 else "")
            # Экранируем специальные символы для MarkdownV2
            escaped_text = re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', text)
            caption += escaped_text
        
        if post['url']:
            # Экранируем только специальные символы в URL, сохраняя его структуру
            escaped_url = re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', post['url'])
            caption += f"\n\n[🔗 Оригинал]({escaped_url})"

        target_chat_id = TARGET_CHAT_ID
        # Роутинг по каналу: для retakenews используем отдельную тему
        logging.info(f"🔄 Роутинг: канал={source_channel}")
        
        if source_channel:
            channel_lower = source_channel.lower().strip()
            if channel_lower == "retakenews":
                message_thread_id = TOPIC_IDS.get("NEWS_RETAKE_CHAT")
                if message_thread_id:
                    logging.info(f"✅ Отправка в тему ретейк {message_thread_id}")
                else:
                    logging.error("❌ NEWS_RETAKE_CHAT не найден в TOPIC_IDS")
            else:
                message_thread_id = TOPIC_IDS.get("NEWS_CHAT")
                logging.info(f"� Отправка в общую тему {message_thread_id}")
        else:
            message_thread_id = TOPIC_IDS.get("NEWS_CHAT")
            logging.warning("⚠️ source_channel не указан, используем общую тему")

        thread_kwargs = {"message_thread_id": message_thread_id} if message_thread_id is not None else {}

        # Отправка медиа
        photo_urls = [normalize_url(url) for url in post['photo_urls']]
        video_urls = [normalize_url(url) for url in post['video_urls']]
        
        if photo_urls:
            if len(photo_urls) == 1:
                photo_data = await download_media(photo_urls[0])
                if photo_data:
                    await bot.send_photo(
                        chat_id=target_chat_id,
                        photo=BufferedInputFile(photo_data, "photo.jpg"),
                        caption=caption,
                        parse_mode="MarkdownV2",
                        **thread_kwargs
                    )
            else:
                media_group = []
                for i, url in enumerate(photo_urls[:10]):
                    photo_data = await download_media(url)
                    if photo_data:
                        media = InputMediaPhoto(
                            media=BufferedInputFile(photo_data, f"photo_{i}.jpg"),
                            caption=caption if i == 0 else None,
                            parse_mode="MarkdownV2" if i == 0 else None
                        )
                        media_group.append(media)
                
                if media_group:
                    await bot.send_media_group(
                        chat_id=target_chat_id,
                        media=media_group,
                        **thread_kwargs
                    )
        
        elif video_urls:
            video_data = await download_media(video_urls[0])
            if video_data:
                await bot.send_video(
                    chat_id=target_chat_id,
                    video=BufferedInputFile(video_data, "video.mp4"),
                    caption=caption,
                    parse_mode="MarkdownV2",
                    **thread_kwargs
                )
        
        logging.info("✅ Новый пост отправлен")
        return True
        
    except Exception as e:
        logging.error(f"❌ Ошибка отправки поста: {e}")
        return False

async def check_telegram_channel(channel: str):
    logging.info(f"🔍 Проверка канала {channel}")
    posts = await get_telegram_posts(channel)
    if not posts:
        return
    
    data = load_last_post()
    channel_data = data.get("channels", {}).get(channel, {"last_post_time": None, "processed_posts": []})
    last_post_time = channel_data["last_post_time"]
    processed_posts = set(channel_data["processed_posts"])
    new_posts_found = 0
    latest_post_time = last_post_time
    
    for post in posts:
        if post['id'] in processed_posts:
            continue

        if last_post_time is None or post['time'] > last_post_time:
            success = await send_telegram_post(post, source_channel=channel)
            if success:
                processed_posts.add(post['id'])
                if latest_post_time is None or post['time'] > latest_post_time:
                    latest_post_time = post['time']
                new_posts_found += 1
                await asyncio.sleep(1)
    
    if new_posts_found > 0:
        save_last_post(channel, latest_post_time, processed_posts)
        logging.info(f"✅ Обработано {new_posts_found} новых постов")

async def scheduled_channel_check():
    while True:
        for channel in TELEGRAM_CHANNELS:
            try:
                await check_telegram_channel(channel)
            except Exception:
                logging.exception("Ошибка при проверке канала %s", channel)
            await asyncio.sleep(1)
        await asyncio.sleep(CHECK_INTERVAL)

# ==================== СОБЫТИЯ CS2 ====================
async def check_event(chat_id, hh, mm):
    """Запускает напоминания о событии CS2"""
    event_time = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    now = datetime.now()

    if event_time < now:
        event_time += timedelta(days=1)

    reminder_time = event_time - timedelta(minutes=15)
    reminder_sent = False

    while True:
        now = datetime.now()
        if now >= reminder_time and not reminder_sent:
            await bot.send_message(chat_id, "⏰ Напоминание: сбор по CS2 через 15 минут!")
            reminder_sent = True

        if now >= event_time:
            await bot.send_message(chat_id, "🎮 Пора на сбор по CS2! Всем собраться!")
            break

        await asyncio.sleep(30)

# ==================== ОБРАБОТЧИКИ КОМАНД ====================
@dp.message(Command("start"))
async def start_command(message: Message):
    if not is_allowed_topic(message):
        return
        
    await message.reply(
        f"**Информация о чате:**\n"
        f"💬 ID чата: `{message.chat.id}`\n"
        f"📌 ID темы: `{message.message_thread_id}`\n"
        f"👤 Пользователь: {message.from_user.username or message.from_user.first_name}",
        parse_mode="Markdown"
    )

async def show_commands(message: types.Message):
        
    text = (
        "🤖 *ДОСТУПНЫЕ КОМАНДЫ*\n\n"
        
        "👋 *Основные*\n"
        "▫️ /start \\- Запустить бота\n"
        "▫️ /stats \\- Моя статистика Faceit\n"
        "▫️ команды \\- Этот список\n\n"
        
        "📊 *Статистика*\n"  
        "▫️ /list\\_all\\_stats \\- Статистика всех игроков\n"
        "▫️ /bind STEAM\\_ID \\- Привязать Steam аккаунт\n\n"
        
        "🎮 *События CS2*\n"
        "▫️ /create\\_event \\- Создать новое событие\n"
        "▫️ /events \\- Показать текущие сборы\n"
        "▫️ /clear\\_events \\- Очистить все события\n\n"
        
        "⚙️ *Управление темами*\n"
        "▫️ /setup\\_topics \\- Настроить ID тем\n"
        "▫️ /topics\\_info \\- Информация о темах\n"
        "▫️ /get\\_topic \\- Получить ID темы\n\n"
        
        "😄 *Взаимодействие*\n"
        "▫️ Напиши 'габен' или 'хуесос' для общения\n"
        "▫️ Напиши 'анекдот', 'шутка' для шутки"
    )
    
    await message.reply(text, parse_mode="MarkdownV2")

@dp.message(Command("create_event"))
async def create_event_command(message: types.Message):
    if not is_allowed_topic(message):
        return
        
    await message.answer(
        "Напиши время сбора в формате ЧЧ:ММ, например 20:30",
        reply_markup=cancel_keyboard
    )
    cs2_events[message.from_user.id] = {
        "waiting_for_time": True,
        "chat_id": message.chat.id,
        "user_id": message.from_user.id
    }
    save_events(cs2_events)

@dp.message(Command("backup"))
async def backup_command(message: Message):
    if not is_allowed_topic(message):
        return
        
    if not GITHUB_TOKEN or not GITHUB_REPO:
        await message.reply("❌ GitHub синхронизация не настроена")
        return
        
    await message.reply("🔄 Создаю резервную копию...")
    success = await async_backup_to_github()
    
    if success:
        await message.reply("✅ Резервная копия создана в GitHub")
    else:
        await message.reply("❌ Ошибка создания резервной копии")

@dp.message(lambda message: message.from_user.id in cs2_events and cs2_events[message.from_user.id].get("waiting_for_time"))
async def handle_event_time(message: types.Message):
    user_event = cs2_events.get(message.from_user.id)
    if not user_event:
        return

    if message.text and message.text.lower() == "отмена":
        cs2_events.pop(message.from_user.id, None)
        save_events(cs2_events)
        await message.reply("❌ Создание сбора отменено.")
        return

    try:
        hh, mm = map(int, message.text.strip().split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except:
        await message.reply("Неверный формат! Используй ЧЧ:ММ")
        return

    user_event["waiting_for_time"] = False
    user_event["time"] = f"{hh:02d}:{mm:02d}"
    save_events(cs2_events)
    await message.reply(f"✅ Сбор на CS2 назначен на {hh:02d}:{mm:02d}!")
    asyncio.create_task(check_event(user_event["chat_id"], hh, mm))

async def stats_command(message: Message):
    if not is_allowed_topic(message):
        return

    users = load_users()
    tg_id = str(message.from_user.id)

    if tg_id not in users:
        await message.reply(
            "❌ Используй /bind <SteamID64> чтобы привязать Steam "
            "(или заполни faceit_nick в users.json)"
        )
        return

    loading_msg = await message.reply("🔄 Получаю полную статистику...")

    user_info = users[tg_id]
    faceit_nick = user_info.get("faceit_nick")
    steam_id = user_info.get("steam_id")

    try:
        card = await get_player_full_card(faceit_nick, steam_id)
    except Exception as e:
        logging.exception("Ошибка при запросе Faceit: %s", e)
        card = None

    if not card:
        await loading_msg.edit_text("❌ Не удалось получить статистику через Faceit API.")
        return

    # экранируем перед отправкой
    card = escape_md(card)

    # Разбиваем длинное сообщение на части
    chunks = [card[i:i+MAX_CHUNK] for i in range(0, len(card), MAX_CHUNK)]
    await loading_msg.delete()
    for chunk in chunks:
        await message.reply(chunk, parse_mode="MarkdownV2")

@dp.message(Command("clear_faceit_cache"))
async def clear_faceit_cache_cmd(message: Message):
    if message.from_user.id not in [1089779100, 1404218084]:  # твои админы
        await message.reply("❌ Только админы.")
        return
    clear_cache()
    await message.reply("✅ Faceit cache очищен.")

@dp.message(Command("list_all_stats"))
async def list_all_stats(message: types.Message):
    if not is_allowed_topic(message):
        return

    users = load_users()
    if not users:
        await message.reply("Пользователей пока нет.")
        return

    loading_msg = await message.reply(
        "🔄 Собираю полную статистику для всех пользователей..."
    )

    full_text = ""
    for tg_id, user_info in users.items():
        faceit_nick = user_info.get("faceit_nick")
        steam_id = user_info.get("steam_id")
        try:
            card = await get_player_full_card(faceit_nick, steam_id)
            if card:
                full_text += card + "\n\n" + ("─" * 20) + "\n\n"
        except Exception as e:
            logging.exception("Ошибка получения данных для %s: %s", tg_id, e)
            full_text += f"❌ Ошибка получения статистики для {tg_id}\n\n"

    await loading_msg.delete()

    if not full_text:
        await message.reply("❌ Не удалось собрать статистику ни для одного пользователя.")
        return

    # Экранируем весь текст перед отправкой
    full_text = escape_md(full_text)

    # Разбиваем на безопасные для Telegram части
    chunks = [full_text[i:i+MAX_CHUNK] for i in range(0, len(full_text), MAX_CHUNK)]
    for chunk in chunks:
        await message.reply(chunk, parse_mode="MarkdownV2")

@dp.message(Command("events"))
async def show_events(message: Message):
    if not is_allowed_topic(message):
        return
        
    if not cs2_events:
        await message.reply("Событий пока нет.")
        return

    msg = "Текущие события:\n"
    for user_id, ev in cs2_events.items():
        status = "Ждём время" if ev.get("waiting_for_time") else f"Время: {ev.get('time')}"
        msg += f"- Пользователь {user_id}: {status}\n"

    await message.reply(msg)

@dp.message(Command("clear_events"))
async def clear_events(message: types.Message):
    if not is_allowed_topic(message):
        return
        
    cs2_events.clear()
    save_events(cs2_events)
    await message.reply("🗑 Все события очищены.")

# ==================== КОМАНДЫ ДЛЯ УПРАВЛЕНИЯ ТЕМАМИ ====================

@dp.message(Command("get_topic"))
async def get_topic_info(message: Message):
    """Команда для получения информации о теме"""
    topic_id = message.message_thread_id
    chat_id = message.chat.id
    
    if topic_id:
        await message.reply(
            f"**Информация о теме:**\n"
            f"🆔 ID темы: `{topic_id}`\n"
            f"💬 ID чата: `{chat_id}`",
            parse_mode="Markdown"
        )
    else:
        await message.reply("❌ Это сообщение не в теме (основной чат)")

@dp.message(Command("setup_topics"))
async def setup_topics_command(message: Message):
    """Команда для настройки ID тем"""
    if message.from_user.id not in [1089779100, 1404218084]:
        await message.reply("❌ Только админы могут настраивать темы")
        return
    
    args = message.text.split()
    
    if len(args) != 5:
        await message.reply(
            "**Использование:**\n"
            "`/setup_topics <human_topic_id> <bot_topic_id> <news_topic_id>`\n\n"
            "💡 Чтобы получить ID темы, напиши `/get_topic` в нужной теме"
        )
        return
    
    try:
        TOPIC_IDS["HUMAN_CHAT"] = int(args[1])
        TOPIC_IDS["BOT_CHAT"] = int(args[2]) 
        TOPIC_IDS["NEWS_CHAT"] = int(args[3])
        TOPIC_IDS["NEWS_RETAKE_CHAT"] = int(args[4])  # По умолчанию та же тема для новостей ретейк
        
        await message.reply(
            "✅ **Темы настроены!**\n\n"
            f"💬 Общий чат: `{TOPIC_IDS['HUMAN_CHAT']}`\n"
            f"🤖 Бот Габен: `{TOPIC_IDS['BOT_CHAT']}`\n" 
            f"📢 Новости CS2: `{TOPIC_IDS['NEWS_CHAT']}`\n"
            f"📢 Новости CS2 Ретейк: `{TOPIC_IDS['NEWS_RETAKE_CHAT']}`"
        )
        
    except ValueError:
        await message.reply("❌ ID тем должны быть числами!")

@dp.message(Command("topics_info"))
async def topics_info_command(message: Message):
    """Показывает текущие настройки тем"""
    info_text = (
        "**Текущие настройки тем:**\n\n"
        f"💬 Общий чат: `{TOPIC_IDS['HUMAN_CHAT']}`\n"
        f"🤖 Бот Габен: `{TOPIC_IDS['BOT_CHAT']}`\n"
        f"📢 Новости CS2: `{TOPIC_IDS['NEWS_CHAT']}`\n\n"
        f"🆔 Текущая тема: `{message.message_thread_id}`\n"
        f"✅ Бот отвечает здесь: **{'ДА' if is_allowed_topic(message) else 'НЕТ'}**"
    )
    
    await message.reply(info_text, parse_mode="Markdown")

# ==================== ОСНОВНОЙ ОБРАБОТЧИК ====================
@dp.message()
async def handle_message(message: Message):
    if not is_allowed_topic(message) or not message.text:
        return

    text = message.text.lower().strip()

    if "команды" in text:
        await show_commands(message)
        return

    # Очистка событий
    if text == "очистить события":
        cs2_events.clear()
        save_events(cs2_events)
        await message.reply("🗑 Все события очищены.")
        return

    # Шутки
    if any(trigger in text for trigger in JOKE_TRIGGERS):
        await message.reply(random.choice(JOKES))
        return

    # AI-ответы
    if not any(trigger in text for trigger in TRIGGER_WORDS):
        return

    user_id = str(message.from_user.id)
    users = load_users()
    user_stats = None

    history = user_memory.get(user_id, [])
    history.append({"role": "user", "content": text})
    history = history[-5:]

    # Запрос к Mistral: выполняем в отдельном потоке и оборачиваем ответ
    system_prompt = (
        "Ты запуганный, опущенный бот, говоришь по-русски с небольшим количеством элементов геймерского сленга, "
        "но без прямых оскорблений и призывов к насилию. Используй статистику Faceit только по запросу. Отвечай коротко и иронично."
    )

    def sync_mistral_call(system_prompt, messages):
        if client is None:
            logging.error("Mistral client not configured")
            return None
        try:
            return client.chat.complete(model=MODEL, messages=[{"role": "system", "content": system_prompt}, *messages])
        except Exception as e:
            logging.exception("Mistral sync call failed: %s", e)
            return None

    try:
        resp = await asyncio.wait_for(asyncio.to_thread(sync_mistral_call, system_prompt, history), timeout=30)
    except asyncio.TimeoutError:
        logging.error("Mistral request timed out")
        resp = None

    if not resp:
        logging.error("Ошибка получения ответа от Mistral")
        await message.reply("❌ Ошибка генерации ответа.")
        return

    # Попытка гибко распарсить ответ в разных форматах
    bot_reply = None
    try:
        if hasattr(resp, 'choices') and resp.choices:
            choice = resp.choices[0]
            if hasattr(choice, 'message') and hasattr(choice.message, 'content'):
                bot_reply = choice.message.content
            elif isinstance(choice, dict):
                bot_reply = choice.get('message', {}).get('content') or choice.get('text')
        elif isinstance(resp, dict):
            bot_reply = resp.get('output') or resp.get('text')
        else:
            bot_reply = str(resp)
    except Exception as e:
        logging.exception("Failed to parse Mistral response: %s", e)
        bot_reply = None

    if not bot_reply:
        logging.error("Mistral returned empty response after parsing")
        await message.reply("❌ Ошибка генерации ответа.")
        return

    history.append({"role": "assistant", "content": bot_reply})
    user_memory[user_id] = history
    save_memory(user_memory)

    await message.reply(bot_reply, parse_mode="Markdown")


def handle_sigterm(*args):
    """Обработчик сигнала завершения"""
    print("🔄 Received SIGTERM, shutting down gracefully...")
    # Не отправляем сообщение в Telegram - это может вызвать рестарт
    sys.exit(0)

# Регистрируем обработчик сигналов
signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)




# ==================== ЗАПУСК ====================
async def hard_reset_webhook():

    """Жесткий сброс через прямые HTTP запросы"""
    try:
        # Асинхронный метод через aiohttp
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook"
            params = {"drop_pending_updates": "true"}
            async with session.get(url, params=params) as resp:
                try:
                    logging.info(f"✅ Webhook reset response: {resp.status}")
                except Exception:
                    pass

            # Дополнительная проверка
            url2 = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
            try:
                async with session.get(url2) as resp2:
                    logging.debug(f"getMe status: {resp2.status}")
            except Exception:
                pass

        await asyncio.sleep(1)
        
    except Exception as e:
        logging.exception(f"❌ Hard reset failed: {e}")

async def main():
    # Проверяем обязательные переменные окружения
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN не задан. Завершение работы.")
        return

    await hard_reset_webhook()
    
    # Создаем файлы если нет
    for file, default in [(USERS_FILE, {}), (LAST_POST_FILE, {"last_post_time": None, "processed_posts": []}), 
                         (EVENTS_FILE, {}), (MEMORY_FILE, {})]:
        if not os.path.exists(file):
            with open(file, "w", encoding="utf-8") as f:
                json.dump(default, f, indent=2, ensure_ascii=False)

    # Восстанавливаем данные из GitHub при запуске
    if GITHUB_TOKEN and GITHUB_REPO:
        await asyncio.get_event_loop().run_in_executor(None, restore_from_github)

    # Запускаем мониторинг канала в фоне
    asyncio.create_task(scheduled_channel_check())
    
    print("🟢 Бот запущен на Railway...")

    # Очищаем обновления и запускаем polling
    await bot(DeleteWebhook(drop_pending_updates=True))
    
    try:
        # Старт бота - ОСНОВНОЙ ЦИКЛ
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as e:
        logging.error(f"❌ Ошибка при работе бота: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())

#"Ты токсичный, саркастичный бот, говоришь по-русски с небольшим количеством элементов геймерского сленга, также употребляешь русские маты. "
           #         "Ты также получаешь статистику из Faceit и можешь её использовать в разговорах только если попросят, а не в каждом сообщении. ТОЛЬКО КОГДА ПОПРОСЯТ! "
         #           f"Статистика пользователя: {user_stats if user_stats else 'Нет привязанного SteamID или статистика недоступна.'} "
         #           "Тебя зовут Габен, ты не пытешься обидеть пользователя, но говоришь всё как есть, без цензуры и чтобы было смешно. "
         #           "Твои ответы короткие, острые: немного рофла, немного боли. "

