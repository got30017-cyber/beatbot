import json
import os
import sqlite3
import telebot
from datetime import datetime

from config import (
    BASE_DIR, DB_FILE,
    ROOMS, ROOM_LABELS, COINS_MAX, DAILY_LIMIT_FREE, DAILY_LIMIT_PRO,
    BEAT_COST_FREE, BEAT_COST_PRO,
)

# ─── Легаси JSON-файлы (только для одноразовой миграции) ──
_USERS_JSON    = os.path.join(BASE_DIR, "users.json")
_BATTLES_JSON  = os.path.join(BASE_DIR, "battles.json")
_QUEUE_JSON    = os.path.join(BASE_DIR, "queue.json")
_FINALS_JSON   = os.path.join(BASE_DIR, "finals.json")
_SETTINGS_JSON = os.path.join(BASE_DIR, "settings.json")


def _empty_queue():
    return {r: {} for r in ROOMS}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


# ─── Инициализация БД ────────────────────────

def init_db():
    conn = _connect()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id                TEXT PRIMARY KEY,
                    nickname          TEXT,
                    role              TEXT,
                    rating            INTEGER DEFAULT 0,
                    coins             INTEGER DEFAULT 3,
                    wins              INTEGER DEFAULT 0,
                    final_wins        INTEGER DEFAULT 0,
                    battles_today     INTEGER DEFAULT 0,
                    last_battle_date  TEXT,
                    is_pro            INTEGER DEFAULT 0,
                    bio               TEXT DEFAULT '',
                    votes_this_round  TEXT DEFAULT '[]'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS battles (
                    id                  TEXT PRIMARY KEY,
                    player1             TEXT,
                    player2             TEXT,
                    beat1_file_id       TEXT,
                    beat2_file_id       TEXT,
                    votes1              INTEGER DEFAULT 0,
                    votes2              INTEGER DEFAULT 0,
                    voters              TEXT DEFAULT '{}',
                    status              TEXT DEFAULT 'active',
                    room                TEXT,
                    start_time          TEXT,
                    end_time            TEXT,
                    counted_for_final   INTEGER DEFAULT 0,
                    included_in_final   TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    user_id  TEXT PRIMARY KEY,
                    room     TEXT,
                    file_id  TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS finals (
                    id          TEXT PRIMARY KEY,
                    room        TEXT,
                    status      TEXT DEFAULT 'active',
                    beats       TEXT DEFAULT '[]',
                    votes       TEXT DEFAULT '{}',
                    voters      TEXT DEFAULT '[]',
                    start_time  TEXT,
                    end_time    TEXT,
                    winner_id   TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key    TEXT PRIMARY KEY,
                    value  TEXT
                )
            """)
    finally:
        conn.close()


# ─── Миграция из JSON ────────────────────────

def migrate_from_json():
    init_db()

    conn = _connect()
    try:
        already = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    finally:
        conn.close()
    if already > 0:
        return

    def _read(path):
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return data or None

    users     = _read(_USERS_JSON)
    battles_  = _read(_BATTLES_JSON)
    raw_queue = _read(_QUEUE_JSON)
    finals_   = _read(_FINALS_JSON)
    settings_ = _read(_SETTINGS_JSON)

    if not any([users, battles_, raw_queue, finals_, settings_]):
        return

    if users:
        save_users(users)
    if battles_:
        save_battles(battles_)
    if raw_queue:
        if not any(k in raw_queue for k in ROOMS):
            queue = _empty_queue()
        else:
            queue = {r: raw_queue.get(r, {}) for r in ROOMS}
        save_queue(queue)
    if finals_:
        save_finals(finals_)
    if settings_:
        save_settings(settings_)

    for path in (_USERS_JSON, _BATTLES_JSON, _QUEUE_JSON, _FINALS_JSON, _SETTINGS_JSON):
        if os.path.exists(path):
            os.rename(path, path + ".bak")


# ─── Users ────────────────────────────────────

def load_users() -> dict:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM users").fetchall()
    finally:
        conn.close()
    return {
        row["id"]: {
            "nickname":         row["nickname"],
            "role":             row["role"],
            "rating":           row["rating"],
            "coins":            row["coins"],
            "wins":             row["wins"],
            "final_wins":       row["final_wins"],
            "battles_today":    row["battles_today"],
            "last_battle_date": row["last_battle_date"],
            "is_pro":           bool(row["is_pro"]),
            "votes_this_round": json.loads(row["votes_this_round"] or "[]"),
            "bio":              row["bio"] or "",
        }
        for row in rows
    }


def save_users(users: dict):
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM users")
            conn.executemany(
                """INSERT INTO users
                   (id, nickname, role, rating, coins, wins, final_wins,
                    battles_today, last_battle_date, is_pro, bio, votes_this_round)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        uid,
                        u.get("nickname"),
                        u.get("role"),
                        u.get("rating", 0),
                        u.get("coins", 0),
                        u.get("wins", 0),
                        u.get("final_wins", 0),
                        u.get("battles_today", 0),
                        u.get("last_battle_date"),
                        int(bool(u.get("is_pro"))),
                        u.get("bio", ""),
                        json.dumps(u.get("votes_this_round", []), ensure_ascii=False),
                    )
                    for uid, u in users.items()
                ],
            )
    finally:
        conn.close()


# ─── Battles ──────────────────────────────────

def load_battles() -> dict:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM battles").fetchall()
    finally:
        conn.close()
    return {
        row["id"]: {
            "player1":           row["player1"],
            "player2":           row["player2"],
            "beat1_file_id":     row["beat1_file_id"],
            "beat2_file_id":     row["beat2_file_id"],
            "votes1":            row["votes1"],
            "votes2":            row["votes2"],
            "voters":            json.loads(row["voters"] or "{}"),
            "status":            row["status"],
            "room":              row["room"],
            "start_time":        row["start_time"],
            "end_time":          row["end_time"],
            "counted_for_final": bool(row["counted_for_final"]),
            "included_in_final": row["included_in_final"],
        }
        for row in rows
    }


def save_battles(battles: dict):
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM battles")
            conn.executemany(
                """INSERT INTO battles
                   (id, player1, player2, beat1_file_id, beat2_file_id,
                    votes1, votes2, voters, status, room, start_time, end_time,
                    counted_for_final, included_in_final)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        bid,
                        b.get("player1"),
                        b.get("player2"),
                        b.get("beat1_file_id"),
                        b.get("beat2_file_id"),
                        b.get("votes1", 0),
                        b.get("votes2", 0),
                        json.dumps(b.get("voters", {}), ensure_ascii=False),
                        b.get("status", "active"),
                        b.get("room"),
                        b.get("start_time"),
                        b.get("end_time"),
                        int(bool(b.get("counted_for_final"))),
                        b.get("included_in_final"),
                    )
                    for bid, b in battles.items()
                ],
            )
    finally:
        conn.close()


# ─── Queue ────────────────────────────────────

def load_queue() -> dict:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM queue").fetchall()
    finally:
        conn.close()
    queue = _empty_queue()
    for row in rows:
        if row["room"] in queue:
            queue[row["room"]][row["user_id"]] = row["file_id"]
    return queue


def save_queue(queue: dict):
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM queue")
            conn.executemany(
                "INSERT INTO queue (user_id, room, file_id) VALUES (?, ?, ?)",
                [
                    (uid, room, file_id)
                    for room, entries in queue.items()
                    for uid, file_id in entries.items()
                ],
            )
    finally:
        conn.close()


# ─── Finals ───────────────────────────────────

def load_finals() -> dict:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM finals").fetchall()
    finally:
        conn.close()
    return {
        row["id"]: {
            "room":       row["room"],
            "status":     row["status"],
            "beats":      json.loads(row["beats"] or "[]"),
            "votes":      json.loads(row["votes"] or "{}"),
            "voters":     json.loads(row["voters"] or "[]"),
            "start_time": row["start_time"],
            "end_time":   row["end_time"],
            "winner_id":  row["winner_id"],
        }
        for row in rows
    }


def save_finals(finals: dict):
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM finals")
            conn.executemany(
                """INSERT INTO finals
                   (id, room, status, beats, votes, voters, start_time, end_time, winner_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        fid,
                        f.get("room"),
                        f.get("status", "active"),
                        json.dumps(f.get("beats", []), ensure_ascii=False),
                        json.dumps(f.get("votes", {}), ensure_ascii=False),
                        json.dumps(f.get("voters", []), ensure_ascii=False),
                        f.get("start_time"),
                        f.get("end_time"),
                        f.get("winner_id"),
                    )
                    for fid, f in finals.items()
                ],
            )
    finally:
        conn.close()


# ─── Settings ─────────────────────────────────

def load_settings() -> dict:
    conn = _connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()
    settings = {}
    for row in rows:
        try:
            settings[row["key"]] = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            settings[row["key"]] = row["value"]
    return settings


def save_settings(settings: dict):
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM settings")
            conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                [(k, json.dumps(v, ensure_ascii=False)) for k, v in settings.items()],
            )
    finally:
        conn.close()


# ─── Шаблон пользователя ─────────────────────

def default_user(nickname: str) -> dict:
    return {
        "nickname":         nickname,
        "role":             None,
        "rating":           0,
        "coins":            3,
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
