import os
import time
import logging
import aiohttp

FACEIT_API_KEY = os.environ.get("FACEIT_API_KEY", "")
BASE_URL = "https://open.faceit.com/data/v4"
CACHE_DURATION = 3000
_api_cache = {}

async def _api_get(session, path, params=None):
    if not FACEIT_API_KEY:
        logging.error("FACEIT_API_KEY not configured")
        return None
    headers = {
        "Authorization": f"Bearer {FACEIT_API_KEY}",
        "Accept": "application/json",
        "User-Agent": "telegram-cs2-bot/1.0"
    }
    url = BASE_URL + path
    try:
        async with session.get(url, headers=headers, params=params, timeout=15) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logging.warning(f"Faceit API {url} returned status {resp.status}")
                return None
    except Exception as e:
        logging.exception(f"Error calling Faceit API {url}: {e}")
        return None

async def get_player_by_nickname(nickname, game="csgo"):
    """Возвращает объект игрока по нику (или None)."""
    async with aiohttp.ClientSession() as session:
        data = await _api_get(session, "/players", params={"nickname": nickname, "game": game})
        return data

async def get_player_by_game_player_id(game_player_id, game="csgo"):
    """Найти игрока по игровому id (например SteamID)"""
    async with aiohttp.ClientSession() as session:
        data = await _api_get(session, "/players", params={"game": game, "game_player_id": game_player_id})
        return data

async def get_player_stats(player_id, game="csgo"):
    """Возвращает статистику игрока (lifetime и др.)"""
    async with aiohttp.ClientSession() as session:
        data = await _api_get(session, f"/players/{player_id}/stats", params={"game": game})
        return data

async def get_stats_cached(identifier):
    """Универсальная обёртка: identifier может быть faceit_nick или steam id.
    Возвращает словарь с ключами: faceit_nick, faceit_level, ELO, Matches, Wins, K/D, Winrt, source
    """
    now = time.time()
    if identifier in _api_cache:
        ts, val = _api_cache[identifier]
        if now - ts < CACHE_DURATION:
            return val

    # Попробуем найти игрока по нику
    player = None
    # Если идентификатор похож на steam id (начинается с STEAM_ или содержит только цифры of 17 len), попробуем game_player_id
    try:
        if identifier.startswith("STEAM_") or identifier.isdigit():
            player = await get_player_by_game_player_id(identifier)
        if not player:
            player = await get_player_by_nickname(identifier)
    except Exception as e:
        logging.warning(f"Faceit client lookup failed for {identifier}: {e}")
        player = None

    result = None
    if player:
        # У игрока может быть поле player_id или playerId
        player_id = player.get("player_id") or player.get("playerId") or player.get("playerId")
        nickname = player.get("nickname") or player.get("player_nickname") or player.get("playerName") or player.get("nickname")
        # ELO и level могут находиться в player['games']["csgo"]
        games = player.get("games") or {}
        game_info = games.get("csgo") or games.get("CS:GO") or games.get("cs2") or {}
        elo = None
        level = None
        if isinstance(game_info, dict):
            elo = game_info.get("faceit_elo") or game_info.get("elo") or game_info.get("skill_level")
            level = game_info.get("skill_level") or game_info.get("faceit_level")
        # Получаем подробную статистику
        stats_data = None
        if player_id:
            stats_data = await get_player_stats(player_id)
        # Формируем результат аккуратно
        result = {
            "faceit_nick": nickname or identifier,
            "player_id": player_id,
            "faceit_level": str(level) if level else "?",
            "ELO": str(elo) if elo else "?",
            "Matches": "?",
            "Wins": "?",
            "K/D": "?",
            "Winrt": "?",
            "source": "Faceit API"
        }
        # Разбираем stats_data если есть
        try:
            if stats_data and isinstance(stats_data, dict):
                lifetime = stats_data.get("lifetime") or {}
                # lifetime часто содержит keys with stats
                # Попробуем получить matches, wins, kd и win rate
                # Примеры полей: "Matches", "Wins", "Average K/D", "Win Rate"
                # Будем проверять разные варианты
                # Matches
                m_keys = [k for k in lifetime.keys() if k.lower().startswith("matches") or k.lower() == "matches"]
                if m_keys:
                    result["Matches"] = str(lifetime.get(m_keys[0], "?"))
                # Wins
                w_keys = [k for k in lifetime.keys() if "win" in k.lower() and k.lower() != "win rate"]
                if w_keys:
                    result["Wins"] = str(lifetime.get(w_keys[0], "?"))
                # K/D or Average K/D
                kd_keys = [k for k in lifetime.keys() if "k/d" in k.lower() or "average k/d" in k.lower() or "average kd" in k.lower()]
                if kd_keys:
                    result["K/D"] = str(lifetime.get(kd_keys[0], "?"))
                # Win rate
                wr_keys = [k for k in lifetime.keys() if "win rate" in k.lower() or k.lower().endswith("%")]
                if wr_keys:
                    # some values include % or numbers
                    result["Winrt"] = str(lifetime.get(wr_keys[0], "?"))
        except Exception:
            pass

    # Сохраняем в кеш
    _api_cache[identifier] = (now, result)
    return result


def clear_cache(identifier=None):
    """Очистить кеш для конкретного идентификатора или полностью, если identifier is None"""
    if identifier is None:
        _api_cache.clear()
    else:
        _api_cache.pop(identifier, None)
