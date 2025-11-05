import os
import aiohttp
import asyncio
import time
import logging
from typing import Optional, Dict, Any

FACEIT_API_KEY = os.getenv("FACEIT_API_KEY") or os.getenv("FACEIT_KEY") or os.getenv("FACEIT")  # Railway var
BASE_URL = "https://open.faceit.com/data/v4"

# Настройки кеша
_CACHE: Dict[str, tuple] = {}  # nick_or_id -> (data_dict, timestamp)
_CACHE_LOCK = asyncio.Lock()
CACHE_TTL = int(os.getenv("FACEIT_CACHE_TTL", "300"))  # seconds, по умолчанию 5 минут
REQUEST_TIMEOUT = int(os.getenv("FACEIT_REQ_TIMEOUT", "12"))  # seconds

HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"} if FACEIT_API_KEY else {}

# Escape для MarkdownV2 (копия минимальная, безопасная)
def escape_md_v2(text: str) -> str:
    if not text:
        return ""
    to_escape = r'_*[]()~`>#+-=|{}.!'
    return "".join(f'\\{c}' if c in to_escape else c for c in str(text))

async def _fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None) -> Optional[dict]:
    try:
        async with session.get(url, params=params, timeout=REQUEST_TIMEOUT, headers=HEADERS) as resp:
            status = resp.status
            if status == 200:
                return await resp.json()
            elif status == 404:
                return None
            elif status == 429:
                # Rate limited: honor Retry-After if present, else sleep a bit and retry once
                retry_after = int(resp.headers.get("Retry-After", "2"))
                logging.warning(f"Faceit rate-limit (429). Retry after {retry_after}s.")
                await asyncio.sleep(retry_after)
                async with session.get(url, params=params, timeout=REQUEST_TIMEOUT, headers=HEADERS) as resp2:
                    if resp2.status == 200:
                        return await resp2.json()
                    return None
            else:
                logging.warning(f"Faceit: unexpected status {status} for {url}")
                return None
    except Exception as e:
        logging.exception(f"Faceit request failed for {url}: {e}")
        return None

async def _get_player_by_nickname(nickname: str) -> Optional[dict]:
    """GET /players?nickname=<nick>"""
    if not nickname:
        return None
    url = f"{BASE_URL}/players"
    params = {"nickname": nickname}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await _fetch_json(session, url, params=params)

async def _get_player_by_steamid(steam_id: str) -> Optional[dict]:
    """GET /players?game_player_id=<steamid> — fallback"""
    if not steam_id:
        return None
    url = f"{BASE_URL}/players"
    params = {"game_player_id": steam_id, "game": "cs2"}  # пробуем cs2
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await _fetch_json(session, url, params=params)

async def _get_stats_by_player_id(player_id: str, game: str = "cs2") -> Optional[dict]:
    """GET /players/{player_id}/stats/{game}"""
    if not player_id:
        return None
    url = f"{BASE_URL}/players/{player_id}/stats/{game}"
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await _fetch_json(session, url)

async def _get_recent_matches(player_id: str, game: str = "cs2", limit:int=5) -> Optional[dict]:
    """GET /players/{player_id}/history?game={game}&offset=0&size={limit}"""
    if not player_id:
        return None
    url = f"{BASE_URL}/players/{player_id}/history"
    params = {"game": game, "size": limit}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await _fetch_json(session, url, params=params)

async def _fetch_player_full(nickname: Optional[str] = None, steam_id: Optional[str] = None) -> Optional[dict]:
    """
    Возвращает dict с ключами:
      - player (от /players)
      - stats (от /players/{id}/stats/cs2)
      - recent (от /players/{id}/history)
    """
    if not FACEIT_API_KEY:
        logging.error("FACEIT_API_KEY не настроен в окружении")
        return None

    player = None
    if nickname:
        player = await _get_player_by_nickname(nickname)
    if not player and steam_id:
        player = await _get_player_by_steamid(steam_id)
    if not player:
        return None

    player_id = player.get("player_id") or player.get("playerId") or player.get("id")
    if not player_id:
        return {"player": player, "stats": None, "recent": None}

    stats = await _get_stats_by_player_id(player_id)
    recent = await _get_recent_matches(player_id)

    return {"player": player, "stats": stats, "recent": recent}

async def get_player_stats(nickname: Optional[str] = None, steam_id: Optional[str] = None, use_cache: bool = True) -> Optional[dict]:
    """
    Public: возвращает структурированный объект с сырыми данными и строкой для бота.
    nickname имеет приоритет. Если оба None — вернёт None.
    """
    key = nickname or steam_id
    if not key:
        return None

    now = time.time()
    if use_cache:
        async with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached and now - cached[1] < CACHE_TTL:
                return cached[0]

    payload = await _fetch_player_full(nickname=nickname, steam_id=steam_id)
    if not payload:
        return None

    # Обработаем lifetime в удобный словарь
    stats = payload.get("stats") or {}
    lifetime_raw = stats.get("lifetime")
    lifetime = {}
    # Возможны две формы: list of dicts с label/value или прямо dict
    if isinstance(lifetime_raw, list):
        for item in lifetime_raw:
            label = item.get("label") or item.get("name")
            value = item.get("value")
            if label:
                lifetime[label] = value
    elif isinstance(lifetime_raw, dict):
        lifetime = lifetime_raw
    else:
        lifetime = {}

    # player info
    player = payload.get("player") or {}
    nickname_res = player.get("nickname") or player.get("faceit_nickname") or player.get("profile") or player.get("player_id")
    country = player.get("country") or player.get("country_id") or player.get("countryCode")
    avatar = player.get("avatar") or player.get("avatar_url")
    games = player.get("games") or {}
    games_cs2 = games.get("cs2") if isinstance(games, dict) else None
    elo = None
    level = None
    if games_cs2:
        elo = games_cs2.get("faceit_elo") or games_cs2.get("faceitElo")
        level = games_cs2.get("skill_level") or games_cs2.get("skillLevel")

    # Recent matches summary (some readable string)
    recent_results = []
    recent = payload.get("recent")
    if recent and isinstance(recent, dict):
        items = recent.get("items") or recent.get("results") or recent.get("matches") or []
        for it in (items[:5] if isinstance(items, list) else []):
            # try to get a short readable result
            outcome = it.get("rounds") or it.get("match_result") or it.get("result") or it.get("outcome")
            # many structures: try to find 'result' or 'match_id'
            r = it.get("result") or it.get("match_result") or it.get("outcome")
            # fallback to 'teams' info
            if r:
                recent_results.append(str(r))
            else:
                # try to infer winner field
                winner = it.get("winner")
                if winner:
                    recent_results.append(str(winner))
                else:
                    recent_results.append(it.get("match_id", "match"))

    # Собираем удобный словарь
    result = {
        "player_raw": player,
        "stats_raw": stats,
        "lifetime": lifetime,
        "nickname": nickname_res or nickname or "",
        "country": country or "",
        "avatar": avatar,
        "elo": elo,
        "level": level,
        "recent_results_list": recent_results,
        "fetched_at": now
    }

    async with _CACHE_LOCK:
        _CACHE[key] = (result, now)

    return result

async def get_player_text_card(nickname: Optional[str] = None, steam_id: Optional[str] = None, use_cache: bool = True) -> Optional[str]:
    """
    Возвращает подготовленную строку в MarkdownV2 для отправки в Telegram.
    """
    data = await get_player_stats(nickname=nickname, steam_id=steam_id, use_cache=use_cache)
    if not data:
        return None

    nick = data.get("nickname") or ""
    elo = data.get("elo") or "—"
    level = data.get("level") or "—"
    lifetime = data.get("lifetime") or {}

    # Популярные метрики, которые часто есть
    matches = lifetime.get("Matches") or lifetime.get("Matches Played") or lifetime.get("Total matches") or lifetime.get("Matches")
    wins = lifetime.get("Wins") or lifetime.get("Total wins")
    winrate = lifetime.get("Win Rate") or lifetime.get("Win Rate %") or lifetime.get("Win Rate %")
    kd = lifetime.get("Average K/D Ratio") or lifetime.get("K/D Ratio") or lifetime.get("K/D")
    hs = lifetime.get("Average Headshots %") or lifetime.get("Headshots %") or lifetime.get("HS %")
    adr = lifetime.get("Average Damage Round") or lifetime.get("Avg. Damage") or lifetime.get("ADR")

    recent = data.get("recent_results_list", [])
    recent_str = " ".join(recent) if recent else "—"

    country = data.get("country") or "—"

    # Форматируем аккуратно и коротко (около 12–15 строк)
    lines = []
    lines.append(f"🎮 *{escape_md_v2(nick)}*")
    lines.append(f"📍 Страна: `{escape_md_v2(country)}`")
    lines.append(f"📈 Уровень: `{escape_md_v2(str(level))}`  •  ELO: `{escape_md_v2(str(elo))}`")
    lines.append(f"🕹 Матчей: `{escape_md_v2(str(matches or '—'))}`  •  Побед: `{escape_md_v2(str(wins or '—'))}`  •  Winrate: `{escape_md_v2(str(winrate or '—'))}`")
    lines.append(f"⚔️ K/D: `{escape_md_v2(str(kd or '—'))}`  •  HS%: `{escape_md_v2(str(hs or '—'))}`  •  ADR: `{escape_md_v2(str(adr or '—'))}`")
    lines.append(f"🟢 Последние: `{escape_md_v2(recent_str)}`")
    lines.append(f"🔗 Источник: `Faceit API`")
    lines.append(f"_Обновлено: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data.get('fetched_at', time.time())))}_")

    text = "\n".join(lines)
    return text

async def get_multiple_players_text(users: dict, use_cache: bool = True, limit:int=30) -> str:
    """
    Пробегает словарём users (как в users.json) и возвращает объединённый текст.
    Делает паузу между запросами, чтобы не бомбить API.
    """
    pieces = []
    cnt = 0
    for tg_id, info in users.items():
        if cnt >= limit:
            break
        nick = info.get("faceit_nick")
        steam = info.get("steam_id")
        # если есть faceit_nick — используем его, иначе пробуем steam id
        card = await get_player_text_card(nick, steam, use_cache=use_cache)
        if card:
            # single-line heading with nick, then one-line stats summary — сделаем компактнее
            # возьмём первые 3 строки чтобы не засорять
            short = "\n".join(card.splitlines()[:4])
            pieces.append(short)
        else:
            pieces.append(f"🎮 `{escape_md_v2(nick or steam or 'unknown')}` — `No data`")
        cnt += 1
        await asyncio.sleep(0.4)  # пауза между запросами
    return "\n\n".join(pieces)

def clear_cache():
    """Очистить кеш (синхронно)"""
    global _CACHE
    _CACHE = {}