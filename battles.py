import random
import telebot
from datetime import datetime, timedelta

from config import (
    ADMIN_ID, ROOMS, ROOM_LABELS,
    COINS_MAX, BEAT_COST_FREE, BEAT_COST_PRO,
    _settings, get_battle_hours,
)
from storage import (
    load_users, save_users,
    load_battles, save_battles,
    load_queue, save_queue,
    _empty_queue,
    check_daily_limit, user_in_queue, get_menu,
)
from finals import check_and_start_final

_bot: telebot.TeleBot = None
_scheduler = None

VOTE_UNLOCK_DELAY  = 20   # сек. — кнопки голосования появляются не раньше
VOTE_GRACE_PERIOD  = 5    # сек. — голос засчитывается не раньше, чем через это время после разблокировки

# Сессии голосования (видны из bot.py для сброса в admin_stop_round)
vote_context: dict = {}        # user_id -> "beat" | "free" | "room_<uid>"
vote_session: dict = {}        # user_id -> {"required": N, "battles": [...]}
vote_unlocked_at: dict = {}    # user_id -> iso_timestamp, когда юзеру реально пришли кнопки голосования


def init(bot: telebot.TeleBot, scheduler):
    global _bot, _scheduler
    _bot       = bot
    _scheduler = scheduler


# ─── Вспомогательные функции голосования ─────

def get_eligible_battles(user_id: str) -> dict:
    battles = load_battles()
    return {
        bid: b for bid, b in battles.items()
        if b["status"] == "active"
        and user_id not in [b["player1"], b["player2"]]
    }


def votes_needed(user_id: str):
    eligible = get_eligible_battles(user_id)
    users    = load_users()
    voted    = users.get(user_id, {}).get("votes_this_round", [])

    if user_id in vote_session:
        session_battles = vote_session[user_id]["battles"]
        required        = vote_session[user_id]["required"]
        not_voted     = [bid for bid in session_battles if bid in eligible and bid not in voted]
        already_voted = [bid for bid in session_battles if bid in eligible and bid in voted]
        return required, len(already_voted), not_voted

    required  = min(len(eligible), 5)
    not_voted = [bid for bid in eligible if bid not in voted]
    already   = [bid for bid in eligible if bid in voted]
    return required, len(already), not_voted


def start_vote_session(user_id: str):
    eligible  = get_eligible_battles(user_id)
    users     = load_users()
    voted     = users.get(user_id, {}).get("votes_this_round", [])
    not_voted = [bid for bid in eligible if bid not in voted]
    required  = min(len(eligible), 5)
    vote_session[user_id] = {"required": required, "battles": not_voted[:required]}
    return required, 0, not_voted[:required]


def clear_vote_session(user_id: str):
    vote_session.pop(user_id, None)


def find_user_battle(user_id: str):
    battles  = load_battles()
    for bid, b in battles.items():
        if b["status"] == "active" and user_id in [b["player1"], b["player2"]]:
            return bid, b
    finished = [
        (bid, b) for bid, b in battles.items()
        if b["status"] == "finished" and user_id in [b["player1"], b["player2"]]
    ]
    return finished[-1] if finished else (None, None)


# ─── Завершение батла ─────────────────────────

def finish_battle(bid: str):
    battles = load_battles()
    users   = load_users()

    b = battles.get(bid)
    if not b or b["status"] != "active":
        return

    b["status"]   = "finished"
    b["end_time"] = datetime.now().isoformat()
    room          = b.get("room", "")

    votes1  = b.get("votes1", 0)
    votes2  = b.get("votes2", 0)
    p1, p2  = b["player1"], b["player2"]
    p1_nick = users.get(p1, {}).get("nickname", "Игрок 1")
    p2_nick = users.get(p2, {}).get("nickname", "Игрок 2")

    if votes1 > votes2:
        winning_side                 = 1
        winner_id, loser_id          = p1, p2
        winner_nick, loser_nick      = p1_nick, p2_nick
        winner_votes, loser_votes    = votes1, votes2
    elif votes2 > votes1:
        winning_side                 = 2
        winner_id, loser_id          = p2, p1
        winner_nick, loser_nick      = p2_nick, p1_nick
        winner_votes, loser_votes    = votes2, votes1
    else:
        winning_side = 0

    for uid_str, voted_side in b.get("voters", {}).items():
        if uid_str not in users:
            continue
        u = users[uid_str]
        u["coins"] = min(u.get("coins", 0) + 1, COINS_MAX)
        if winning_side != 0 and voted_side == winning_side:
            u["rating"] = u.get("rating", 0) + 1

    if winning_side == 0:
        total = votes1 + votes2
        save_battles(battles)
        save_users(users)
        for pid in [p1, p2]:
            if pid in users:
                try:
                    _bot.send_message(
                        pid,
                        f"⚔️ Батл #{bid} завершён!\n\n"
                        f"🤝 Ничья!\n"
                        f"🎵 {p1_nick} — {votes1} гол.\n"
                        f"🎵 {p2_nick} — {votes2} гол.\n"
                        f"📊 Всего голосов: {total}",
                    )
                except Exception:
                    pass
        return

    if winner_id in users:
        users[winner_id]["rating"] = users[winner_id].get("rating", 0) + 10
        users[winner_id]["wins"]   = users[winner_id].get("wins", 0) + 1

    b["counted_for_final"] = True

    save_battles(battles)
    save_users(users)

    total      = winner_votes + loser_votes
    winner_pct = round(winner_votes / total * 100) if total > 0 else 0
    loser_pct  = 100 - winner_pct
    new_rating = users.get(winner_id, {}).get("rating", 0)

    try:
        _bot.send_message(
            winner_id,
            f"🏆 Батл #{bid} завершён!\n\n"
            f"✅ Ты победил!\n"
            f"🎵 {winner_nick} — {winner_votes} гол. ({winner_pct}%)\n"
            f"🎵 {loser_nick} — {loser_votes} гол. ({loser_pct}%)\n\n"
            f"📊 Всего голосов: {total}\n"
            f"⭐️ Твой рейтинг: {new_rating} (+10)",
        )
    except Exception:
        pass
    try:
        _bot.send_message(
            loser_id,
            f"⚔️ Батл #{bid} завершён!\n\n"
            f"❌ Ты проиграл.\n"
            f"🎵 {winner_nick} — {winner_votes} гол. ({winner_pct}%)\n"
            f"🎵 {loser_nick} — {loser_votes} гол. ({loser_pct}%)\n\n"
            f"📊 Всего голосов: {total}",
        )
    except Exception:
        pass

    if room:
        check_and_start_final(room)


def _force_finish_battle(bid: str, winning_side: int):
    battles = load_battles()
    users   = load_users()

    b = battles.get(bid)
    if not b or b["status"] != "active":
        return

    b["status"]   = "finished"
    b["end_time"] = datetime.now().isoformat()

    p1, p2  = b["player1"], b["player2"]
    p1_nick = users.get(p1, {}).get("nickname", "Игрок 1")
    p2_nick = users.get(p2, {}).get("nickname", "Игрок 2")

    if winning_side == 1:
        winner_id, loser_id     = p1, p2
    else:
        winner_id, loser_id     = p2, p1

    if winner_id in users:
        users[winner_id]["rating"] = users[winner_id].get("rating", 0) + 10
        users[winner_id]["wins"]   = users[winner_id].get("wins", 0) + 1
    b["counted_for_final"] = True

    try:
        _scheduler.remove_job(bid)
    except Exception:
        pass

    save_battles(battles)
    save_users(users)

    new_rating = users.get(winner_id, {}).get("rating", 0)
    for pid, result in [
        (winner_id, f"✅ Ты победил! (бит соперника удалён)\n⭐️ Твой рейтинг: {new_rating} (+10)"),
        (loser_id,  "❌ Твой бит был удалён администратором. Батл завершён."),
    ]:
        try:
            _bot.send_message(pid, f"⚔️ Батл #{bid} завершён досрочно.\n\n{result}")
        except Exception:
            pass

    room = b.get("room", "")
    if room:
        check_and_start_final(room)


# ─── Показ батла для голосования ─────────────

def send_battle_for_vote(chat_id, bid, battles):
    b          = battles[bid]
    room_label = ROOM_LABELS.get(b.get("room", ""), "")

    order = [1, 2]
    random.shuffle(order)   # порядок прослушивания рандомный для каждого голосующего

    for position, side in enumerate(order):
        file_id = b.get(f"beat{side}_file_id")
        markup  = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(
            f"⚠️ Пожаловаться на бит {side}", callback_data=f"report_{bid}_{side}",
        ))

        if position == 0:
            caption = f"🎵 Бит {side}"
        else:
            caption = (
                f"🎵 Бит {side}\n\n⚔️ Батл #{bid} {room_label}\n"
                f"Послушай оба бита — голосование откроется через {VOTE_UNLOCK_DELAY} секунд 👇"
            )

        if file_id:
            try:
                _bot.send_audio(chat_id, file_id, caption=caption, reply_markup=markup)
            except Exception:
                _bot.send_message(chat_id, caption, reply_markup=markup)
        else:
            _bot.send_message(chat_id, caption, reply_markup=markup)

    _scheduler.add_job(
        _unlock_vote_buttons,
        "date",
        run_date=datetime.now() + timedelta(seconds=VOTE_UNLOCK_DELAY),
        args=[chat_id, bid],
        id=f"unlock_{bid}_{chat_id}",
        replace_existing=True,
    )


def _unlock_vote_buttons(chat_id, bid):
    battles = load_battles()
    b       = battles.get(bid)
    if not b or b["status"] != "active":
        return

    user_id = str(chat_id)
    if user_id in b.get("voters", {}):
        return

    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("🔥 Бит 1", callback_data=f"vote_{bid}_1"),
        telebot.types.InlineKeyboardButton("🔥 Бит 2", callback_data=f"vote_{bid}_2"),
    )
    try:
        _bot.send_message(chat_id, "✅ Теперь можешь голосовать 👇", reply_markup=markup)
    except Exception:
        return

    vote_unlocked_at[user_id] = datetime.now().isoformat()


def send_next_battle_for_vote(chat_id, user_id, not_voted):
    if not not_voted:
        _bot.send_message(
            chat_id,
            "✅ Ты проголосовал во всех доступных батлах!",
            reply_markup=get_menu(user_id),
        )
        return
    battles = load_battles()
    send_battle_for_vote(chat_id, not_voted[0], battles)


# ─── Хэндлеры батлов ─────────────────────────

def _handle_report(call):
    parts   = call.data.split("_")
    side    = parts[-1]
    bid     = "_".join(parts[1:-1])
    user_id = str(call.from_user.id)
    users   = load_users()
    battles = load_battles()

    b = battles.get(bid)
    if not b or b["status"] != "active":
        _bot.answer_callback_query(call.id, "Этот батл уже завершён.")
        return
    if user_id in [b["player1"], b["player2"]]:
        _bot.answer_callback_query(call.id, "Нельзя жаловаться на свой батл.")
        return

    nick   = users.get(user_id, {}).get("nickname", "Неизвестный")
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("🗑 Удалить бит",  callback_data=f"admin_del_beat_{bid}_{side}"),
        telebot.types.InlineKeyboardButton("✅ Ок",           callback_data=f"admin_ok_report_{bid}_{side}"),
    )
    try:
        _bot.send_message(
            ADMIN_ID,
            f"⚠️ Жалоба на батл #{bid}, бит {side}.\nОт: @{nick}",
            reply_markup=markup,
        )
    except Exception:
        pass
    _bot.answer_callback_query(call.id, "⚠️ Жалоба отправлена администратору.")


def _send_beat(message):
    user_id = str(message.from_user.id)
    users   = load_users()

    if user_id not in users:
        _bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    u = users[user_id]

    exhausted, _ = check_daily_limit(u)
    save_users(users)
    if exhausted:
        from datetime import timedelta
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d.%m в 00:00")
        _bot.send_message(
            message.chat.id,
            f"⛔️ Дневной лимит исчерпан. Обновится {tomorrow}.\n\n"
            f"{'Pro-подписка даёт 3 батла в день.' if not u.get('is_pro') else ''}",
            reply_markup=get_menu(user_id),
        )
        return

    cost  = BEAT_COST_PRO if u.get("is_pro") else BEAT_COST_FREE
    coins = u.get("coins", 0)
    if coins < cost:
        need = cost - coins
        _bot.send_message(
            message.chat.id,
            f"🪙 Не хватает монет.\nНужно: {cost}, у тебя: {coins} (ещё нужно: {need})\n\n"
            f"Голосуй в батлах — каждый голос даёт +1 монету.\nНажми 🗳 Голосовать",
            reply_markup=get_menu(user_id),
        )
        return

    queue = load_queue()
    if user_in_queue(user_id, queue):
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("❌ Отменить бит", callback_data="cancel_beat"),
            telebot.types.InlineKeyboardButton("🔙 Назад",        callback_data="cancel_ignore"),
        )
        _bot.send_message(message.chat.id, "⏳ Твой бит уже в очереди.\n\nХочешь отменить?", reply_markup=markup)
        return

    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    for room, label in ROOM_LABELS.items():
        markup.add(telebot.types.InlineKeyboardButton(label, callback_data=f"room_{room}"))
    _bot.send_message(
        message.chat.id,
        f"🎵 Выбери жанровую комнату:\n\n(Стоимость: {cost} монеты, у тебя: {coins})",
        reply_markup=markup,
    )


def _handle_room_select(call):
    room    = call.data.split("_", 1)[1]
    user_id = str(call.from_user.id)

    if room not in ROOMS:
        _bot.answer_callback_query(call.id, "Неизвестная комната")
        return

    _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    vote_context[f"room_{user_id}"] = room

    _bot.send_message(call.message.chat.id, f"✅ Комната: {ROOM_LABELS[room]}\n\nОтправь аудиофайл с битом:")
    _bot.answer_callback_query(call.id)


def _handle_cancel_beat(call):
    user_id = str(call.from_user.id)
    _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

    if call.data == "cancel_ignore":
        _bot.answer_callback_query(call.id)
        _bot.send_message(call.message.chat.id, "Хорошо, бит остаётся в очереди.", reply_markup=get_menu(user_id))
        return

    queue = load_queue()
    room  = user_in_queue(user_id, queue)
    if room:
        del queue[room][user_id]
        save_queue(queue)
        users = load_users()
        if user_id in users:
            cost = BEAT_COST_PRO if users[user_id].get("is_pro") else BEAT_COST_FREE
            users[user_id]["coins"] = min(users[user_id].get("coins", 0) + cost, COINS_MAX)
            save_users(users)
        _bot.answer_callback_query(call.id, "Бит отменён, монеты возвращены!")
        _bot.send_message(call.message.chat.id, "✅ Бит удалён. Монеты возвращены.", reply_markup=get_menu(user_id))
    else:
        _bot.answer_callback_query(call.id, "Бит уже не в очереди.")
        _bot.send_message(call.message.chat.id, "Бит уже не в очереди.", reply_markup=get_menu(user_id))


def _receive_beat(message):
    user_id = str(message.from_user.id)
    users   = load_users()

    if user_id not in users:
        _bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    room_key = f"room_{user_id}"
    room     = vote_context.get(room_key)
    if not room:
        _bot.send_message(
            message.chat.id,
            "Сначала выбери жанровую комнату — нажми 🎵 Отправить бит",
            reply_markup=get_menu(user_id),
        )
        return

    u = users[user_id]
    exhausted, _ = check_daily_limit(u)
    cost  = BEAT_COST_PRO if u.get("is_pro") else BEAT_COST_FREE
    coins = u.get("coins", 0)
    if exhausted or coins < cost:
        vote_context.pop(room_key, None)
        _bot.send_message(message.chat.id, "⛔️ Условия изменились — проверь лимит и монеты.", reply_markup=get_menu(user_id))
        return

    u["coins"] = coins - cost
    vote_context.pop(room_key, None)

    if message.audio:
        file_id = message.audio.file_id
    elif message.voice:
        file_id = message.voice.file_id
    else:
        file_id = message.document.file_id

    queue = load_queue()
    if user_in_queue(user_id, queue):
        _bot.send_message(message.chat.id, "⏳ Твой бит уже в очереди!", reply_markup=get_menu(user_id))
        return

    waiting = [uid for uid in queue[room] if uid != user_id]

    if waiting:
        opponent_id   = waiting[0]
        opponent_beat = queue[room][opponent_id]
        del queue[room][opponent_id]
        save_queue(queue)

        battles    = load_battles()
        bid        = f"battle_{len(battles) + 1}"
        start_time = datetime.now()

        battles[bid] = {
            "player1":       opponent_id,
            "player2":       user_id,
            "beat1_file_id": opponent_beat,
            "beat2_file_id": file_id,
            "votes1":        0,
            "votes2":        0,
            "voters":        {},
            "status":        "active",
            "room":          room,
            "start_time":    start_time.isoformat(),
        }
        save_battles(battles)

        _scheduler.add_job(
            finish_battle,
            "date",
            run_date=start_time + timedelta(hours=get_battle_hours()),
            args=[bid],
            id=bid,
            replace_existing=True,
        )

        today = datetime.now().date().isoformat()
        for pid in [opponent_id, user_id]:
            if pid in users:
                if users[pid].get("last_battle_date") != today:
                    users[pid]["battles_today"]    = 0
                    users[pid]["last_battle_date"] = today
                users[pid]["battles_today"]    = users[pid].get("battles_today", 0) + 1
                users[pid]["votes_this_round"] = []

        save_users(users)

        p1_nick    = users.get(opponent_id, {}).get("nickname", "Соперник")
        p2_nick    = users.get(user_id, {}).get("nickname", "Ты")
        room_label = ROOM_LABELS[room]

        try:
            _bot.send_message(
                opponent_id,
                f"⚔️ Батл начался! {room_label}\nСоперник: {p2_nick}\nБатл ID: {bid}\n\n"
                f"Голосование открыто {get_battle_hours()} часов.",
            )
        except Exception:
            pass
        _bot.send_message(
            message.chat.id,
            f"⚔️ Батл начался! {room_label}\nСоперник: {p1_nick}\nБатл ID: {bid}\n\n"
            f"Голосование открыто {get_battle_hours()} часов.",
            reply_markup=get_menu(user_id),
        )
    else:
        queue[room][user_id] = file_id
        save_queue(queue)
        u["votes_this_round"] = []
        save_users(users)
        _bot.send_message(
            message.chat.id,
            f"✅ Бит принят! {ROOM_LABELS[room]}\nЖдём соперника... ⏳\nМонеты списаны: -{cost} 🪙",
            reply_markup=get_menu(user_id),
        )


def _vote_menu(message):
    user_id = str(message.from_user.id)
    users   = load_users()

    if user_id not in users:
        _bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    vote_context[user_id] = "free"
    _, _, not_voted = start_vote_session(user_id)

    if not not_voted:
        _bot.send_message(
            message.chat.id,
            "✅ Ты уже проголосовал во всех активных батлах!\n\n"
            "Как только появятся новые — снова можешь голосовать и зарабатывать монеты.",
            reply_markup=get_menu(user_id),
        )
        return

    _bot.send_message(
        message.chat.id,
        f"🗳 Батлов для голосования: {len(not_voted)}\n\n"
        f"Слушай оба бита внимательно — голосовать можно через {VOTE_UNLOCK_DELAY} секунд после получения битов.\n"
        f"+1 монета за голос, угадал победителя — ещё и +1 рейтинг.\nИмена скрыты до твоего голоса. 👇",
    )
    send_next_battle_for_vote(message.chat.id, user_id, not_voted)


def _handle_vote(call):
    parts     = call.data.split("_")
    voted_for = int(parts[-1])
    bid       = "_".join(parts[1:-1])
    user_id   = str(call.from_user.id)

    users   = load_users()
    battles = load_battles()

    if user_id not in users:
        _bot.answer_callback_query(call.id, "Сначала зарегистрируйся!")
        return
    if bid in users[user_id].get("votes_this_round", []):
        _bot.answer_callback_query(call.id, "Ты уже голосовал в этом батле!")
        return

    b = battles.get(bid)
    if not b or b["status"] != "active":
        _bot.answer_callback_query(call.id, "Этот батл уже завершён.")
        return
    if user_id in [b["player1"], b["player2"]]:
        _bot.answer_callback_query(call.id, "Нельзя голосовать в своём батле!")
        return

    unlocked_at_str = vote_unlocked_at.get(user_id)
    if unlocked_at_str:
        unlocked_at = datetime.fromisoformat(unlocked_at_str)
        if (datetime.now() - unlocked_at).total_seconds() < VOTE_GRACE_PERIOD:
            _bot.answer_callback_query(call.id, "Подожди немного — дай биту доиграть 🎧")
            return
    # если unlocked_at отсутствует (например, бот перезапустился) — не блокируем голос

    if voted_for == 1:
        b["votes1"] = b.get("votes1", 0) + 1
    else:
        b["votes2"] = b.get("votes2", 0) + 1

    if "voters" not in b:
        b["voters"] = {}
    b["voters"][user_id] = voted_for

    save_battles(battles)

    if "votes_this_round" not in users[user_id]:
        users[user_id]["votes_this_round"] = []
    users[user_id]["votes_this_round"].append(bid)
    save_users(users)

    vote_unlocked_at.pop(user_id, None)

    _bot.answer_callback_query(call.id, "✅ Голос засчитан!")
    _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

    p1_nick = users.get(b["player1"], {}).get("nickname", "Игрок 1")
    p2_nick = users.get(b["player2"], {}).get("nickname", "Игрок 2")
    _bot.send_message(
        call.message.chat.id,
        f"👤 Бит 1 — {p1_nick}\n👤 Бит 2 — {p2_nick}\n\n"
        f"+1 монета засчитается после завершения батла. Угадал победителя — будет ещё +1 рейтинг.",
    )

    required, already_voted, not_voted = votes_needed(user_id)
    if not not_voted or already_voted >= required:
        ctx  = vote_context.pop(user_id, "free")
        text = (
            "✅ Отлично! Теперь можешь загрузить бит — нажми 🎵 Отправить бит"
            if ctx == "beat"
            else "✅ Проголосовал во всех доступных батлах!"
        )
        clear_vote_session(user_id)
        _bot.send_message(call.message.chat.id, text, reply_markup=get_menu(user_id))
    else:
        _bot.send_message(call.message.chat.id, f"Осталось батлов: {required - already_voted}")
        send_next_battle_for_vote(call.message.chat.id, user_id, not_voted)


def _my_battle(message):
    user_id = str(message.from_user.id)
    users   = load_users()

    if user_id not in users:
        _bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    queue = load_queue()
    if user_in_queue(user_id, queue):
        _bot.send_message(message.chat.id, "⏳ Твой бит в очереди — ждём соперника.", reply_markup=get_menu(user_id))
        return

    bid, b = find_user_battle(user_id)
    if not b:
        _bot.send_message(
            message.chat.id,
            "У тебя нет активного батла.\nЗагрузи бит — нажми 🎵 Отправить бит",
            reply_markup=get_menu(user_id),
        )
        return

    p1_id, p2_id   = b["player1"], b["player2"]
    p1_nick        = users.get(p1_id, {}).get("nickname", "Игрок 1")
    p2_nick        = users.get(p2_id, {}).get("nickname", "Игрок 2")
    votes1, votes2 = b.get("votes1", 0), b.get("votes2", 0)
    total          = votes1 + votes2

    if user_id == p1_id:
        my_nick, my_v, opp_nick, opp_v = p1_nick, votes1, p2_nick, votes2
    else:
        my_nick, my_v, opp_nick, opp_v = p2_nick, votes2, p1_nick, votes1

    my_pct  = round(my_v / total * 100) if total > 0 else 0
    opp_pct = 100 - my_pct

    status_label = "✅ Завершён" if b["status"] == "finished" else "⏳ Идёт"
    room_label   = ROOM_LABELS.get(b.get("room", ""), "")

    _bot.send_message(
        message.chat.id,
        f"⚔️ Батл #{bid} {room_label} — {status_label}\n\n"
        f"🎵 {my_nick} (ты) — {my_v} голосов ({my_pct}%)\n"
        f"🎵 {opp_nick} — {opp_v} голосов ({opp_pct}%)\n\n"
        f"📊 Всего голосов: {total}",
        reply_markup=get_menu(user_id),
    )


# ─── Регистрация хэндлеров ───────────────────

def register_handlers(bot: telebot.TeleBot):
    bot.message_handler(
        func=lambda m: m.text in ["🎵 Отправить бит", "✏️ Редактировать бит"]
    )(_send_beat)
    bot.message_handler(func=lambda m: m.text == "⚔️ Мой батл")(_my_battle)
    bot.message_handler(func=lambda m: m.text == "🗳 Голосовать")(_vote_menu)
    bot.message_handler(content_types=["audio", "voice", "document"])(_receive_beat)
    bot.callback_query_handler(func=lambda c: c.data.startswith("room_"))(_handle_room_select)
    bot.callback_query_handler(func=lambda c: c.data in ["cancel_beat", "cancel_ignore"])(_handle_cancel_beat)
    bot.callback_query_handler(func=lambda c: c.data.startswith("vote_"))(_handle_vote)
    bot.callback_query_handler(func=lambda c: c.data.startswith("report_"))(_handle_report)
