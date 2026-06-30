import re
import telebot
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

from config import (
    TOKEN, ADMIN_ID,
    ROOMS, ROOM_LABELS,
    COINS_MAX, BEAT_COST_FREE, BEAT_COST_PRO,
    DAILY_LIMIT_FREE, DAILY_LIMIT_PRO,
    _settings,
    get_battle_hours, get_final_hours, get_final_threshold,
)
from storage import (
    load_users, save_users,
    load_battles, save_battles,
    load_finals,
    load_queue, save_queue, _empty_queue,
    default_user, get_badge, get_room_wins,
    get_menu,
    load_settings, save_settings,
    init_db, migrate_from_json,
)
import battles
import finals

# ─── База данных ──────────────────────────────
init_db()
migrate_from_json()

# ─── Создание экземпляров ────────────────────
bot       = telebot.TeleBot(TOKEN)
scheduler = BackgroundScheduler()
scheduler.start()

battles.init(bot, scheduler)
finals.init(bot, scheduler)

battles.register_handlers(bot)
finals.register_handlers(bot)

# ─── Сессии ──────────────────────────────────
msg_pending   = {}   # user_id -> target_user_id
admin_session = {}   # admin_id -> target_uid (флоу начисления монет)

_NICKNAME_RE = re.compile(r'^[\w ]+$', re.UNICODE)


# ─── Онбординг и регистрация ─────────────────

_ONBOARD = [
    (
        "🎵 Добро пожаловать в Beat Battle!\n\n"
        "Это платформа, где битмейкеры соревнуются друг с другом.\n"
        "Загружай биты, побеждай в батлах и поднимайся на вершину рейтинга!"
    ),
    (
        "⚔️ Как работают батлы?\n\n"
        "1. Загружаешь бит — тратишь монеты\n"
        "2. Бот автоматически находит соперника\n"
        "3. Слушатели голосуют анонимно\n"
        "4. Победитель получает +10 к рейтингу и монеты\n\n"
        "Накопи 3 победы → попади в финальный турнир!"
    ),
    (
        "🪙 Монеты и рейтинг\n\n"
        "• Голосуй в батлах → +1 монета за каждый голос, +1 рейтинг если угадал победителя\n"
        "• Побеждай батлы → +10 к рейтингу\n"
        "• Выиграй финал → +100 к рейтингу и бейдж 👑 Легенда\n\n"
        "Готов начать? Создай профиль!"
    ),
]


@bot.message_handler(commands=["start"])
def cmd_start(message):
    users   = load_users()
    user_id = str(message.from_user.id)

    if user_id in users and users[user_id].get("role"):
        bot.send_message(
            message.chat.id,
            f"👋 С возвращением, {users[user_id]['nickname']}!",
            reply_markup=get_menu(user_id),
        )
        return

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Далее ▶️", callback_data="onboard_1"))
    bot.send_message(message.chat.id, _ONBOARD[0], reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("onboard_"))
def handle_onboard(call):
    step = call.data.split("_")[1]
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.answer_callback_query(call.id)

    if step == "1":
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("Далее ▶️", callback_data="onboard_2"))
        bot.send_message(call.message.chat.id, _ONBOARD[1], reply_markup=markup)
    elif step == "2":
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("Зарегистрироваться 🎵", callback_data="onboard_3"))
        bot.send_message(call.message.chat.id, _ONBOARD[2], reply_markup=markup)
    elif step == "3":
        bot.send_message(call.message.chat.id, "👤 Введи свой никнейм:")
        bot.register_next_step_handler(call.message, save_nickname)


def save_nickname(message):
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        bot.register_next_step_handler(message, save_nickname)
        return

    nickname = message.text.strip()

    if not (2 <= len(nickname) <= 20):
        bot.send_message(message.chat.id, "❌ Никнейм должен быть от 2 до 20 символов. Попробуй ещё раз:")
        bot.register_next_step_handler(message, save_nickname)
        return

    if not _NICKNAME_RE.fullmatch(nickname):
        bot.send_message(
            message.chat.id,
            "❌ Никнейм может содержать только буквы, цифры, пробелы и подчёркивания. Попробуй ещё раз:",
        )
        bot.register_next_step_handler(message, save_nickname)
        return

    users   = load_users()
    user_id = str(message.from_user.id)

    if any(uid != user_id and u.get("nickname", "").lower() == nickname.lower() for uid, u in users.items()):
        bot.send_message(message.chat.id, "❌ Этот никнейм уже занят. Попробуй другой:")
        bot.register_next_step_handler(message, save_nickname)
        return

    users[user_id] = default_user(nickname)
    save_users(users)

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        telebot.types.KeyboardButton("🎵 Я битмейкер"),
        telebot.types.KeyboardButton("🎧 Я слушатель"),
    )
    bot.send_message(
        message.chat.id,
        f"✅ Никнейм сохранён: {nickname}\n\nВыбери роль:",
        reply_markup=markup,
    )


@bot.message_handler(func=lambda m: m.text in ["🎵 Я битмейкер", "🎧 Я слушатель"])
def save_role(message):
    user_id = str(message.from_user.id)
    users   = load_users()

    if user_id not in users:
        bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    role = "beatmaker" if "битмейкер" in message.text else "listener"
    users[user_id]["role"] = role
    save_users(users)

    if role == "beatmaker":
        text = (
            "🎵 Отлично! Загружай биты и побеждай в батлах.\n\n"
            f"Отправка бита стоит {BEAT_COST_FREE} монеты.\n"
            "Голосуй в батлах — зарабатывай монеты!"
        )
    else:
        text = "🎧 Отлично! Голосуй в батлах, зарабатывай монеты и пиши победителям. Удачи!"

    bot.send_message(message.chat.id, text, reply_markup=get_menu(user_id))


# ─── Профиль ──────────────────────────────────

def _build_own_profile_text(user_id: str, users: dict, battles_data: dict) -> str:
    u       = users[user_id]
    badge   = get_badge(u.get("wins", 0), u.get("final_wins", 0))
    pro_str = "💎 Pro" if u.get("is_pro") else "Free"
    role    = u.get("role", "")

    room_wins = get_room_wins(user_id, battles_data)
    best_room = max(room_wins, key=lambda r: room_wins[r])
    best_wins = room_wins[best_room]

    today = datetime.now().date().isoformat()
    if u.get("last_battle_date") != today:
        battles_left = DAILY_LIMIT_PRO if u.get("is_pro") else DAILY_LIMIT_FREE
    else:
        limit        = DAILY_LIMIT_PRO if u.get("is_pro") else DAILY_LIMIT_FREE
        battles_left = max(0, limit - u.get("battles_today", 0))

    lines = [
        f"👤 {u['nickname']} ({pro_str})",
        f"{badge}\n",
        f"⭐️ Рейтинг: {u.get('rating', 0)}",
        f"🪙 Монеты: {u.get('coins', 0)}/{COINS_MAX}",
        f"✅ Побед: {u.get('wins', 0)}",
        f"🏆 Финальных побед: {u.get('final_wins', 0)}",
    ]
    if best_wins > 0:
        lines.append(f"🎯 Лучшая комната: {ROOM_LABELS[best_room]} ({best_wins} побед)")
    if role == "beatmaker":
        lines.append(f"⚔️ Батлов сегодня осталось: {battles_left}")
    bio = u.get("bio", "").strip()
    if bio:
        lines.append(f"\n📝 {bio}")

    return "\n".join(lines)


@bot.message_handler(commands=["profile"])
@bot.message_handler(func=lambda m: m.text == "📊 Мой профиль")
def my_profile(message):
    user_id = str(message.from_user.id)
    users   = load_users()

    if user_id not in users:
        bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    battles_data = load_battles()
    markup       = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✏️ Изменить bio", callback_data="bio_edit"))
    bot.send_message(
        message.chat.id,
        _build_own_profile_text(user_id, users, battles_data),
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data == "bio_edit")
def handle_bio_edit(call):
    user_id = str(call.from_user.id)
    users   = load_users()
    if user_id not in users:
        bot.answer_callback_query(call.id, "Сначала зарегистрируйся!")
        return
    bot.answer_callback_query(call.id)
    current = users[user_id].get("bio", "").strip()
    hint    = f"Текущее: \"{current}\"\n\n" if current else ""
    bot.send_message(call.message.chat.id, f"✏️ {hint}Введи новый bio (до 150 символов):")
    bot.register_next_step_handler(call.message, _save_bio)


def _save_bio(message):
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return
    user_id = str(message.from_user.id)
    text    = message.text.strip()
    if len(text) > 150:
        bot.send_message(message.chat.id, f"❌ Слишком длинный текст ({len(text)} символов). Максимум 150.")
        return
    users = load_users()
    if user_id not in users:
        return
    users[user_id]["bio"] = text
    save_users(users)
    bot.send_message(message.chat.id, "✅ Bio обновлён!", reply_markup=get_menu(user_id))


@bot.callback_query_handler(func=lambda call: call.data.startswith("profile_"))
def handle_view_profile(call):
    target_uid = call.data.split("_", 1)[1]
    viewer_uid = str(call.from_user.id)
    users      = load_users()
    battles_data = load_battles()

    u = users.get(target_uid)
    if not u:
        bot.answer_callback_query(call.id, "Пользователь не найден.")
        return

    badge     = get_badge(u.get("wins", 0), u.get("final_wins", 0))
    room_wins = get_room_wins(target_uid, battles_data)
    top_rooms = sorted(
        [(r, w) for r, w in room_wins.items() if w > 0],
        key=lambda x: x[1], reverse=True,
    )[:3]

    lines = [f"👤 {u['nickname']}", badge]
    if u.get("is_pro"):
        lines.append("💎 Pro")
    lines += ["", f"⭐️ Рейтинг: {u.get('rating', 0)}", f"✅ Побед: {u.get('wins', 0)}"]
    if top_rooms:
        lines += ["", "🏆 Победы по жанрам:"]
        for room, w in top_rooms:
            lines.append(f"  {ROOM_LABELS[room]}: {w}")
    bio = u.get("bio", "").strip()
    if bio:
        lines.append(f"\n📝 {bio}")

    markup = None
    viewer = users.get(viewer_uid, {})
    if viewer_uid != target_uid and viewer.get("role") == "listener":
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("✉️ Написать", callback_data=f"write_{target_uid}"))

    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "\n".join(lines), reply_markup=markup)


# ─── Рейтинг ──────────────────────────────────

@bot.message_handler(commands=["rating"])
@bot.message_handler(func=lambda m: m.text == "🏆 Рейтинг")
def show_rating(message):
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    for room, label in ROOM_LABELS.items():
        markup.add(telebot.types.InlineKeyboardButton(label, callback_data=f"rating_room_{room}"))
    markup.add(telebot.types.InlineKeyboardButton("🌍 Общий рейтинг", callback_data="rating_room_global"))
    bot.send_message(message.chat.id, "🏆 Выбери комнату для рейтинга:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("rating_room_"))
def handle_rating_room(call):
    room         = call.data[len("rating_room_"):]
    users        = load_users()
    battles_data = load_battles()
    medals       = ["🥇", "🥈", "🥉"]

    if room == "global":
        entries = [(uid, u, u.get("rating", 0), "очков") for uid, u in users.items()]
        entries.sort(key=lambda x: x[2], reverse=True)
        title = "🏆 Общий рейтинг"
    elif room in ROOMS:
        entries = []
        for uid, u in users.items():
            rw = get_room_wins(uid, battles_data)
            entries.append((uid, u, rw[room], "побед"))
        entries.sort(key=lambda x: x[2], reverse=True)
        entries = [(uid, u, sc, suf) for uid, u, sc, suf in entries if sc > 0]
        title = f"🏆 Топ {ROOM_LABELS[room]}"
    else:
        bot.answer_callback_query(call.id, "Неизвестная комната")
        return

    text = f"{title}\n\n"
    for i, (uid, u, sc, suf) in enumerate(entries[:10]):
        medal       = medals[i] if i < 3 else f"{i + 1}."
        badge_emoji = get_badge(u.get("wins", 0), u.get("final_wins", 0)).split()[0]
        text       += f"{medal} {u['nickname']} {badge_emoji} — {sc} {suf}\n"

    if not entries:
        text += "Пока нет данных для этой комнаты."

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id)
    except Exception:
        bot.send_message(call.message.chat.id, text)
    bot.answer_callback_query(call.id)


# ─── Витрина ──────────────────────────────────

@bot.message_handler(commands=["winners"])
@bot.message_handler(func=lambda m: m.text == "🎯 Звёзды")
def show_winners(message):
    battles_data = load_battles()
    users        = load_users()
    user_id      = str(message.from_user.id)

    finished = [(bid, b) for bid, b in battles_data.items() if b.get("status") == "finished"]
    finished.sort(
        key=lambda x: x[1].get("end_time") or x[1].get("start_time") or "",
        reverse=True,
    )

    winners_cards = []
    for bid, b in finished:
        v1, v2 = b.get("votes1", 0), b.get("votes2", 0)
        if v1 > v2:
            winner_id = b["player1"]
        elif v2 > v1:
            winner_id = b["player2"]
        else:
            continue
        winners_cards.append((bid, b, winner_id))
        if len(winners_cards) == 5:
            break

    if not winners_cards:
        bot.send_message(
            message.chat.id,
            "🏆 Пока нет завершённых батлов с победителем.",
            reply_markup=get_menu(user_id),
        )
        return

    bot.send_message(message.chat.id, "🌟 Последние чемпионы Beat Battle:")

    for bid, b, winner_id in winners_cards:
        wu         = users.get(winner_id, {})
        badge      = get_badge(wu.get("wins", 0), wu.get("final_wins", 0))
        room_label = ROOM_LABELS.get(b.get("room", ""), "").upper()
        bio_line   = f"\n📝 {wu['bio']}" if wu.get("bio", "").strip() else ""

        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("✉️ Написать", callback_data=f"write_{winner_id}"),
            telebot.types.InlineKeyboardButton("👤 Профиль",  callback_data=f"profile_{winner_id}"),
        )
        bot.send_message(
            message.chat.id,
            f"🏆 ЧЕМПИОН {room_label}\n\n"
            f"{badge} {wu.get('nickname', '—')}\n"
            f"{wu.get('rating', 0)} очков · {wu.get('wins', 0)} побед"
            f"{bio_line}",
            reply_markup=markup,
        )


# ─── Переписка ────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data.startswith("write_"))
def handle_write(call):
    target_uid = call.data.split("_", 1)[1]
    sender_uid = str(call.from_user.id)
    users      = load_users()

    if target_uid not in users:
        bot.answer_callback_query(call.id, "Пользователь не найден.")
        return
    if sender_uid == target_uid:
        bot.answer_callback_query(call.id, "Нельзя написать самому себе.")
        return

    target_nick             = users[target_uid]["nickname"]
    msg_pending[sender_uid] = target_uid

    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"✉️ Пишешь {target_nick}.\n\nВведи сообщение:")
    bot.register_next_step_handler(call.message, handle_outgoing_message)


def handle_outgoing_message(message):
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return

    sender_uid = str(message.from_user.id)
    target_uid = msg_pending.pop(sender_uid, None)

    if not target_uid:
        bot.send_message(message.chat.id, "Сессия устарела — попробуй снова.")
        return

    text = message.text.strip()
    if not text:
        bot.send_message(message.chat.id, "Пустое сообщение не отправлено.")
        return

    users       = load_users()
    sender_nick = users.get(sender_uid, {}).get("nickname", "Неизвестный")

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("↩️ Ответить", callback_data=f"reply_{sender_uid}"),
        telebot.types.InlineKeyboardButton("👤 Профиль",  callback_data=f"profile_{sender_uid}"),
    )
    try:
        bot.send_message(
            target_uid,
            f"📬 Новое сообщение!\n\nОт: {sender_nick}\n\"{text}\"",
            reply_markup=markup,
        )
        bot.send_message(message.chat.id, "✅ Сообщение отправлено!", reply_markup=get_menu(sender_uid))
    except Exception:
        bot.send_message(message.chat.id, "⚠️ Не удалось доставить сообщение.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("reply_"))
def handle_reply(call):
    target_uid = call.data.split("_", 1)[1]
    sender_uid = str(call.from_user.id)
    users      = load_users()

    if target_uid not in users:
        bot.answer_callback_query(call.id, "Пользователь не найден.")
        return

    target_nick             = users[target_uid].get("nickname", "Неизвестный")
    msg_pending[sender_uid] = target_uid

    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"↩️ Отвечаешь {target_nick}.\n\nВведи ответ:")
    bot.register_next_step_handler(call.message, handle_outgoing_message)


# ─── Админ-панель ─────────────────────────────

@bot.message_handler(commands=["admin"])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "⛔️ Нет доступа.")
        return

    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("🛑 Остановить раунд",  callback_data="admin_stop_round"),
        telebot.types.InlineKeyboardButton("🏆 Остановить финал",  callback_data="admin_stop_final"),
        telebot.types.InlineKeyboardButton("💰 Начислить монеты",  callback_data="admin_add_coins"),
        telebot.types.InlineKeyboardButton("👤 Тест-пользователи", callback_data="admin_test_users"),
        telebot.types.InlineKeyboardButton("⏱ Время батлов",       callback_data="admin_set_time"),
        telebot.types.InlineKeyboardButton("🏆 Порог финала",       callback_data="admin_set_threshold"),
    )
    bot.send_message(message.chat.id, "👑 Админ-панель\n\nВыбери действие:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
def handle_admin_actions(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔️ Нет доступа.")
        return

    if call.data == "admin_stop_round":
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("✅ Да, остановить", callback_data="admin_confirm_stop"),
            telebot.types.InlineKeyboardButton("❌ Отмена",         callback_data="admin_cancel"),
        )
        bot.edit_message_text(
            "⚠️ Остановить все активные батлы и очистить очередь?\n\nПодтверждаешь?",
            call.message.chat.id, call.message.message_id, reply_markup=markup,
        )

    elif call.data == "admin_confirm_stop":
        bot.edit_message_text("⏳ Останавливаю раунд...", call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, _admin_stop_round())

    elif call.data == "admin_stop_final":
        finals_data = load_finals()
        active      = {fid: f for fid, f in finals_data.items() if f["status"] == "active"}
        if not active:
            bot.answer_callback_query(call.id, "Нет активных финалов.")
            return
        markup = telebot.types.InlineKeyboardMarkup()
        for fid, f in active.items():
            label = ROOM_LABELS.get(f["room"], f["room"])
            markup.add(telebot.types.InlineKeyboardButton(
                f"🏆 Остановить {label}", callback_data=f"admin_confirm_final_{fid}"
            ))
        markup.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel"))
        bot.edit_message_text(
            "Выбери финал для остановки:",
            call.message.chat.id, call.message.message_id, reply_markup=markup,
        )

    elif call.data.startswith("admin_confirm_final_"):
        fid = call.data[len("admin_confirm_final_"):]
        bot.edit_message_text("⏳ Останавливаю финал...", call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, _admin_stop_final(fid))

    elif call.data == "admin_set_time":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"⏱ Настройка времени\n\n"
            f"Текущие значения:\n  Батл: {get_battle_hours()} ч\n  Финал: {get_final_hours()} ч\n\n"
            f"Введи длительность батла в часах (сейчас: {get_battle_hours()}):",
        )
        bot.register_next_step_handler(call.message, _admin_time_step1)

    elif call.data == "admin_add_coins":
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "💰 Начисление монет\n\nВведи Telegram ID пользователя:")
        bot.register_next_step_handler(call.message, _admin_coins_step1)

    elif call.data == "admin_test_users":
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, _create_test_users())

    elif call.data.startswith("admin_del_beat_"):
        rest        = call.data[len("admin_del_beat_"):]
        side        = int(rest.rsplit("_", 1)[1])
        bid         = rest.rsplit("_", 1)[0]
        winner_side = 2 if side == 1 else 1
        bot.edit_message_text(
            f"🗑 Бит {side} батла #{bid} удалён. Победа присвоена биту {winner_side}.",
            call.message.chat.id, call.message.message_id,
        )
        battles._force_finish_battle(bid, winner_side)

    elif call.data.startswith("admin_ok_report_"):
        rest = call.data[len("admin_ok_report_"):]
        bid  = rest.rsplit("_", 1)[0]
        side = rest.rsplit("_", 1)[1]
        bot.edit_message_text(
            f"✅ Жалоба на батл #{bid} бит {side} отклонена.",
            call.message.chat.id, call.message.message_id,
        )

    elif call.data == "admin_set_threshold":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"🏆 Порог финала\n\nТекущее значение: {get_final_threshold()} побед\n\n"
            f"Введи новое значение (целое число ≥ 1):",
        )
        bot.register_next_step_handler(call.message, _admin_threshold_step)

    elif call.data == "admin_cancel":
        bot.edit_message_text("Отменено.", call.message.chat.id, call.message.message_id)


# ─── Шаговые хэндлеры админа ─────────────────

def _admin_coins_step1(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return
    uid   = message.text.strip()
    users = load_users()
    if uid not in users:
        bot.send_message(message.chat.id, "❌ Пользователь не найден.")
        return
    u = users[uid]
    admin_session[str(message.from_user.id)] = uid
    bot.send_message(
        message.chat.id,
        f"👤 {u['nickname']}\n"
        f"Текущий баланс: {u.get('coins', 0)}/{COINS_MAX} монет\n\n"
        f"Сколько монет начислить?",
    )
    bot.register_next_step_handler(message, _admin_coins_step2)


def _admin_coins_step2(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return
    admin_uid  = str(message.from_user.id)
    target_uid = admin_session.pop(admin_uid, None)
    if not target_uid:
        bot.send_message(message.chat.id, "Сессия устарела — начни заново через /admin.")
        return
    try:
        amount = int(message.text.strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи целое число.")
        return
    users = load_users()
    if target_uid not in users:
        bot.send_message(message.chat.id, "❌ Пользователь не найден.")
        return
    u          = users[target_uid]
    u["coins"] = min(u.get("coins", 0) + amount, COINS_MAX)
    save_users(users)
    bot.send_message(message.chat.id, f"✅ Готово. @{u['nickname']} теперь имеет {u['coins']} монет.")


def _admin_time_step1(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return
    try:
        hours = int(message.text.strip())
        if hours <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи целое положительное число.")
        return
    _settings["battle_hours"] = hours
    bot.send_message(
        message.chat.id,
        f"✅ Батл: {hours} ч\n\n"
        f"Введи длительность финала в часах (сейчас: {get_final_hours()}):",
    )
    bot.register_next_step_handler(message, _admin_time_step2)


def _admin_time_step2(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return
    try:
        hours = int(message.text.strip())
        if hours <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи целое положительное число.")
        return
    _settings["final_hours"] = hours
    save_settings(_settings)
    bot.send_message(
        message.chat.id,
        f"✅ Обновлено. Батл: {get_battle_hours()} ч · Финал: {get_final_hours()} ч\n\n"
        f"Новое время применяется к следующим батлам.",
    )


def _admin_threshold_step(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        bot.send_message(message.chat.id, "Отправь текстовое сообщение.")
        return
    try:
        val = int(message.text.strip())
        if val < 1:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введи целое число ≥ 1.")
        return
    _settings["final_threshold"] = val
    save_settings(_settings)
    bot.send_message(message.chat.id, f"✅ Порог финала обновлён: {val} побед.")


# ─── Функции остановки раунда/финала ─────────

def _admin_stop_round() -> str:
    battles_data   = load_battles()
    users          = load_users()
    queue          = load_queue()
    active         = {bid: b for bid, b in battles_data.items() if b["status"] == "active"}
    stopped        = 0
    rooms_to_check = set()

    for bid, b in active.items():
        votes1, votes2 = b.get("votes1", 0), b.get("votes2", 0)
        p1, p2         = b["player1"], b["player2"]
        p1_nick        = users.get(p1, {}).get("nickname", "Игрок 1")
        p2_nick        = users.get(p2, {}).get("nickname", "Игрок 2")
        room           = b.get("room", "")

        b["status"]   = "finished"
        b["end_time"] = datetime.now().isoformat()

        if votes1 > votes2:
            winner_id, winning_side = p1, 1
        elif votes2 > votes1:
            winner_id, winning_side = p2, 2
        else:
            winner_id, winning_side = None, 0

        if winner_id and winner_id in users:
            users[winner_id]["rating"] = users[winner_id].get("rating", 0) + 10
            users[winner_id]["wins"]   = users[winner_id].get("wins", 0) + 1
            b["counted_for_final"]     = True
            if room:
                rooms_to_check.add(room)

        for uid_str, voted_side in b.get("voters", {}).items():
            if uid_str not in users:
                continue
            u = users[uid_str]
            u["coins"] = min(u.get("coins", 0) + 1, COINS_MAX)
            if winning_side and voted_side == winning_side:
                u["rating"] = u.get("rating", 0) + 1

        try:
            scheduler.remove_job(bid)
        except Exception:
            pass

        for pid, nick, votes, opp_nick, opp_votes in [
            (p1, p1_nick, votes1, p2_nick, votes2),
            (p2, p2_nick, votes2, p1_nick, votes1),
        ]:
            try:
                if winner_id is None:
                    result_text = "🤝 Ничья!"
                elif pid == winner_id:
                    result_text = "✅ Ты победил! +10 очков"
                else:
                    result_text = "❌ Ты проиграл."
                bot.send_message(
                    pid,
                    f"🛑 Батл #{bid} остановлен администратором.\n\n"
                    f"{nick} — {votes} голосов\n{opp_nick} — {opp_votes} голосов\n\n{result_text}",
                )
            except Exception:
                pass

        stopped += 1

    for room in ROOMS:
        for uid in list(queue[room].keys()):
            if uid in users:
                cost = BEAT_COST_PRO if users[uid].get("is_pro") else BEAT_COST_FREE
                users[uid]["coins"] = min(users[uid].get("coins", 0) + cost, COINS_MAX)
            try:
                bot.send_message(uid, "🛑 Раунд остановлен. Твой бит удалён, монеты возвращены.")
            except Exception:
                pass

    for uid in users:
        users[uid]["votes_this_round"] = []

    # Очищаем сессионные данные в battles
    battles.vote_session.clear()
    battles.vote_context.clear()

    save_battles(battles_data)
    save_users(users)
    save_queue(_empty_queue())

    for room in rooms_to_check:
        finals.check_and_start_final(room)

    return f"✅ Раунд остановлен!\n\nБатлов завершено: {stopped}\nОчередь очищена\nГолосования сброшены"


def _admin_stop_final(fid: str) -> str:
    from storage import load_finals, save_finals
    finals_data = load_finals()
    users       = load_users()

    f = finals_data.get(fid)
    if not f or f["status"] != "active":
        return "Финал не найден или уже завершён."

    votes      = f.get("votes", {})
    room_label = ROOM_LABELS.get(f["room"], f["room"])

    f["status"]   = "finished"
    f["end_time"] = datetime.now().isoformat()

    winner_nick = None
    if votes and any(v > 0 for v in votes.values()):
        winner_id    = max(votes, key=lambda uid: votes[uid])
        f["winner_id"] = winner_id

        if winner_id in users:
            users[winner_id]["final_wins"] = users[winner_id].get("final_wins", 0) + 1
            users[winner_id]["rating"]     = users[winner_id].get("rating", 0) + 100

        for beat in f["beats"]:
            uid = beat["user_id"]
            if uid in users and uid != winner_id:
                users[uid]["rating"] = users[uid].get("rating", 0) + 25

        winner_nick = users.get(winner_id, {}).get("nickname", "Неизвестный")

    try:
        scheduler.remove_job(f"final_{fid}")
    except Exception:
        pass

    save_finals(finals_data)
    save_users(users)

    msg = (
        f"🛑 Финал {room_label} остановлен администратором.\n\n"
        + (f"👑 Победитель: {winner_nick} (+100 очков · бейдж 👑 Легенда)" if winner_nick
           else "Голосов не было — победитель не определён.")
    )
    for uid in load_users():
        try:
            bot.send_message(uid, msg)
        except Exception:
            pass

    return f"✅ Финал {room_label} остановлен."


def _create_test_users() -> str:
    users   = load_users()
    created = []
    for i in range(1, 6):
        uid = f"test_{i}"
        if uid not in users:
            u          = default_user(f"TestBeat{i}")
            u["role"]  = "beatmaker"
            u["coins"] = 5
            users[uid] = u
            created.append(f"TestBeat{i}")
    save_users(users)
    if created:
        return "✅ Тест-пользователи созданы:\n" + "\n".join(f"• {n}" for n in created)
    return "ℹ️ Все тест-пользователи уже существуют."


# ─── Восстановление таймеров ──────────────────

def restore_timers():
    battles_data = load_battles()
    finals_data  = load_finals()
    now          = datetime.now()

    for bid, b in battles_data.items():
        if b["status"] == "active" and "start_time" in b:
            start    = datetime.fromisoformat(b["start_time"])
            end_time = start + timedelta(hours=get_battle_hours())
            if end_time > now:
                scheduler.add_job(
                    battles.finish_battle,
                    "date",
                    run_date=end_time,
                    args=[bid],
                    id=bid,
                    replace_existing=True,
                )
            else:
                battles.finish_battle(bid)

    for fid, f in finals_data.items():
        if f["status"] == "active" and "start_time" in f:
            start    = datetime.fromisoformat(f["start_time"])
            end_time = start + timedelta(hours=get_final_hours())
            if end_time > now:
                scheduler.add_job(
                    finals.finish_final,
                    "date",
                    run_date=end_time,
                    args=[fid],
                    id=f"final_{fid}",
                    replace_existing=True,
                )
            else:
                finals.finish_final(fid)


# ─── Запуск ───────────────────────────────────
_settings.update(load_settings())
restore_timers()
print("Бот запущен!")
bot.infinity_polling()
