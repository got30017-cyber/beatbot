import json
import random
import telebot
from datetime import datetime, timedelta

from config import (
    WEEK_CLOSE_WEEKDAY, WEEK_CLOSE_HOUR, WEEK_VOTING_HOURS,
    WEEK_MIN_QUALIFIED, WEEK_MAX_PARTICIPANTS,
)
from storage import (
    create_week, get_current_week, update_week, finish_week_record,
    load_users, save_users, load_battles, load_settings, save_settings,
    get_menu,
    get_beat, list_finished_beats_between, mark_beat_qualified, set_beat_placement,
)

_bot: telebot.TeleBot = None
_scheduler = None

_user_week_order: dict = {}            # user_id -> [beat_id, ...] порядок прослушивания
_pending_week_prediction: dict = {}    # user_id -> {"week_id", "vote_beat_id", "chat_id"}


def init(bot: telebot.TeleBot, scheduler):
    global _bot, _scheduler
    _bot       = bot
    _scheduler = scheduler


def _pluralize_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "дня"
    return "дней"


# ─── Планирование и создание недели ──────────

def _next_week_close_datetime(now: datetime) -> datetime:
    """Ближайшая суббота 12:00 UTC после now (или сегодня, если сегодня суббота
    и час ещё не наступил)."""
    target      = now.replace(hour=WEEK_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    days_ahead  = (WEEK_CLOSE_WEEKDAY - now.weekday()) % 7
    if days_ahead == 0 and now >= target:
        days_ahead = 7
    return target + timedelta(days=days_ahead)


def ensure_current_week() -> dict:
    """Гарантирует, что есть активная неделя. Возвращает её dict."""
    week = get_current_week()
    if week:
        return week

    now        = datetime.now()
    closes_at  = _next_week_close_datetime(now)
    week_id    = create_week(now.isoformat(), closes_at.isoformat())

    _scheduler.add_job(
        close_week, "date",
        run_date=closes_at,
        args=[week_id],
        id=f"week_close_{week_id}",
        replace_existing=True,
    )
    return get_current_week()


# ─── Закрытие недели (квалификация) ───────────

def _beat_totals(beat_id: str, battles_data: dict) -> tuple:
    """(sum_votes_for, sum_predictions_for) — по всем батлам карьеры бита."""
    sum_votes = 0
    sum_preds = 0
    for b in battles_data.values():
        beat1_id, beat2_id = b.get("beat1_id"), b.get("beat2_id")
        if beat1_id == beat_id:
            sum_votes += b.get("votes1", 0)
        if beat2_id == beat_id:
            sum_votes += b.get("votes2", 0)
        for side in b.get("predictions", {}).values():
            if (side == "1" and beat1_id == beat_id) or (side == "2" and beat2_id == beat_id):
                sum_preds += 1
    return sum_votes, sum_preds


def close_week(week_id: str):
    week = get_current_week()
    if not week or week["id"] != week_id or week["status"] != "running":
        return

    started_at = week["started_at"]
    closes_at  = week["closes_at"]

    finished_beats = list_finished_beats_between(started_at, closes_at)
    battles_data    = load_battles()

    ranked = []
    for beat in finished_beats:
        sum_votes, sum_preds = _beat_totals(beat["id"], battles_data)
        ranked.append((beat, sum_votes, sum_preds))
    ranked.sort(key=lambda t: (-t[0]["wins"], -t[1], -t[2]))

    if len(ranked) < WEEK_MIN_QUALIFIED:
        new_closes = datetime.fromisoformat(closes_at) + timedelta(days=7)
        update_week(week_id, closes_at=new_closes.isoformat())
        _scheduler.add_job(
            close_week, "date",
            run_date=new_closes,
            args=[week_id],
            id=f"week_close_{week_id}",
            replace_existing=True,
        )
        for uid in load_users():
            try:
                _bot.send_message(
                    uid,
                    f"⏳ На этой неделе пока мало завершённых карьер для полноценного отбора.\n\n"
                    f"Бит недели пройдёт в следующую субботу — у тебя есть ещё немного времени, "
                    f"чтобы твой бит попал в число лучших. Загружай и играй!",
                )
            except Exception:
                pass
        return

    top          = ranked[:WEEK_MAX_PARTICIPANTS]
    participants = []
    for beat, _, _ in top:
        mark_beat_qualified(beat["id"], week_id)
        participants.append({
            "beat_id":   beat["id"],
            "author_id": beat["author_id"],
            "file_id":   beat["file_id"],
        })

    now            = datetime.now()
    voting_ends_at = now + timedelta(hours=WEEK_VOTING_HOURS)

    update_week(
        week_id,
        status="voting",
        participants=json.dumps(participants, ensure_ascii=False),
        voting_ends_at=voting_ends_at.isoformat(),
        votes=json.dumps({p["beat_id"]: 0 for p in participants}, ensure_ascii=False),
    )

    _scheduler.add_job(
        finish_week_voting, "date",
        run_date=voting_ends_at,
        args=[week_id],
        id=f"week_voting_{week_id}",
        replace_existing=True,
    )

    for uid in load_users():
        try:
            _bot.send_message(
                uid,
                f"🎧 Бит недели начался!\n\n"
                f"{len(participants)} лучших битов этой недели ждут твоего вердикта. "
                f"У тебя {WEEK_VOTING_HOURS} часов, чтобы послушать всех и выбрать фаворита.\n\n"
                f"Заходи → /week",
            )
        except Exception:
            pass


# ─── Флоу голосования пользователя ────────────

def _cmd_week(message):
    user_id = str(message.from_user.id)
    users   = load_users()
    if user_id not in users:
        _bot.send_message(message.chat.id, "Сначала зарегистрируйся — нажми /start")
        return

    week = get_current_week()
    if not week:
        _bot.send_message(
            message.chat.id,
            "Сейчас нет активного цикла — но это временно, новый скоро запустится. Попробуй чуть позже.",
            reply_markup=get_menu(user_id),
        )
        return

    if week["status"] == "running":
        closes_at  = datetime.fromisoformat(week["closes_at"])
        remaining  = closes_at - datetime.now()
        days_left  = remaining.days
        hours_left = remaining.seconds // 3600
        if days_left > 0:
            time_str = f"{days_left} {_pluralize_days(days_left)}"
            if hours_left > 0:
                time_str += f" {hours_left} ч"
        else:
            time_str = f"{hours_left} ч"
        _bot.send_message(
            message.chat.id,
            f"🎯 Сейчас идёт набор карьер этой недели.\n\n"
            f"До закрытия недели и старта голосования Бита недели: {time_str}.\n\n"
            f"Загружай биты и играй карьеры, чтобы попасть в число лучших!",
            reply_markup=get_menu(user_id),
        )
        return

    if week["status"] != "voting":
        _bot.send_message(
            message.chat.id,
            "Сейчас нет активного голосования Бита недели. Загляни чуть позже.",
            reply_markup=get_menu(user_id),
        )
        return

    if user_id in week["voters"]:
        _bot.send_message(
            message.chat.id,
            "Ты уже проголосовал! Результаты — в конце голосования 🤫",
            reply_markup=get_menu(user_id),
        )
        return

    participants = week["participants"]
    shuffled     = list(participants)
    random.shuffle(shuffled)
    _user_week_order[user_id] = [p["beat_id"] for p in shuffled]

    _bot.send_message(
        message.chat.id,
        f"🎧 Послушай {len(shuffled)} финалистов недели и выбери своего фаворита.",
    )

    for i, p in enumerate(shuffled, start=1):
        caption = f"🎵 Бит {i}"
        file_id = p.get("file_id")
        if file_id:
            try:
                _bot.send_audio(message.chat.id, file_id, caption=caption)
            except Exception:
                _bot.send_message(message.chat.id, caption)
        else:
            _bot.send_message(message.chat.id, caption)

    buttons = [
        telebot.types.InlineKeyboardButton(f"Бит {i}", callback_data=f"week_vote_{i - 1}")
        for i in range(1, len(shuffled) + 1)
    ]
    markup = telebot.types.InlineKeyboardMarkup(row_width=3)
    markup.add(*buttons[:3])
    if len(buttons) > 3:
        markup.add(*buttons[3:])
    _bot.send_message(message.chat.id, "❤️ Какой бит — твой фаворит?", reply_markup=markup)


def _handle_week_vote(call):
    user_id  = str(call.from_user.id)
    position = int(call.data[len("week_vote_"):])

    order = _user_week_order.get(user_id)
    if not order or position >= len(order):
        _bot.answer_callback_query(call.id, "Сессия устарела — открой /week заново.")
        return
    vote_beat_id = order[position]

    week = get_current_week()
    if not week or week["status"] != "voting":
        _bot.answer_callback_query(call.id, "Голосование уже завершилось.")
        return
    if user_id in week["voters"]:
        _bot.answer_callback_query(call.id, "Ты уже проголосовал.")
        return

    beat = get_beat(vote_beat_id)
    if beat and beat["author_id"] == user_id:
        _bot.answer_callback_query(call.id, "Нельзя голосовать за свой бит.")
        return

    _pending_week_prediction[user_id] = {
        "week_id":       week["id"],
        "vote_beat_id":  vote_beat_id,
        "chat_id":       call.message.chat.id,
    }

    try:
        _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    _bot.answer_callback_query(call.id, "✅ Выбор записан!")

    pred_positions = list(range(len(order)))
    random.shuffle(pred_positions)   # микротрение против автопилота
    buttons = [
        telebot.types.InlineKeyboardButton(f"Бит {p + 1}", callback_data=f"week_pred_{p}")
        for p in pred_positions
    ]
    markup = telebot.types.InlineKeyboardMarkup(row_width=3)
    markup.add(*buttons[:3])
    if len(buttons) > 3:
        markup.add(*buttons[3:])
    _bot.send_message(
        call.message.chat.id,
        "🧠 А какой бит, по-твоему, выберет большинство?",
        reply_markup=markup,
    )


def _handle_week_prediction(call):
    user_id  = str(call.from_user.id)
    position = int(call.data[len("week_pred_"):])

    pending = _pending_week_prediction.get(user_id)
    if not pending:
        _bot.answer_callback_query(call.id, "Сессия устарела — открой /week заново.")
        return

    order = _user_week_order.get(user_id)
    if not order or position >= len(order):
        _bot.answer_callback_query(call.id, "Сессия устарела — открой /week заново.")
        return
    pred_beat_id = order[position]

    week = get_current_week()
    if not week or week["id"] != pending["week_id"] or week["status"] != "voting":
        _bot.answer_callback_query(call.id, "Голосование уже завершилось.")
        return
    if user_id in week["voters"]:
        _bot.answer_callback_query(call.id, "Ты уже проголосовал.")
        return

    vote_beat_id = pending["vote_beat_id"]
    votes        = week["votes"]
    votes[vote_beat_id] = votes.get(vote_beat_id, 0) + 1
    predictions  = week["predictions"]
    predictions[user_id] = pred_beat_id
    voters       = week["voters"]
    voters.append(user_id)

    update_week(
        week["id"],
        votes=json.dumps(votes, ensure_ascii=False),
        predictions=json.dumps(predictions, ensure_ascii=False),
        voters=json.dumps(voters, ensure_ascii=False),
    )

    users = load_users()
    if user_id in users:
        users[user_id]["last_weekly_vote"] = week["id"]
        save_users(users)

    try:
        _bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    _bot.answer_callback_query(call.id)

    vote_pos = order.index(vote_beat_id) + 1
    pred_pos = position + 1

    _pending_week_prediction.pop(user_id, None)
    _user_week_order.pop(user_id, None)

    voting_ends_at = datetime.fromisoformat(week["voting_ends_at"])
    hours_left     = max(0, int((voting_ends_at - datetime.now()).total_seconds() // 3600))

    _bot.send_message(
        call.message.chat.id,
        f"❤️ Твой выбор: Бит {vote_pos}\n"
        f"🧠 Твой прогноз: Бит {pred_pos}\n\n"
        f"Итоги — примерно через {hours_left} ч. 🤫",
        reply_markup=get_menu(user_id),
    )


# ─── Завершение Бита недели ───────────────────

def finish_week_voting(week_id: str):
    week = get_current_week()
    if not week or week["id"] != week_id or week["status"] != "voting":
        return

    participants = week["participants"]
    if not participants:
        return
    votes       = week["votes"]
    predictions = week["predictions"]

    if not votes or all(v == 0 for v in votes.values()):
        winner_beat_id = participants[0]["beat_id"]
    else:
        def _pred_count_for(beat_id):
            return sum(1 for b in predictions.values() if b == beat_id)
        winner_beat_id = max(votes, key=lambda bid: (votes.get(bid, 0), _pred_count_for(bid)))

    ranked_ids = sorted(
        [p["beat_id"] for p in participants],
        key=lambda bid: votes.get(bid, 0),
        reverse=True,
    )
    if winner_beat_id in ranked_ids:
        ranked_ids.remove(winner_beat_id)
    ranked_ids.insert(0, winner_beat_id)

    users       = load_users()
    medals      = ["🥇", "🥈", "🥉"]
    top_lines   = []
    winner_nick = "—"

    for i, beat_id in enumerate(ranked_ids, start=1):
        set_beat_placement(beat_id, i)
        beat = get_beat(beat_id)
        if not beat:
            continue
        author_id = beat["author_id"]
        nick      = users.get(author_id, {}).get("nickname", "—")
        v         = votes.get(beat_id, 0)

        if i == 1:
            winner_nick = nick
            if author_id in users:
                users[author_id]["final_wins"] = users[author_id].get("final_wins", 0) + 1
                users[author_id]["rating"]     = users[author_id].get("rating", 0) + 100
        else:
            if author_id in users:
                users[author_id]["rating"] = users[author_id].get("rating", 0) + 25

        medal = medals[i - 1] if i <= 3 else f"{i}."
        top_lines.append(f"{medal} {nick} — {v}")

    save_users(users)
    finish_week_record(week_id, winner_beat_id)

    msg = (
        f"🏆 Бит недели — {winner_nick}!\n\n"
        f"Сообщество выбрало. Вот как распределились голоса:\n\n"
        + "\n".join(top_lines)
        + f"\n\nПоздравляем {winner_nick} с титулом 👑 Легенда недели!"
    )
    for uid in load_users():
        try:
            _bot.send_message(uid, msg)
        except Exception:
            pass

    # Персональная сводка интуиции — переехала сюда из finish_final (промпт 2).
    from battles import build_intuition_summary   # ленивый импорт — избегаем циклической зависимости

    settings  = load_settings()
    since_iso = settings.get("summary_since", week["started_at"])
    for uid in load_users():
        try:
            summary = build_intuition_summary(uid, since_iso)
        except Exception:
            continue
        if not summary:
            continue
        try:
            _bot.send_message(uid, summary)
        except Exception:
            pass
    settings["summary_since"] = week["voting_ends_at"]
    save_settings(settings)

    ensure_current_week()


# ─── Церемония открытия недели (guard) ────────

def weekly_gate_check(user_id: str):
    """(passed, hint_text). Если Бит недели активен и пользователь ещё не
    проголосовал — passed=False + текст-приглашение в /week."""
    week = get_current_week()
    if not week or week["status"] != "voting":
        return True, None

    users = load_users()
    if users.get(user_id, {}).get("last_weekly_vote") == week["id"]:
        return True, None

    hint = (
        "🎧 Сначала — Бит недели!\n\n"
        "На этой неделе идёт голосование за лучший бит. Прежде чем участвовать в "
        "новых батлах, послушай финалистов и выбери своего.\n\n"
        "Нажми /week"
    )
    return False, hint


# ─── Регистрация хэндлеров ───────────────────

def register_handlers(bot: telebot.TeleBot):
    bot.message_handler(commands=["week"])(_cmd_week)
    bot.message_handler(func=lambda m: m.text == "🏆 Бит недели")(_cmd_week)
    bot.callback_query_handler(func=lambda c: c.data.startswith("week_vote_"))(_handle_week_vote)
    bot.callback_query_handler(func=lambda c: c.data.startswith("week_pred_"))(_handle_week_prediction)
