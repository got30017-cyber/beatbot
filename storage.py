import json
import telebot
from datetime import datetime

from config import (
    USERS_FILE, BATTLES_FILE, QUEUE_FILE, FINALS_FILE, SETTINGS_FILE,
    ROOMS, ROOM_LABELS, COINS_MAX, DAILY_LIMIT_FREE, DAILY_LIMIT_PRO,
    BEAT_COST_FREE, BEAT_COST_PRO,
)

# ─── Инициализация JSON-файлов ───────────────

def _empty_queue():
    return {r: {} for r in ROOMS}


for _f, _default in [
    (USERS_FILE,   {}),
    (BATTLES_FILE, {}),
    (QUEUE_FILE,   _empty_queue()),
    (FINALS_FILE,  {}),
    (SETTINGS_FILE, {}),
]:
    import os
    if not os.path.exists(_f):
        with open(_f, "w", encoding="utf-8") as _fh:
            json.dump(_default, _fh)


# ─── CRUD ────────────────────────────────────

def load_users() -> dict:
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_users(users: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=4)


def load_battles() -> dict:
    with open(BATTLES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_battles(battles: dict):
    with open(BATTLES_FILE, "w", encoding="utf-8") as f:
        json.dump(battles, f, ensure_ascii=False, indent=4)


def load_queue() -> dict:
    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if raw and not any(k in raw for k in ROOMS):
        return _empty_queue()
    return {r: raw.get(r, {}) for r in ROOMS}


def save_queue(queue: dict):
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=4)


def load_finals() -> dict:
    with open(FINALS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_finals(finals: dict):
    with open(FINALS_FILE, "w", encoding="utf-8") as f:
        json.dump(finals, f, ensure_ascii=False, indent=4)


# ─── Шаблон пользователя ─────────────────────

def default_user(nickname: str) -> dict:
    return {
        "nickname":         nickname,
        "role":             None,
        "rating":           0,
        "coins":            0,
        "wins":             0,
        "final_wins":       0,
        "battles_today":    0,
        "last_battle_date": None,
        "is_pro":           False,
        "votes_this_round": [],
        "bio":              "",
    }


# ─── Вспомогательные функции ─────────────────

def get_badge(wins: int, final_wins: int) -> str:
    if final_wins > 0:
        return "👑 Легенда"
    if wins >= 25:
        return "🥇 Золото"
    if wins >= 10:
        return "🥈 Серебро"
    if wins >= 3:
        return "🥉 Бронза"
    return "⚙️ Железо"


def get_room_wins(uid_str: str, battles: dict) -> dict:
    wins = {r: 0 for r in ROOMS}
    for b in battles.values():
        if b.get("status") != "finished":
            continue
        room = b.get("room")
        if not room or room not in wins:
            continue
        v1, v2 = b.get("votes1", 0), b.get("votes2", 0)
        if v1 > v2 and b.get("player1") == uid_str:
            wins[room] += 1
        elif v2 > v1 and b.get("player2") == uid_str:
            wins[room] += 1
    return wins


def check_daily_limit(user: dict) -> tuple:
    today = datetime.now().date().isoformat()
    if user.get("last_battle_date") != today:
        user["battles_today"]    = 0
        user["last_battle_date"] = today
    limit = DAILY_LIMIT_PRO if user.get("is_pro") else DAILY_LIMIT_FREE
    used  = user.get("battles_today", 0)
    return used >= limit, max(0, limit - used)


def user_in_queue(user_id: str, queue: dict):
    for room, entries in queue.items():
        if user_id in entries:
            return room
    return None


def get_menu(user_id: str, bot_ref=None):
    users = load_users()
    queue = load_queue()
    u     = users.get(str(user_id), {})
    role  = u.get("role", "beatmaker")
    in_q  = user_in_queue(str(user_id), queue) is not None

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    if role == "beatmaker":
        beat_btn = "✏️ Редактировать бит" if in_q else "🎵 Отправить бит"
        markup.add(
            telebot.types.KeyboardButton(beat_btn),
            telebot.types.KeyboardButton("⚔️ Мой батл"),
            telebot.types.KeyboardButton("🗳 Голосовать"),
            telebot.types.KeyboardButton("📊 Мой профиль"),
            telebot.types.KeyboardButton("🏆 Рейтинг"),
        )
    else:
        markup.add(
            telebot.types.KeyboardButton("🗳 Голосовать"),
            telebot.types.KeyboardButton("📊 Мой профиль"),
            telebot.types.KeyboardButton("🏆 Рейтинг"),
            telebot.types.KeyboardButton("🎯 Звёзды"),
        )
    return markup
