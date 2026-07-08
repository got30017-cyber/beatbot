import json
import os
import sqlite3
import telebot
from datetime import datetime

from config import (
    BASE_DIR, DB_FILE,
    ROOMS, ROOM_LABELS, DAILY_LIMIT_FREE, DAILY_LIMIT_PRO,
    FEEDBACK_CATEGORIES, RATING_POINTS,
    REFERRAL_RATING_BONUS, REFERRAL_TICKET_DISCOUNT,
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


def _ensure_user_columns(conn: sqlite3.Connection):
    """Добавляет новые колонки users в БД, созданную до их появления."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    new_columns = {
        "messages_sent_today": "INTEGER DEFAULT 0",
        "last_message_date":   "TEXT",
        "last_notified_at":    "TEXT",
        "ticket_progress":     "INTEGER DEFAULT 0",
        "ticket_required":     "INTEGER DEFAULT 0",
        "last_weekly_vote":    "TEXT",
        "referred_by":         "TEXT",
        "referral_rewarded":   "INTEGER DEFAULT 0",
        "referral_count":      "INTEGER DEFAULT 0",
        "ticket_discount":     "INTEGER DEFAULT 0",
    }
    for col, decl in new_columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")


def _ensure_battle_columns(conn: sqlite3.Connection):
    """Добавляет новые колонки battles в БД, созданную до их появления."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(battles)").fetchall()}
    new_columns = {
        "feedback":    "TEXT DEFAULT '{}'",
        "votes":       "TEXT DEFAULT '{}'",
        "predictions": "TEXT DEFAULT '{}'",
        "beat1_id":    "TEXT",
        "beat2_id":    "TEXT",
    }
    for col, decl in new_columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE battles ADD COLUMN {col} {decl}")


def _ensure_queue_schema(conn: sqlite3.Connection):
    """Очередь теперь ключуется по beat_id (а не user_id) — нужно для матчинга
    по битам (история встреч, статус карьеры). Старая схема с этим несовместима.

    Очередь — эфемерные данные ожидания матчинга, не история: при обнаружении
    старой структуры просто пересоздаём таблицу пустой. Максимум потеряем
    непойманные ожидающие биты — их авторы отправят заново одной кнопкой.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(queue)").fetchall()}
    if existing and "beat_id" not in existing:
        conn.execute("DROP TABLE queue")


def _ensure_beat_columns(conn: sqlite3.Connection):
    """Добавляет новые колонки beats в БД, созданную до их появления."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(beats)").fetchall()}
    new_columns = {
        "week_id":         "TEXT",
        "qualified_for":   "TEXT",
        "final_placement": "INTEGER",
    }
    for col, decl in new_columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE beats ADD COLUMN {col} {decl}")


def _ensure_slot_columns(conn: sqlite3.Connection):
    """Добавляет новые колонки slots в БД, созданную до их появления."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(slots)").fetchall()}
    new_columns = {
        "started_at":       "TEXT",
        "voting_ends_at":   "TEXT",
        "registered_beats": "TEXT DEFAULT '[]'",
        "battle_ids":       "TEXT DEFAULT '[]'",
        "finished_at":      "TEXT",
    }
    for col, decl in new_columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE slots ADD COLUMN {col} {decl}")


# ─── Инициализация БД ────────────────────────

def init_db():
    conn = _connect()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id                    TEXT PRIMARY KEY,
                    nickname              TEXT,
                    role                  TEXT,
                    rating                INTEGER DEFAULT 0,
                    coins                 INTEGER DEFAULT 3,
                    wins                  INTEGER DEFAULT 0,
                    final_wins            INTEGER DEFAULT 0,
                    battles_today         INTEGER DEFAULT 0,
                    last_battle_date      TEXT,
                    is_pro                INTEGER DEFAULT 0,
                    bio                   TEXT DEFAULT '',
                    votes_this_round      TEXT DEFAULT '[]',
                    messages_sent_today   INTEGER DEFAULT 0,
                    last_message_date     TEXT,
                    last_notified_at      TEXT,
                    ticket_progress       INTEGER DEFAULT 0,
                    ticket_required       INTEGER DEFAULT 0,
                    last_weekly_vote      TEXT,
                    referred_by           TEXT,
                    referral_rewarded     INTEGER DEFAULT 0,
                    referral_count        INTEGER DEFAULT 0,
                    ticket_discount       INTEGER DEFAULT 0
                )
            """)
            _ensure_user_columns(conn)
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
                    included_in_final   TEXT,
                    feedback            TEXT DEFAULT '{}',
                    votes               TEXT DEFAULT '{}',
                    predictions         TEXT DEFAULT '{}',
                    beat1_id            TEXT,
                    beat2_id            TEXT
                )
            """)
            _ensure_battle_columns(conn)
            _ensure_queue_schema(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    beat_id  TEXT PRIMARY KEY,
                    user_id  TEXT,
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pair_ratings (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id            TEXT NOT NULL,
                    battle_id          TEXT NOT NULL,
                    vote_side          TEXT NOT NULL,
                    pred_side          TEXT NOT NULL,
                    shown_at           TEXT,
                    voted_at           TEXT,
                    predicted_at       TEXT NOT NULL,
                    time_on_pair       REAL,
                    diverged           INTEGER NOT NULL,
                    winning_side       TEXT,
                    prediction_correct INTEGER,
                    UNIQUE(user_id, battle_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS beats (
                    id               TEXT PRIMARY KEY,
                    author_id        TEXT NOT NULL,
                    file_id          TEXT NOT NULL,
                    title            TEXT DEFAULT '',
                    status           TEXT NOT NULL,
                    battles_played   INTEGER DEFAULT 0,
                    wins             INTEGER DEFAULT 0,
                    losses           INTEGER DEFAULT 0,
                    draws            INTEGER DEFAULT 0,
                    predicted_for    INTEGER DEFAULT 0,
                    created_at       TEXT NOT NULL,
                    finished_at      TEXT,
                    week_id          TEXT,
                    qualified_for    TEXT,
                    final_placement  INTEGER
                )
            """)
            _ensure_beat_columns(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_beats_author ON beats(author_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_beats_status ON beats(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_beats_week ON beats(week_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_beats_finished ON beats(status, finished_at)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS weeks (
                    id                TEXT PRIMARY KEY,
                    status            TEXT NOT NULL,
                    started_at        TEXT NOT NULL,
                    closes_at         TEXT NOT NULL,
                    voting_ends_at    TEXT,
                    participants      TEXT DEFAULT '[]',
                    votes             TEXT DEFAULT '{}',
                    predictions       TEXT DEFAULT '{}',
                    voters            TEXT DEFAULT '[]',
                    winner_beat_id    TEXT,
                    finished_at       TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS slots (
                    id                  TEXT PRIMARY KEY,
                    status              TEXT NOT NULL,
                    created_at          TEXT NOT NULL,
                    started_at          TEXT,
                    voting_ends_at      TEXT,
                    registered_beats    TEXT DEFAULT '[]',
                    battle_ids          TEXT DEFAULT '[]',
                    finished_at         TEXT
                )
            """)
            _ensure_slot_columns(conn)
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
            "nickname":            row["nickname"],
            "role":                row["role"],
            "rating":              row["rating"],
            "wins":                row["wins"],
            "final_wins":          row["final_wins"],
            "battles_today":       row["battles_today"],
            "last_battle_date":    row["last_battle_date"],
            "is_pro":              bool(row["is_pro"]),
            "votes_this_round":    json.loads(row["votes_this_round"] or "[]"),
            "bio":                 row["bio"] or "",
            "messages_sent_today": row["messages_sent_today"] or 0,
            "last_message_date":   row["last_message_date"],
            "last_notified_at":    row["last_notified_at"],
            "ticket_progress":     row["ticket_progress"] or 0,
            "ticket_required":     row["ticket_required"] or 0,
            "last_weekly_vote":    row["last_weekly_vote"],
            "referred_by":         row["referred_by"],
            "referral_rewarded":   bool(row["referral_rewarded"]),
            "referral_count":      row["referral_count"] or 0,
            "ticket_discount":     row["ticket_discount"] or 0,
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
                   (id, nickname, role, rating, wins, final_wins,
                    battles_today, last_battle_date, is_pro, bio, votes_this_round,
                    messages_sent_today, last_message_date, last_notified_at,
                    ticket_progress, ticket_required, last_weekly_vote,
                    referred_by, referral_rewarded, referral_count, ticket_discount)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        uid,
                        u.get("nickname"),
                        u.get("role"),
                        u.get("rating", 0),
                        u.get("wins", 0),
                        u.get("final_wins", 0),
                        u.get("battles_today", 0),
                        u.get("last_battle_date"),
                        int(bool(u.get("is_pro"))),
                        u.get("bio", ""),
                        json.dumps(u.get("votes_this_round", []), ensure_ascii=False),
                        u.get("messages_sent_today", 0),
                        u.get("last_message_date"),
                        u.get("last_notified_at"),
                        u.get("ticket_progress", 0),
                        u.get("ticket_required", 0),
                        u.get("last_weekly_vote"),
                        u.get("referred_by"),
                        int(bool(u.get("referral_rewarded"))),
                        u.get("referral_count", 0),
                        u.get("ticket_discount", 0),
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
            "feedback":          json.loads(row["feedback"] or "{}"),
            "votes":             json.loads(row["votes"] or "{}"),
            "predictions":       json.loads(row["predictions"] or "{}"),
            "beat1_id":          row["beat1_id"],
            "beat2_id":          row["beat2_id"],
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
                    counted_for_final, included_in_final, feedback, votes, predictions,
                    beat1_id, beat2_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        json.dumps(b.get("feedback", {}), ensure_ascii=False),
                        json.dumps(b.get("votes", {}), ensure_ascii=False),
                        json.dumps(b.get("predictions", {}), ensure_ascii=False),
                        b.get("beat1_id"),
                        b.get("beat2_id"),
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
    """queue: {room: {user_id: file_id}} — внешний формат не меняется, чтобы не
    трогать вызывающий код. Внутри строка требует beat_id — берём его через
    find_active_beat_by_user (инвариант: у пользователя максимум один активный
    бит, и при постановке в очередь он уже в статусе 'queued'). Если бит не
    найден или уже не 'queued' — строка пропускается защитно, это не должно
    происходить при соблюдении инварианта.
    """
    rows = []
    for room, entries in queue.items():
        for uid, file_id in entries.items():
            beat = find_active_beat_by_user(uid)
            if not beat or beat["status"] != "queued":
                continue
            rows.append((beat["id"], uid, room, file_id))

    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM queue")
            conn.executemany(
                "INSERT INTO queue (beat_id, user_id, room, file_id) VALUES (?, ?, ?, ?)",
                rows,
            )
    finally:
        conn.close()


# ─── Биты (карьера) ───────────────────────────
# Точечные операции, НЕ load-all/save-all — таблица растёт с каждым битом.

def _beat_row_to_dict(row) -> dict:
    return {
        "id":              row["id"],
        "author_id":       row["author_id"],
        "file_id":         row["file_id"],
        "title":           row["title"] or "",
        "status":          row["status"],
        "battles_played":  row["battles_played"],
        "wins":            row["wins"],
        "losses":          row["losses"],
        "draws":           row["draws"],
        "predicted_for":   row["predicted_for"],
        "created_at":      row["created_at"],
        "finished_at":     row["finished_at"],
        "week_id":         row["week_id"],
        "qualified_for":   row["qualified_for"],
        "final_placement": row["final_placement"],
    }


def create_beat(author_id: str, file_id: str) -> str:
    """Проставляет week_id на текущую неделю (running/voting), если она есть.
    Если нет — оставляет NULL, восстановится при следующем ensure_current_week()
    (текущий пилот всегда стартует неделю при запуске бота, так что практически
    неделя есть всегда)."""
    conn = _connect()
    try:
        with conn:
            c = conn.execute("SELECT COUNT(*) AS c FROM beats").fetchone()["c"]
            beat_id = f"beat_{c + 1}"
            week_row = conn.execute(
                "SELECT id FROM weeks WHERE status IN ('running', 'voting') "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            week_id = week_row["id"] if week_row else None
            conn.execute(
                """INSERT INTO beats (id, author_id, file_id, status, created_at, week_id)
                   VALUES (?, ?, ?, 'queued', ?, ?)""",
                (beat_id, author_id, file_id, datetime.now().isoformat(), week_id),
            )
    finally:
        conn.close()
    return beat_id


def mark_beat_qualified(beat_id: str, week_id: str):
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE beats SET qualified_for = ? WHERE id = ?", (week_id, beat_id))
    finally:
        conn.close()


def set_beat_placement(beat_id: str, place: int):
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE beats SET final_placement = ? WHERE id = ?", (place, beat_id))
    finally:
        conn.close()


def list_finished_beats_between(start_iso: str, end_iso: str) -> list:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM beats WHERE status = 'career_finished' "
            "AND finished_at >= ? AND finished_at <= ?",
            (start_iso, end_iso),
        ).fetchall()
    finally:
        conn.close()
    return [_beat_row_to_dict(r) for r in rows]


def get_beat(beat_id: str):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM beats WHERE id = ?", (beat_id,)).fetchone()
    finally:
        conn.close()
    return _beat_row_to_dict(row) if row else None


def update_beat_status(beat_id: str, status: str):
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE beats SET status = ? WHERE id = ?", (status, beat_id))
    finally:
        conn.close()


def update_beat_file(beat_id: str, file_id: str):
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE beats SET file_id = ? WHERE id = ?", (file_id, beat_id))
    finally:
        conn.close()


def record_beat_battle_result(beat_id: str, result: str):
    """result: 'win' | 'loss' | 'draw'. Инкрементит счётчик и battles_played разом."""
    column = {"win": "wins", "loss": "losses", "draw": "draws"}.get(result)
    if not column:
        return
    conn = _connect()
    try:
        with conn:
            conn.execute(
                f"UPDATE beats SET {column} = {column} + 1, battles_played = battles_played + 1 WHERE id = ?",
                (beat_id,),
            )
    finally:
        conn.close()


def add_predicted_for(beat_id: str, delta: int):
    if not delta:
        return
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "UPDATE beats SET predicted_for = predicted_for + ? WHERE id = ?",
                (delta, beat_id),
            )
    finally:
        conn.close()


def finish_beat_career(beat_id: str):
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "UPDATE beats SET status = 'career_finished', finished_at = ? WHERE id = ?",
                (datetime.now().isoformat(), beat_id),
            )
    finally:
        conn.close()


def list_user_beats(user_id: str) -> list:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM beats WHERE author_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_beat_row_to_dict(r) for r in rows]


def find_active_beat_by_user(user_id: str):
    """Бит пользователя в 'queued'/'battling'/'awaiting_decision'/'paused'. У
    пользователя максимум один активный бит одновременно (инвариант
    поддерживается вызывающим кодом в battles.py). 'paused' считается активным
    статусом — пока бит на паузе, новый отправить нельзя (одна карьера
    единовременно; возможность держать паузный + новый бит — задел на S6)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM beats WHERE author_id = ? "
            "AND status IN ('queued', 'battling', 'awaiting_decision', 'paused') "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    return _beat_row_to_dict(row) if row else None


# ─── Недельный цикл (Бит недели) ──────────────
# Точечные операции, НЕ load-all/save-all.

def _week_row_to_dict(row) -> dict:
    return {
        "id":             row["id"],
        "status":         row["status"],
        "started_at":     row["started_at"],
        "closes_at":      row["closes_at"],
        "voting_ends_at": row["voting_ends_at"],
        "participants":   json.loads(row["participants"] or "[]"),
        "votes":          json.loads(row["votes"] or "{}"),
        "predictions":    json.loads(row["predictions"] or "{}"),
        "voters":         json.loads(row["voters"] or "[]"),
        "winner_beat_id": row["winner_beat_id"],
        "finished_at":    row["finished_at"],
    }


def create_week(started_at: str, closes_at: str) -> str:
    conn = _connect()
    try:
        with conn:
            c = conn.execute("SELECT COUNT(*) AS c FROM weeks").fetchone()["c"]
            week_id = f"week_{c + 1}"
            conn.execute(
                "INSERT INTO weeks (id, status, started_at, closes_at) VALUES (?, 'running', ?, ?)",
                (week_id, started_at, closes_at),
            )
    finally:
        conn.close()
    return week_id


def get_current_week():
    """Неделя в статусе running или voting; при нескольких — самая свежая по started_at."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM weeks WHERE status IN ('running', 'voting') "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return _week_row_to_dict(row) if row else None


def update_week(week_id: str, **fields):
    """Точечный апдейт нескольких полей одной транзакцией. Значения для JSON-полей
    (participants/votes/predictions/voters) должны быть уже сериализованы вызывающим кодом."""
    if not fields:
        return
    columns = ", ".join(f"{k} = ?" for k in fields)
    values  = list(fields.values()) + [week_id]
    conn = _connect()
    try:
        with conn:
            conn.execute(f"UPDATE weeks SET {columns} WHERE id = ?", values)
    finally:
        conn.close()


def finish_week_record(week_id: str, winner_beat_id: str):
    """Названа _record, чтобы не путать с finish_week_voting в weeks.py."""
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "UPDATE weeks SET status = 'finished', winner_beat_id = ?, finished_at = ? WHERE id = ?",
                (winner_beat_id, datetime.now().isoformat(), week_id),
            )
    finally:
        conn.close()


def get_finished_weeks(limit: int = 10) -> list:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM weeks WHERE status = 'finished' ORDER BY finished_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [_week_row_to_dict(r) for r in rows]


# ─── Слоты (слотовая модель батлов) ──────────

def _slot_row_to_dict(row) -> dict:
    return {
        "id":               row["id"],
        "status":           row["status"],
        "created_at":       row["created_at"],
        "started_at":       row["started_at"],
        "voting_ends_at":   row["voting_ends_at"],
        "registered_beats": json.loads(row["registered_beats"] or "[]"),
        "battle_ids":       json.loads(row["battle_ids"] or "[]"),
        "finished_at":      row["finished_at"],
    }


def create_slot() -> str:
    """Создаёт слот в статусе 'registration'. Возвращает slot_id."""
    conn = _connect()
    try:
        with conn:
            c = conn.execute("SELECT COUNT(*) AS c FROM slots").fetchone()["c"]
            slot_id = f"slot_{c + 1}"
            conn.execute(
                "INSERT INTO slots (id, status, created_at) VALUES (?, 'registration', ?)",
                (slot_id, datetime.now().isoformat()),
            )
    finally:
        conn.close()
    return slot_id


def get_open_registration_slot():
    """Слот в статусе registration; при нескольких — самый свежий по created_at."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM slots WHERE status = 'registration' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return _slot_row_to_dict(row) if row else None


def get_running_slot():
    """Слот в статусе running; при нескольких — самый свежий по created_at."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM slots WHERE status = 'running' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return _slot_row_to_dict(row) if row else None


def get_slot(slot_id: str):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
    finally:
        conn.close()
    return _slot_row_to_dict(row) if row else None


def update_slot(slot_id: str, **fields):
    """Точечный апдейт нескольких полей одной транзакцией. Значения для JSON-полей
    (registered_beats/battle_ids) должны быть уже сериализованы вызывающим кодом."""
    if not fields:
        return
    columns = ", ".join(f"{k} = ?" for k in fields)
    values  = list(fields.values()) + [slot_id]
    conn = _connect()
    try:
        with conn:
            conn.execute(f"UPDATE slots SET {columns} WHERE id = ?", values)
    finally:
        conn.close()


def register_beat_to_slot(slot_id: str, beat_id: str):
    """Добавляет beat_id в набор слота, если его там ещё нет (идемпотентно)."""
    conn = _connect()
    try:
        with conn:
            row = conn.execute(
                "SELECT registered_beats FROM slots WHERE id = ?", (slot_id,)
            ).fetchone()
            if row is None:
                return
            registered = json.loads(row["registered_beats"] or "[]")
            if beat_id not in registered:
                registered.append(beat_id)
                conn.execute(
                    "UPDATE slots SET registered_beats = ? WHERE id = ?",
                    (json.dumps(registered, ensure_ascii=False), slot_id),
                )
    finally:
        conn.close()


def set_slot_status(slot_id: str, status: str):
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE slots SET status = ? WHERE id = ?", (status, slot_id))
    finally:
        conn.close()


def ensure_registration_slot() -> str:
    """Возвращает id открытого registration-слота, лениво создавая новый,
    если сейчас такого нет (например, идёт running-слот, а набор на
    следующий ещё не начинался)."""
    slot = get_open_registration_slot()
    if slot:
        return slot["id"]
    return create_slot()


def get_registration_beats() -> list:
    """beat_id из набора текущего открытого registration-слота. Пустой список,
    если открытого слота нет."""
    slot = get_open_registration_slot()
    return slot["registered_beats"] if slot else []


def beat_in_registration(beat_id: str) -> bool:
    return beat_id in get_registration_beats()


def user_in_registration(user_id: str):
    """Аналог user_in_queue для набора слота: если у пользователя есть активный
    бит, который сейчас в наборе открытого registration-слота — возвращает
    slot_id, иначе None."""
    slot = get_open_registration_slot()
    if not slot:
        return None
    beat = find_active_beat_by_user(user_id)
    if not beat:
        return None
    if beat["id"] in slot["registered_beats"]:
        return slot["id"]
    return None


def remove_beat_from_registration(beat_id: str):
    """Убирает beat_id из набора текущего registration-слота. Идемпотентно —
    если бита там нет, ничего не делает."""
    slot = get_open_registration_slot()
    if not slot:
        return
    if beat_id not in slot["registered_beats"]:
        return
    registered = [b for b in slot["registered_beats"] if b != beat_id]
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "UPDATE slots SET registered_beats = ? WHERE id = ?",
                (json.dumps(registered, ensure_ascii=False), slot["id"]),
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


# ─── Аналитика пар (интуиция/прогнозы) ───────
# Таблица pair_ratings — append-only, точечные операции без load-all/save-all.

def add_pair_rating(user_id: str, battle_id: str, vote_side: str, pred_side: str,
                    shown_at, voted_at):
    """Вставляет строку оценки пары. INSERT OR IGNORE защищает от дублей при двойном тапе.

    time_on_pair считается только если есть обе метки (иначе NULL — метки живут
    в памяти и теряются при рестарте бота между показом пары и голосом).
    """
    time_on_pair = None
    if shown_at and voted_at:
        try:
            time_on_pair = (
                datetime.fromisoformat(voted_at) - datetime.fromisoformat(shown_at)
            ).total_seconds()
        except (ValueError, TypeError):
            time_on_pair = None

    diverged     = 1 if vote_side != pred_side else 0
    predicted_at = datetime.now().isoformat()

    conn = _connect()
    try:
        with conn:
            conn.execute(
                """INSERT OR IGNORE INTO pair_ratings
                   (user_id, battle_id, vote_side, pred_side, shown_at, voted_at,
                    predicted_at, time_on_pair, diverged)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, battle_id, vote_side, pred_side, shown_at, voted_at,
                 predicted_at, time_on_pair, diverged),
            )
    finally:
        conn.close()


def resolve_pair_ratings(battle_id: str, winning_side: str):
    """При завершении батла проставляет winning_side и prediction_correct.

    winning_side: "1"|"2"|"0" (ничья). При ничьей prediction_correct остаётся NULL.
    """
    conn = _connect()
    try:
        with conn:
            if winning_side in ("1", "2"):
                conn.execute(
                    """UPDATE pair_ratings
                       SET winning_side = ?,
                           prediction_correct = CASE WHEN pred_side = ? THEN 1 ELSE 0 END
                       WHERE battle_id = ?""",
                    (winning_side, winning_side, battle_id),
                )
            else:
                conn.execute(
                    "UPDATE pair_ratings SET winning_side = ? WHERE battle_id = ?",
                    (winning_side, battle_id),
                )
    finally:
        conn.close()


def get_user_intuition_stats(user_id: str, since_iso: str) -> dict:
    """Статистика прогнозов пользователя за период (predicted_at >= since_iso).

    total_resolved — пар с разрешённым исходом (prediction_correct не NULL).
    reward_a — голос == прогноз == победитель (diverged=0 и угадал).
    reward_b — голос != прогноз, но прогноз == победитель (diverged=1 и угадал).
    pairs_rated — все строки за период, включая неразрешённые.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT prediction_correct, diverged FROM pair_ratings "
            "WHERE user_id = ? AND predicted_at >= ?",
            (user_id, since_iso),
        ).fetchall()
    finally:
        conn.close()

    total_resolved = correct = reward_a = reward_b = 0
    pairs_rated = len(rows)
    for r in rows:
        pc = r["prediction_correct"]
        if pc is None:
            continue
        total_resolved += 1
        if pc == 1:
            correct += 1
            if r["diverged"] == 0:
                reward_a += 1
            else:
                reward_b += 1

    return {
        "total_resolved": total_resolved,
        "correct":        correct,
        "reward_a":       reward_a,
        "reward_b":       reward_b,
        "pairs_rated":    pairs_rated,
    }


def get_all_intuition_accuracy(since_iso: str) -> dict:
    """{user_id: (correct, total_resolved)} за период — только пользователи с total_resolved >= 3.

    Нужно для процентиля «лучше, чем N% участников» — люди с одной парой его бы ломали.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, "
            "SUM(CASE WHEN prediction_correct = 1 THEN 1 ELSE 0 END) AS correct, "
            "SUM(CASE WHEN prediction_correct IS NOT NULL THEN 1 ELSE 0 END) AS total_resolved "
            "FROM pair_ratings WHERE predicted_at >= ? GROUP BY user_id",
            (since_iso,),
        ).fetchall()
    finally:
        conn.close()

    result = {}
    for r in rows:
        total = r["total_resolved"] or 0
        if total >= 3:
            result[r["user_id"]] = (r["correct"] or 0, total)
    return result


# ─── Шаблон пользователя ─────────────────────

def default_user(nickname: str) -> dict:
    return {
        "nickname":            nickname,
        "role":                None,
        "rating":              0,
        "wins":                0,
        "final_wins":          0,
        "battles_today":       0,
        "last_battle_date":    None,
        "is_pro":              False,
        "votes_this_round":    [],
        "bio":                 "",
        "messages_sent_today": 0,
        "last_message_date":   None,
        "last_notified_at":    None,
        "ticket_progress":     0,
        "ticket_required":     0,
        "last_weekly_vote":    None,
        "referred_by":         None,
        "referral_rewarded":   False,
        "referral_count":      0,
        "ticket_discount":     0,
    }


# ─── Реферальная система ─────────────────────
# Точечные операции — не гоняем весь users через load-all/save-all ради одного поля.

def set_referred_by(user_id: str, referrer_id: str):
    """Проставляет referred_by только если сейчас NULL — защита от повторной
    привязки (например, если пользователь позже перейдёт по чужой ссылке)."""
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "UPDATE users SET referred_by = ? WHERE id = ? AND referred_by IS NULL",
                (referrer_id, user_id),
            )
    finally:
        conn.close()


def mark_referral_rewarded(user_id: str):
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE users SET referral_rewarded = 1 WHERE id = ?", (user_id,))
    finally:
        conn.close()


def add_referral_reward(referrer_id: str):
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "UPDATE users SET rating = rating + ?, referral_count = referral_count + 1, "
                "ticket_discount = ticket_discount + ? WHERE id = ?",
                (REFERRAL_RATING_BONUS, REFERRAL_TICKET_DISCOUNT, referrer_id),
            )
    finally:
        conn.close()


def pop_ticket_discount(user_id: str) -> int:
    """Если ticket_discount > 0, уменьшает на 1 и возвращает 1 (скидка
    применена), иначе возвращает 0 без изменений."""
    conn = _connect()
    try:
        with conn:
            row = conn.execute(
                "SELECT ticket_discount FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not row or (row["ticket_discount"] or 0) <= 0:
                return 0
            conn.execute(
                "UPDATE users SET ticket_discount = ticket_discount - 1 WHERE id = ?",
                (user_id,),
            )
    finally:
        conn.close()
    return 1


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


def _battle_scores(b: dict) -> tuple:
    """Суммарный балл по структурированным оценкам для каждой стороны битвы.

    Старые батлы (до структурированной оценки) не имеют feedback — для них
    используем votes1/votes2, чтобы не переписывать задним числом уже
    подсчитанную историю побед/финалов.
    """
    feedback = b.get("feedback") or {}
    if not feedback:
        return b.get("votes1", 0), b.get("votes2", 0)

    score1 = score2 = 0
    for entry in feedback.values():
        side1 = entry.get("1")
        if side1:
            score1 += sum(RATING_POINTS.get(v, 0) for v in side1.values())
        side2 = entry.get("2")
        if side2:
            score2 += sum(RATING_POINTS.get(v, 0) for v in side2.values())
    return score1, score2


def _category_mode_summary(b: dict, side: str):
    """Самая частая оценка по каждой категории для битов стороны side.

    Возвращает None, если оценок меньше двух — недостаточно для сводки.
    """
    feedback = b.get("feedback") or {}
    entries  = [entry[side] for entry in feedback.values() if side in entry]
    if len(entries) < 2:
        return None

    summary = {}
    for cat_key, _ in FEEDBACK_CATEGORIES:
        counts = {}
        for e in entries:
            r = e.get(cat_key)
            if r:
                counts[r] = counts.get(r, 0) + 1
        if counts:
            summary[cat_key] = max(counts, key=counts.get)
    return summary


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
    in_reg = user_in_registration(str(user_id)) is not None

    beat_btn = "✏️ Редактировать бит" if in_reg else "🎵 Отправить бит"
    markup   = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        telebot.types.KeyboardButton(beat_btn),
        telebot.types.KeyboardButton("⚔️ Мой батл"),
        telebot.types.KeyboardButton("🗳 Голосовать"),
        telebot.types.KeyboardButton("📊 Мой профиль"),
        telebot.types.KeyboardButton("🏆 Рейтинг"),
        telebot.types.KeyboardButton("🏆 Бит недели"),
        telebot.types.KeyboardButton("🤝 Пригласить друга"),
    )
    return markup
