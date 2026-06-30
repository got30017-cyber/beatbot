import os
import sys

# ─── Пути к файлам ───────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
DB_FILE  = os.path.join(DATA_DIR, "beat_battle.db")

# ─── Telegram credentials ────────────────────
TOKEN    = os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")

if not TOKEN:
    print("❌ BOT_TOKEN не задан! Добавь переменную окружения.")
    sys.exit(1)
if not ADMIN_ID:
    print("❌ ADMIN_ID не задан! Добавь переменную окружения.")
    sys.exit(1)

ADMIN_ID = int(ADMIN_ID)

# ─── Комнаты ─────────────────────────────────
# Одна комната на старте — при 10-50 пользователях деление на жанры дробит
# аудиторию настолько, что батлы не набираются. Список оставлен расширяемым:
# чтобы вернуть жанры, просто добавь элементы сюда.
ROOMS = ["general"]
ROOM_LABELS = {
    "general": "🎵 Биты",
}

# ─── Игровые константы ───────────────────────
BEAT_COST_FREE   = 3
BEAT_COST_PRO    = 2
DAILY_LIMIT_FREE = 1
DAILY_LIMIT_PRO  = 3
COINS_MAX        = 10
FINAL_THRESHOLD  = 2    # дефолт: побед для запуска финала
BATTLE_HOURS     = 1    # дефолт: длительность батла (ч)
FINAL_HOURS      = 2    # дефолт: длительность финала (ч)

# ─── Сообщения и уведомления ─────────────────
MESSAGE_DAILY_LIMIT      = 5     # макс. исходящих личных сообщений в день на пользователя
MESSAGE_COOLDOWN_MINUTES = 10    # мин. интервал между сообщениями одному и тому же адресату
NOTIFY_THROTTLE_HOURS    = 2     # не чаще одного уведомления о новом батле за этот период

# ─── Рантайм-настройки (загружаются при старте) ──
_settings: dict = {}


def get_battle_hours() -> int:
    return int(_settings.get("battle_hours", BATTLE_HOURS))


def get_final_hours() -> int:
    return int(_settings.get("final_hours", FINAL_HOURS))


def get_final_threshold() -> int:
    return int(_settings.get("final_threshold", FINAL_THRESHOLD))
