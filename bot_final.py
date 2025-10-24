import logging
import os
import asyncio
import json
import random
import aiohttp
import re
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
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



LAST_POST_FILE = "last_telegram_post.json"
TELEGRAM_CHANNEL = "newcsgo"
TELEGRAM_CHANNEL_URL = f"https://t.me/s/{TELEGRAM_CHANNEL}"
CHECK_INTERVAL = 60

MISTRAL_API_KEY = "V68jKeWkbgouyImfFx7rHS7RwdwsI0kV"
BOT_TOKEN = "8023437078:AAFT5qCCe05oVgKgqaBZlbzuq1nd4wLizhM"
CHAT_ID = -4619177118

TARGET_CHAT_ID = CHAT_ID
USERS_FILE = "users.json"
EVENTS_FILE = "events.json"

MODEL = "mistral-medium-latest"
client = Mistral(api_key=MISTRAL_API_KEY)

TRIGGER_WORDS = ["габен", "хуесос"]
JOKE_TRIGGERS = ["анекдот", "шутка", "рофл", "прикол"]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()



def load_last_post():
    try:
        if not os.path.exists(LAST_POST_FILE):
            return None, set()
            
        with open(LAST_POST_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return None, set()
                
            data = json.loads(content)
            last_post_time = data.get("last_post_time")
            # Преобразуем список в множество
            processed_posts_list = data.get("processed_posts", [])
            processed_posts = set(processed_posts_list)  # ← ИСПРАВЛЕНИЕ ЗДЕСЬ
            return last_post_time, processed_posts
            
    except (json.JSONDecodeError, KeyError) as e:
        logging.warning(f"⚠️ Ошибка чтения файла {LAST_POST_FILE}: {e}. Создаю новый.")
        save_last_post(None, set())
        return None, set()
    except Exception as e:
        logging.error(f"❌ Неизвестная ошибка при загрузке постов: {e}")
        return None, set()
    
def save_last_post(post_time, processed_posts):
    with open(LAST_POST_FILE, "w", encoding="utf-8") as f:
        processed_posts_list = list(processed_posts)
        json.dump({
            "last_post_time": post_time,
            "processed_posts": processed_posts_list
        }, f, ensure_ascii=False, indent=2)

def create_post_id(post):
    text_hash = hash(post['text'][:100] if post['text'] else "media") % 10000
    return f"{post['time']}_{text_hash}"

async def get_telegram_posts():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(TELEGRAM_CHANNEL_URL, headers=headers) as response:
                if response.status != 200:
                    logging.error(f"Ошибка доступа к каналу: {response.status}")
                    return None
                
                html = await response.text()
                soup = BeautifulSoup(html, 'html.parser')
                
                messages = soup.find_all('div', class_='tgme_widget_message')
                posts = []
                
                for message in messages:
                    try:
                        # Проверяем, является ли сообщение реплаем
                        is_reply = bool(message.find('a', class_='tgme_widget_message_reply'))
                        
                        # Текст сообщения - берем из реплая если это реплай
                        text_element = message.find('div', class_='tgme_widget_message_text')
                        
                        # Если это реплай, проверяем есть ли собственный текст
                        if is_reply and text_element:
                            # Ищем текст который относится именно к этому сообщению, а не к цитируемому
                            reply_wrapper = message.find('div', class_='tgme_widget_message_reply')
                            if reply_wrapper:
                                # Убираем текст цитаты из общего текста
                                reply_text = reply_wrapper.get_text(strip=True)
                                full_text = text_element.get_text(strip=True, separator='\n')
                                # Берем текст после цитаты
                                if reply_text in full_text:
                                    post_text = full_text.replace(reply_text, '').strip()
                                else:
                                    post_text = full_text
                            else:
                                post_text = text_element.get_text(strip=True, separator='\n')
                        elif text_element:
                            post_text = text_element.get_text(strip=True, separator='\n')
                        else:
                            post_text = ""
                        
                        # Время сообщения
                        time_element = message.find('time', class_='time')
                        post_time = time_element['datetime'] if time_element and 'datetime' in time_element.attrs else None
                        
                        # Ссылка на пост
                        post_link_element = message.find('a', class_='tgme_widget_message_date')
                        post_url = post_link_element['href'] if post_link_element and 'href' in post_link_element.attrs else None
                        
                        # ФОТО: множественные фото (карусель)
                        photo_elements = message.find_all('a', class_='tgme_widget_message_photo_wrap')
                        photo_urls = []
                        for photo_element in photo_elements:
                            if 'style' in photo_element.attrs:
                                style = photo_element['style']
                                match = re.search(r"background-image:url\('([^']+)'\)", style)
                                if match:
                                    photo_urls.append(match.group(1))
                        
                        # ВИДЕО: поиск видео элементов
                        video_elements = message.find_all('video', class_='tgme_widget_message_video')
                        video_urls = []
                        for video_element in video_elements:
                            if video_element.get('src'):
                                video_urls.append(video_element['src'])
                        
                        # Документы/файлы
                        document_elements = message.find_all('a', class_='tgme_widget_message_document_wrap')
                        documents = []
                        for doc_element in document_elements:
                            doc_url = doc_element.get('href', '')
                            doc_title_elem = doc_element.find('div', class_='tgme_widget_message_document_title')
                            doc_title = doc_title_elem.text.strip() if doc_title_elem else "Документ"
                            documents.append({'url': doc_url, 'title': doc_title})
                        
                        # Проверяем, есть ли хоть какой-то контент
                        has_content = post_text or photo_urls or video_urls or documents
                        
                        if has_content and post_time:
                            post_data = {
                                'text': post_text,
                                'time': post_time,
                                'photo_urls': photo_urls,
                                'video_urls': video_urls,
                                'documents': documents,
                                'url': post_url,
                                'is_reply': is_reply  # добавляем информацию о том, что это реплай
                            }
                            post_data['id'] = create_post_id(post_data)
                            posts.append(post_data)
                            
                    except Exception as e:
                        logging.error(f"Ошибка парсинга сообщения: {e}")
                        continue
                
                return posts
                
    except Exception as e:
        logging.error(f"Ошибка при парсинге канала: {e}")
        return None

async def send_telegram_post(post):
    try:
        clean_text = clean_markdown_text(post['text']) if post['text'] else ""
        
        caption = f"📢 **Новый пост из канала**\n\n"
        if clean_text:
            text_preview = clean_text[:800] + "..." if len(clean_text) > 800 else clean_text
            caption += text_preview
        
        if post['url']:
            caption += f"\n\n[Ссылка на пост]({post['url']})"
        
        normalized_photo_urls = [normalize_url(url) for url in post['photo_urls']]
        normalized_video_urls = [normalize_url(url) for url in post['video_urls']]
        normalized_documents = []
        for doc in post['documents']:
            normalized_doc = doc.copy()
            normalized_doc['url'] = normalize_url(doc['url'])
            normalized_documents.append(normalized_doc)
        
        if normalized_photo_urls:
            if len(normalized_photo_urls) == 1:
                photo_data = await download_media(normalized_photo_urls[0])
                if photo_data:
                    photo_file = BufferedInputFile(photo_data, filename="photo.jpg")
                    await bot.send_photo(
                        chat_id=TARGET_CHAT_ID,
                        photo=photo_file,
                        caption=caption,
                        parse_mode="Markdown"
                    )
                else:
                    await send_text_fallback(clean_text, post['url'])
            else:
                media_group = []
                successful_downloads = 0
                
                for i, photo_url in enumerate(normalized_photo_urls[:10]):
                    photo_data = await download_media(photo_url)
                    if photo_data:
                        photo_file = BufferedInputFile(photo_data, filename=f"photo_{i}.jpg")
                        if i == 0:
                            media_group.append(
                                InputMediaPhoto(
                                    media=photo_file,
                                    caption=caption,
                                    parse_mode="Markdown"
                                )
                            )
                        else:
                            media_group.append(InputMediaPhoto(media=photo_file))
                        successful_downloads += 1
                
                if successful_downloads > 0:
                    await bot.send_media_group(
                        chat_id=TARGET_CHAT_ID,
                        media=media_group
                    )
                else:
                    await send_text_fallback(clean_text, post['url'])
        
        elif normalized_video_urls:
            video_data = await download_media(normalized_video_urls[0])
            if video_data:
                video_file = BufferedInputFile(video_data, filename="video.mp4")
                await bot.send_video(
                    chat_id=TARGET_CHAT_ID,
                    video=video_file,
                    caption=caption,
                    parse_mode="Markdown"
                )
            else:
                await send_text_fallback(clean_text, post['url'])
        
        elif normalized_documents:
            doc = normalized_documents[0]
            doc_data = await download_media(doc['url'])
            if doc_data:
                doc_file = BufferedInputFile(doc_data, filename=doc['title'])
                doc_caption = f"{caption}\n\n📎 {doc['title']}"
                await bot.send_document(
                    chat_id=TARGET_CHAT_ID,
                    document=doc_file,
                    caption=doc_caption,
                    parse_mode="Markdown"
                )
            else:
                await send_text_fallback(clean_text, post['url'], doc['title'])
        
        elif clean_text:
            await bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=caption,
                parse_mode="Markdown",
                disable_web_page_preview=False
            )
        
        logging.info("✅ Новый пост отправлен в чат")
        return True
        
    except Exception as e:
        logging.error(f"❌ Ошибка при отправке поста: {e}")
        return await send_text_fallback(post.get('text', ''), post.get('url'), fallback=True)

async def check_telegram_channel():
    logging.info("🔍 Проверяем новые посты в канале...")
    
    posts = await get_telegram_posts()
    if not posts:
        return
    
    last_post_time, processed_posts = load_last_post()
    
    new_posts_found = 0
    latest_post_time = last_post_time
    
    for post in posts:
        post_id = post['id']

        if post_id in processed_posts:
            continue

        is_new_post = (
            last_post_time is None or 
            post['time'] > last_post_time
        )
        
        if is_new_post:
            logging.info(f"🆕 Найден новый пост: {post['time']}")
            
            # Отправляем пост
            success = await send_telegram_post(post)
            
            if success:
                processed_posts.add(post_id) 
                if latest_post_time is None or post['time'] > latest_post_time:
                    latest_post_time = post['time']
                new_posts_found += 1

                await asyncio.sleep(1)
    
    # Сохраняем состояние
    if new_posts_found > 0:
        save_last_post(latest_post_time, processed_posts)
        logging.info(f"✅ Обработано {new_posts_found} новых постов")
    else:
        logging.info("📭 Новых постов нет")

    if len(processed_posts) > 100:
        processed_posts_list = list(processed_posts)
        processed_posts = set(processed_posts_list[-50:])
        save_last_post(latest_post_time, processed_posts)

async def scheduled_channel_check():
    while True:
        await check_telegram_channel()
        await asyncio.sleep(CHECK_INTERVAL)

def normalize_url(url, base_url="https://t.me"):
    if not url:
        return url
    if url.startswith('//'):
        return 'https:' + url
    elif url.startswith('/'):
        return urljoin(base_url, url)
    else:
        return url

async def download_media(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://t.me/"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=30) as response:
                if response.status == 200:
                    content = await response.read()
                    return content
                else:
                    logging.error(f"Ошибка загрузки медиа: {response.status}")
                    return None
    except Exception as e:
        logging.error(f"Ошибка при загрузке медиа: {e}")
        return None

def clean_markdown_text(text):
    if not text:
        return text
    
    text = html.escape(text)
    
    text = re.sub(r'\[([^\]]*)\]\([^\)]*$', r'\1', text)  # незакрытые ссылки
    text = re.sub(r'\*\*([^*]*)$', r'\1', text)  # незакрытый жирный текст
    text = re.sub(r'\*([^*]*)$', r'\1', text)    # незакрытый курсив
    text = re.sub(r'__([^_]*)$', r'\1', text)    # незакрытый подчеркнутый
    text = re.sub(r'`([^`]*)$', r'\1', text)     # незакрытый код
    
    text = re.sub(r'_{3,}', '___', text)
    
    return text

async def send_text_fallback(text, url=None, doc_title=None, fallback=False):
    try:
        caption = "📢 Новый пост из канала\n\n"
        
        if text:
            clean_text = re.sub(r'[`*_\[\]()]', '', text)
            text_preview = clean_text[:1000] + "..." if len(clean_text) > 1000 else clean_text
            caption += text_preview
        
        if url:
            caption += f"\n\nСсылка: {url}"
        
        if doc_title:
            caption += f"\n\n📎 {doc_title}"
        
        if fallback:
            caption += "\n\n⚠️ Оригинальное форматирование могло быть потеряно из-за ошибки разметки"
        
        await bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=caption,
            parse_mode=None,  # Без разметки
            disable_web_page_preview=False
        )
        return True
    except Exception as e:
        logging.error(f"❌ Фолбэк тоже не сработал: {e}")
        return False

def load_users():
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=4)

async def get_faceit_stats(steam_id):
    url = f"https://faceitfinder.com/profile/{steam_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")

            steam_name_tag = soup.select_one(".account-steam-name span")
            steam_name = steam_name_tag.text.strip() if steam_name_tag else "?"

            cs_hours_tag = soup.select_one("li.tick:-soup-contains('CS total hours') span")
            cs_hours = cs_hours_tag.text.strip() if cs_hours_tag else "?"

            faceit_nick_tag = soup.select_one(".account-faceit-title-username")
            faceit_nick = faceit_nick_tag.text.strip() if faceit_nick_tag else "?"

            faceit_level_tag = soup.select_one(".account-faceit-level img")
            if faceit_level_tag:
                alt_text = faceit_level_tag.get("alt", "")
                faceit_level = next((s for s in alt_text.split() if s.isdigit()), "?")
            else:
                faceit_level = "?"

            stats_tags = soup.select(f"#faceitbase_{steam_id} div.account-faceit-stats-single")
            stats = {}
            for tag in stats_tags:
                key, value = tag.text.split(":")
                stats[key.strip()] = value.strip()

            more_stats_tags = soup.select(f"#faceitmore_{steam_id} div.account-faceit-stats-single")
            for tag in more_stats_tags:
                key, value = tag.text.split(":")
                stats[key.strip()] = value.strip()

            return {
                "steam_name": steam_name,
                "cs_hours": cs_hours,
                "faceit_nick": faceit_nick,
                "faceit_level": faceit_level,
                **stats
            }



cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Отмена")]],
    resize_keyboard=True,
    one_time_keyboard=True
)

commands_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Создать сбор на CS2")],
        [KeyboardButton(text="Команды")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)



def load_events():
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {}  # файл есть, но пустой
                return json.loads(content)
        except json.JSONDecodeError:
            logging.warning(f"⚠️ Файл {EVENTS_FILE} повреждён — перезаписываю пустым.")
            return {}
    return {}

def save_events():
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(cs2_events, f, ensure_ascii=False, indent=2)



cs2_events = load_events()
MEMORY_FILE = "user_memory.json"



def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_memory(memory):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)



user_memory = load_memory()



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



@dp.message(lambda message: message.text == "Создать сбор на CS2")
async def ask_time(message: types.Message):
    await message.answer(
        "Напиши время сбора в формате ЧЧ:ММ, например 20:30",
        reply_markup=cancel_keyboard
    )
    cs2_events[message.from_user.id] = {
        "waiting_for_time": True,
        "chat_id": message.chat.id,
        "user_id": message.from_user.id
    }
    save_events()

@dp.message(lambda message: message.from_user.id in cs2_events and cs2_events[message.from_user.id].get("waiting_for_time"))
async def handle_event_time(message: types.Message):
    user_event = cs2_events.get(message.from_user.id)
    if not user_event or not user_event.get("waiting_for_time"):
        return 

    if message.text.lower() == "отмена":
        cs2_events.pop(message.from_user.id, None)
        save_events()
        await message.reply("❌ Создание сбора отменено.", reply_markup=commands_keyboard)
        return

    time_text = message.text.strip()
    try:
        hh, mm = map(int, time_text.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except:
        await message.reply("Неверный формат! Используй ЧЧ:ММ или нажми 'Отмена'.")
        return

    user_event["waiting_for_time"] = False
    user_event["time"] = f"{hh:02d}:{mm:02d}"
    save_events()
    await message.reply(f"✅ Сбор на CS2 назначен на {hh:02d}:{mm:02d}!", reply_markup=commands_keyboard)

    asyncio.create_task(check_event(user_event["chat_id"], hh, mm))

@dp.message(F.text == "/stats")
async def stats_command(message: Message):
    users = load_users()
    tg_id = str(message.from_user.id)
    if tg_id not in users:
        await message.reply("Вы не привязали SteamID. Используйте /bind <SteamID64>")
        return

    steam_id = users[tg_id]["steam_id"]
    stats = await get_faceit_stats(steam_id)
    if not stats:
        await message.reply("Не удалось получить статистику.")
        return

    text = (
        f"Steam: {stats['steam_name']}\n"
        f"CS total hours: {stats['cs_hours']}\n"
        f"Faceit: {stats['faceit_nick']}\n"
        f"Level: {stats['faceit_level']}\n"
        f"Matches: {stats.get('Matches','?')}\n"
        f"ELO: {stats.get('ELO','?')}\n"
        f"K/D: {stats.get('K/D','?')}\n"
        f"Winrate: {stats.get('Winrt','?')}\n"
        f"Wins: {stats.get('Wins','?')}\n"
        f"HS: {stats.get('HS','?')}\n"
    )

    await message.reply(text)

@dp.message(F.text.startswith("/bind"))
async def bind_steam(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await message.reply("Использование: /bind <SteamID64>")
        return

    steam_id = args[1]
    tg_id = str(message.from_user.id)

    users = load_users()

    stats = await get_faceit_stats(steam_id)
    faceit_nick = stats["faceit_nick"] if stats else "Неизвестно"

    users[tg_id] = {
        "steam_id": steam_id,
        "faceit_nick": faceit_nick
    }

    save_users(users)
    await message.reply(
        f"✅ Привязан SteamID: {steam_id}\n"
        f"Faceit ник: {faceit_nick}\n"
        f"Telegram ID: {tg_id}"
    )

@dp.message(Command("clear_events"))
async def clear_events(message: types.Message):
    cs2_events.clear()
    save_events()
    await message.reply("🗑 Все события очищены.", reply_markup=commands_keyboard)

@dp.message(Command("start"))
async def start_command(message: Message):
    chat_id = message.chat.id
    user = message.from_user.username or f"{message.from_user.first_name} ({message.from_user.id})"
    logging.info("Got message in chat %s from %s", chat_id, user)
    await message.reply(f"chat_id = `{chat_id}`")
    await message.answer("Привет! Иди нахуй, я занят.", reply_markup=commands_keyboard)

@dp.message(Command("list_all_stats"))
async def list_all_stats(message: types.Message):
    users = load_users()
    if not users:
        await message.reply("Пользователей пока нет.")
        return

    msg = "📊 Статистика всех привязанных пользователей:\n\n"

    for tg_id, info in users.items():
        try:
            member = await bot.get_chat_member(message.chat.id, int(tg_id))
            username = member.user.username or f"{member.user.first_name} ({tg_id})"
        except:
            username = f"Пользователь {tg_id}"

        steam_id = info["steam_id"]
        stats = await get_faceit_stats(steam_id)
        if not stats:
            msg += f"{username}: Не удалось получить статистику.\n\n"
            continue

        msg += (
            f"{username}:\n"
            f"Steam: {stats['steam_name']}\n"
            f"CS total hours: {stats['cs_hours']}\n"
            f"Faceit: {stats['faceit_nick']}\n"
            f"Level: {stats['faceit_level']}\n"
            f"Matches: {stats.get('Matches','?')}\n"
            f"ELO: {stats.get('ELO','?')}\n"
            f"K/D: {stats.get('K/D','?')}\n"
            f"Winrate: {stats.get('Winrt','?')}\n"
            f"Wins: {stats.get('Wins','?')}\n"
            f"HS: {stats.get('HS','?')}\n\n"
        )

    await message.reply(msg)

@dp.message(lambda message: message.text == "Команды")
async def show_commands(message: types.Message):
    text = (
        "Доступные команды:\n"
        "Обращение к боту через хуесос или габен\n"
        "/bind <SteamID64> — привязать SteamID к Telegram\n"
        "/stats — получить Faceit CS2 статистику\n"
        "/list_all_stats — получить Faceit CS2 статистику по всем игрокам\n"
        "/events — показать текущие сборы\n"
        "/clear_events — удалить все события"
    )
    await message.reply(text, reply_markup=commands_keyboard)

@dp.message(Command("events"))
async def show_events(message: Message):
    if not cs2_events:
        await message.reply("Событий пока нет.")
        return

    msg = "Текущие события:\n"
    for user_id, ev in cs2_events.items():
        status = "Ждём время" if ev.get("waiting_for_time") else f"Время: {ev.get('time')}"
        msg += f"- Пользователь {user_id}: {status}\n"

    clear_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Очистить события")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    await message.reply(msg, reply_markup=clear_keyboard)

@dp.message()
async def handle_message(message: Message):
    if not message.text:
        return

    if message.text == "Очистить события":
        cs2_events.clear()
        save_events()
        await message.reply("🗑 Все события очищены.", reply_markup=commands_keyboard)
        return

    user_id = str(message.from_user.id)
    text = message.text.strip().lower()

    # 😂 Анекдоты и шутки
    if any(trigger in text for trigger in JOKE_TRIGGERS):
        await message.reply(random.choice(JOKES), reply_markup=commands_keyboard)
        return

    # 🎯 Проверяем, что в сообщении есть триггер
    if not any(trigger in text for trigger in TRIGGER_WORDS):
        return

    users = load_users()
    user_stats = None
    if user_id in users:
        steam_id = users[user_id]["steam_id"]
        user_stats = await get_faceit_stats(steam_id)

    # 🧠 Загружаем историю сообщений пользователя
    history = user_memory.get(user_id, [])
    history.append({"role": "user", "content": text})
    history = history[-5:]  # храним последние 5 сообщений

    # ⚡ Генерируем ответ Mistral
    def mistral_sync():
        return client.chat.complete(
            model=MODEL,
            messages=[
                {"role": "system", "content": (
                    "Ты запуганный, опущенный бот, как петух на зоне, говоришь по-русски с небольшим количеством элементов геймерского сленга, также употребляешь русские маты. "
                    "Ты также получаешь статистику из Faceit и можешь её использовать в разговорах только если попросят, а не в каждом сообщении. ТОЛЬКО КОГДА ПОПРОСЯТ! "
                    f"Статистика пользователя: {user_stats if user_stats else 'Нет привязанного SteamID или статистика недоступна.'} "
                    "Тебя зовут Габен, ты не пытешься обидеть пользователя, он для тебя как блатные на зоне, а ты их прислуга. "
                    "Твои ответы короткие, с уважением ироничным: немного иронии, немного уважения. "
                )},
                *history
            ]
        )

    try:
        response = await asyncio.to_thread(mistral_sync)
        bot_reply = response.choices[0].message.content

        # 💾 Обновляем память
        history.append({"role": "assistant", "content": bot_reply})
        user_memory[user_id] = history
        save_memory(user_memory)

        # Пробуем MarkdownV2
        try:
            await message.reply(bot_reply, parse_mode="Markdown", reply_markup=commands_keyboard)
        except Exception as e:
            logging.warning(f"⚠️ MarkdownV2 не сработал, отправляем без разметки: {e}")
            await message.reply(bot_reply, parse_mode=None, reply_markup=commands_keyboard)
            
    except Exception as e:
        logging.error(f"❌ Ошибка при работе с Mistral: {e}")
        await message.reply("❌ Произошла ошибка при генерации ответа.", reply_markup=commands_keyboard)



async def check_event(chat_id, hh, mm):
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



async def main():
    # Отправляем сообщение о запуске
    try:
        await bot.send_message(
            chat_id=CHAT_ID, 
            text="🚀 **Бот запущен!**\n\n"
                 "✅ Мониторинг канала активирован\n"
                 "✅ Система событий работает\n"
                 "✅ Faceit статистика доступна\n"
                 "✅ AI-помощник готов к работе",
            parse_mode="Markdown"
        )
        logging.info("✅ Сообщение о запуске отправлено в чат")
    except Exception as e:
        logging.error(f"❌ Не удалось отправить сообщение о запуске: {e}")

    # Запускаем мониторинг канала в фоне
    asyncio.create_task(scheduled_channel_check())
    
    # Запускаем планировщик
    scheduler = AsyncIOScheduler()
    scheduler.start()

    print("🟢 Бот запущен и работает...")

    await bot(DeleteWebhook(drop_pending_updates=True))
    
    try:
        # Старт бота
        await dp.start_polling(bot, allowed_updates=["message"])
    except Exception as e:
        logging.error(f"❌ Ошибка при работе бота: {e}")
        # Отправляем сообщение об ошибке
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"❌ **Бот аварийно остановлен!**\n\nПричина: `{str(e)[:200]}`",
                parse_mode="Markdown"
            )
        except:
            pass
        raise
    finally:
        # Отправляем сообщение об остановке
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text="🛑 **Бот остановлен**\n\nВсе функции отключены",
                parse_mode="Markdown"
            )
            logging.info("✅ Сообщение об остановке отправлено в чат")
        except Exception as e:
            logging.error(f"❌ Не удалось отправить сообщение об остановке: {e}")
        
        print("🔴 Бот остановлен.")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("⏹️ Бот остановлен вручную")
    except Exception as e:
        logging.error(f"❌ Критическая ошибка: {e}")
    finally:
        try:
            loop.run_until_complete(bot.session.close())
        except:
            pass
        loop.close()


#"Ты токсичный, саркастичный бот, говоришь по-русски с небольшим количеством элементов геймерского сленга, также употребляешь русские маты. "
           #         "Ты также получаешь статистику из Faceit и можешь её использовать в разговорах только если попросят, а не в каждом сообщении. ТОЛЬКО КОГДА ПОПРОСЯТ! "
         #           f"Статистика пользователя: {user_stats if user_stats else 'Нет привязанного SteamID или статистика недоступна.'} "
         #           "Тебя зовут Габен, ты не пытешься обидеть пользователя, но говоришь всё как есть, без цензуры и чтобы было смешно. "
         #           "Твои ответы короткие, острые: немного рофла, немного боли. "