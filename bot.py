from __future__ import annotations

import logging
from datetime import date, datetime, time

from dotenv import load_dotenv

load_dotenv(override=True)

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import config
import formatters as fmt
import keyboards as kb
from schedule import ScheduleHub
from store import Store
import watcher

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("spt.bot")

hub = ScheduleHub()
store = Store()

KIND_BY_CODE = {"g": "group", "t": "teacher", "r": "room"}


def is_private(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == ChatType.PRIVATE)


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    if chat.type == ChatType.PRIVATE:
        return True
    if user.id in config.ADMIN_IDS:
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        return False
    return member.status in {
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    }


def chat_title(update: Update) -> str:
    chat = update.effective_chat
    if not chat:
        return ""
    if chat.title:
        return chat.title
    user = update.effective_user
    return (user.full_name if user else "") or ""


def ensure(update: Update):
    chat = update.effective_chat
    assert chat is not None
    return store.ensure_chat(chat.id, chat.type, chat_title(update))


def svc_for(chat):
    return hub.get(chat.corpus or None)


async def _send(
    update: Update,
    text: str,
    *,
    markup=None,
    edit: bool = False,
) -> None:
    parts = fmt.split_message(text)
    chat = update.effective_chat
    query = update.callback_query
    for i, part in enumerate(parts):
        extra = markup if i == len(parts) - 1 else None
        if edit and query and i == 0:
            try:
                await query.edit_message_text(
                    part,
                    parse_mode=ParseMode.HTML,
                    reply_markup=extra,
                    disable_web_page_preview=True,
                )
                continue
            except Exception:
                pass
        if query and i == 0 and not edit:
            await query.message.reply_text(
                part,
                parse_mode=ParseMode.HTML,
                reply_markup=extra,
                disable_web_page_preview=True,
            )
        elif update.message:
            await update.message.reply_text(
                part,
                parse_mode=ParseMode.HTML,
                reply_markup=extra,
                disable_web_page_preview=True,
            )
        elif chat:
            await chat.send_message(
                part,
                parse_mode=ParseMode.HTML,
                reply_markup=extra,
                disable_web_page_preview=True,
            )


async def _can_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await is_admin(update, context):
        return True
    await _send(
        update,
        "В группе менять корпус, группу и настройки могут только администраторы чата.",
    )
    return False


async def _can_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Whether the user may interact with the bot in this chat."""
    if is_private(update):
        return True
    if await is_admin(update, context):
        return True
    chat = ensure(update)
    if chat.flag("allow_members"):
        return True
    await _send(
        update,
        "В этой группе бот доступен только администраторам.\n"
        "Админ может включить доступ участникам в настройках.",
    )
    return False


def base_date(chat) -> date:
    svc = svc_for(chat)
    return svc.page_date or datetime.now(config.TZ).date()


def parse_day_token(token: str, chat) -> date:
    if token in {"today", "0", ""}:
        return base_date(chat)
    return date(int(token[0:4]), int(token[4:6]), int(token[6:8]))


async def open_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = ensure(update)
    if not chat.corpus:
        await _send(
            update,
            "🏛 Выберите корпус техникума:",
            markup=kb.corpus_keyboard(),
            edit=bool(update.callback_query),
        )
        return
    await _send(
        update,
        fmt.menu_text(chat),
        markup=kb.menu_keyboard(chat),
        edit=bool(update.callback_query),
    )


async def set_corpus(update: Update, context: ContextTypes.DEFAULT_TYPE, corpus_id: str) -> None:
    if not await _can_pick(update, context):
        return
    chat = ensure(update)
    store.set_corpus(chat.chat_id, corpus_id)
    chat = store.get_chat(chat.chat_id) or chat
    title = config.corpus_meta(corpus_id)["title"]
    await _send(
        update,
        f"Выбран <b>{title}</b>.\nТеперь выберите группу:",
        markup=kb.pick_kind_keyboard(),
        edit=True,
    )


async def _need_entity(update: Update):
    chat = ensure(update)
    if not chat.corpus:
        await _send(update, "Сначала выберите корпус:", markup=kb.corpus_keyboard())
        return None
    svc = svc_for(chat)
    if chat.entity_id and svc.get_entity(chat.entity_id):
        return svc.get_entity(chat.entity_id)
    if chat.entity_id:
        found = svc.search(chat.entity_name, chat.entity_kind)
        if found:
            store.set_entity(chat.chat_id, found[0].id, found[0].name, found[0].kind)
            return found[0]
    await _send(
        update,
        "Сначала выберите, чьё расписание показывать.",
        markup=kb.pick_kind_keyboard(),
    )
    return None


async def show_day(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target: date,
    *,
    edit: bool = False,
) -> None:
    chat = ensure(update)
    if not chat.corpus:
        await open_menu(update, context)
        return
    ent = await _need_entity(update)
    if not ent:
        return
    svc = svc_for(chat)
    try:
        day = await svc.day_for(ent, target)
        prev = await svc.neighbor_day(ent, target, -1)
        nxt = await svc.neighbor_day(ent, target, 1)
    except Exception:
        log.exception("day fetch failed")
        await _send(update, "Не удалось загрузить расписание.")
        return
    chat = store.get_chat(chat.chat_id) or chat
    await _send(
        update,
        fmt.format_day(day, chat, updated_label=svc.updated_label),
        markup=kb.schedule_nav(target, chat, prev_day=prev, next_day=nxt),
        edit=edit,
    )


def _remember_entity(chat_id: int, ent, corpus: str) -> None:
    store.set_entity(chat_id, ent.id, ent.name, ent.kind)
    if ent.kind == "group":
        store.add_favorite(chat_id, ent.id, ent.name, ent.kind, corpus=corpus)


async def ensure_schedule_loaded(update: Update, chat) -> bool:
    svc = svc_for(chat)
    if svc.by_kind.get("group"):
        return True
    await _send(update, "Качаю список с сайта, подождите…")
    try:
        await svc.refresh_today()
        if svc.updated_label:
            store.set_meta(f"updated:{svc.corpus_id}", svc.updated_label)
        store.set_meta("bootstrapped", "1")
    except Exception as exc:
        log.exception("on-demand schedule load failed")
        await _send(
            update,
            "Не удалось скачать расписание с сайта.\n"
            f"Ошибка: <code>{type(exc).__name__}</code>",
        )
        return False
    if not svc.by_kind.get("group"):
        await _send(update, "Сайт ответил, но групп не нашёл.")
        return False
    return True


async def show_letters(update: Update, kind: str) -> None:
    chat = ensure(update)
    if not chat.corpus:
        await _send(update, "🏛 Выберите корпус:", markup=kb.corpus_keyboard())
        return
    if not await ensure_schedule_loaded(update, chat):
        return
    svc = svc_for(chat)
    letters = svc.letters(kind)
    if not letters:
        await _send(update, "Список пуст. Попробуйте ещё раз через минуту.")
        return
    title = {
        "group": "Выберите букву / цифру группы",
        "teacher": "Выберите букву фамилии преподавателя",
        "room": "Выберите аудиторию по первой цифре или букве",
    }[kind]
    await _send(update, title, markup=kb.letters_keyboard(kind, letters))


async def show_entity_page(update: Update, kind: str, letter: str, page: int) -> None:
    chat = ensure(update)
    svc = svc_for(chat)
    items = svc.by_letter(kind, letter) if letter else svc.by_kind.get(kind, [])
    if not items:
        await _send(update, "Никого не нашёл.")
        return
    await _send(
        update,
        f"{kb.KIND_BTN.get(kind, kind)} · {letter or 'все'} ({len(items)})",
        markup=kb.entities_keyboard(kind, items, page, letter),
    )


async def select_entity(
    update: Update, context: ContextTypes.DEFAULT_TYPE, entity_id: str
) -> None:
    chat = ensure(update)
    svc = svc_for(chat)
    ent = svc.get_entity(entity_id)
    if not ent:
        await _send(update, "Не нашёл такую позицию, обновите список.")
        return
    _remember_entity(chat.chat_id, ent, chat.corpus)
    await show_day(update, context, base_date(chat), edit=bool(update.callback_query))


async def open_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = ensure(update)
    if not chat.corpus:
        await open_menu(update, context)
        return
    chat = store.get_chat(chat.chat_id) or chat
    await _send(
        update,
        fmt.format_favorites(chat),
        markup=kb.favorites_keyboard(chat),
        edit=bool(update.callback_query),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure(update)
    if not await _can_use(update, context):
        return
    if update.message:
        await update.message.reply_text(
            fmt.welcome_text(not is_private(update)),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.remove_reply_keyboard(),
        )
    await open_menu(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure(update)
    if not await _can_use(update, context):
        return
    if update.message:
        await update.message.reply_text(
            fmt.help_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.remove_reply_keyboard(),
        )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure(update)
    if not await _can_use(update, context):
        return
    await open_menu(update, context)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id not in config.ADMIN_IDS:
        if update.message:
            await update.message.reply_text("Команда только для администратора бота.")
        return
    text = fmt.format_stats(store.all_chats())
    await _send(update, text)


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id not in config.ADMIN_IDS:
        return
    try:
        await hub.refresh_all()
        parts = [
            f"{cid}: {len(svc.entities)} ({svc.updated_label or '—'})"
            for cid, svc in hub.services.items()
        ]
        await _send(update, "Обновлено.\n" + "\n".join(parts))
    except Exception as exc:
        await _send(update, f"Ошибка обновления: {exc}")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    if not await _can_use(update, context):
        return
    data = query.data
    chat = ensure(update)

    if data.startswith("c:"):
        await set_corpus(update, context, data[2:])
        return

    if data.startswith("t:"):
        if not await _can_pick(update, context):
            return
        chat = store.toggle_setting(chat.chat_id, data[2:])
        await _send(
            update,
            fmt.format_settings(chat),
            markup=kb.settings_keyboard(chat),
            edit=True,
        )
        return

    if data.startswith("d:"):
        try:
            target = parse_day_token(data[2:], chat)
        except ValueError:
            target = base_date(chat)
        await show_day(update, context, target, edit=True)
        return

    if data.startswith("f:"):
        if not await _can_pick(update, context):
            return
        eid = data[2:]
        svc = svc_for(chat)
        ent = svc.get_entity(eid)
        if not ent:
            fav = next(
                (
                    f
                    for f in chat.favorites_for_corpus(chat.corpus)
                    if f["id"] == eid
                ),
                None,
            )
            if fav:
                store.set_entity(chat.chat_id, fav["id"], fav["name"], fav["kind"])
                await show_day(update, context, base_date(chat), edit=True)
                return
            await _send(update, "Избранная позиция устарела.")
            return
        store.set_entity(chat.chat_id, ent.id, ent.name, ent.kind)
        await show_day(update, context, base_date(chat), edit=True)
        return

    if data == "m:home":
        await open_menu(update, context)
        return
    if data == "m:corpus":
        if not await _can_pick(update, context):
            return
        await _send(
            update,
            "🏛 Выберите корпус:",
            markup=kb.corpus_keyboard(),
            edit=True,
        )
        return
    if data == "m:favs":
        if not await _can_pick(update, context):
            return
        await open_favorites(update, context)
        return
    if data == "m:favadd":
        if not await _can_pick(update, context):
            return
        if chat.entity_id and chat.entity_kind == "group":
            store.add_favorite(
                chat.chat_id,
                chat.entity_id,
                chat.entity_name,
                chat.entity_kind,
                corpus=chat.corpus,
            )
        await open_favorites(update, context)
        return
    if data == "m:fav":
        if not await _can_pick(update, context):
            return
        if not chat.entity_id:
            await _send(update, "Сначала выберите группу.")
            return
        store.toggle_favorite(
            chat.chat_id,
            chat.entity_id,
            chat.entity_name,
            chat.entity_kind,
            corpus=chat.corpus,
        )
        await open_favorites(update, context)
        return
    if data.startswith("xf:"):
        if not await _can_pick(update, context):
            return
        store.remove_favorite(chat.chat_id, data[3:], corpus=chat.corpus)
        await open_favorites(update, context)
        return
    if data == "m:pick":
        if not await _can_pick(update, context):
            return
        if not chat.corpus:
            await open_menu(update, context)
            return
        await _send(update, "Кого показывать?", markup=kb.pick_kind_keyboard(), edit=True)
        return
    if data == "m:set":
        if not await _can_pick(update, context):
            return
        chat = store.get_chat(chat.chat_id) or chat
        await _send(
            update,
            fmt.format_settings(chat),
            markup=kb.settings_keyboard(chat),
            edit=True,
        )
        return

    if data.startswith("k:"):
        if not await _can_pick(update, context):
            return
        await show_letters(update, data.split(":", 1)[1])
        return

    if data.startswith("q:"):
        if not await _can_pick(update, context):
            return
        kind = KIND_BY_CODE.get(data.split(":")[1], "group")
        context.user_data["await_search"] = kind
        hint = {
            "group": "Напишите группу, например <code>АСУ-25</code>",
            "teacher": "Напишите фамилию, например <code>Чеботова</code>",
            "room": "Напишите аудиторию, например <code>423</code>",
        }[kind]
        await _send(update, hint)
        return

    if data.startswith("l:"):
        if not await _can_pick(update, context):
            return
        _, code, letter = data.split(":", 2)
        await show_entity_page(update, KIND_BY_CODE[code], letter, 0)
        return

    if data.startswith("p:"):
        if not await _can_pick(update, context):
            return
        _, code, letter, page_s = data.split(":", 3)
        await show_entity_page(update, KIND_BY_CODE[code], letter, int(page_s))
        return

    if data.startswith("s:"):
        if not await _can_pick(update, context):
            return
        await select_entity(update, context, data[2:])
        return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not await _can_use(update, context):
        return
    text = update.message.text.strip()
    chat = ensure(update)

    # Ignore leftover reply-keyboard labels; guide to /menu
    if text in {
        "📅 Расписание",
        "Расписание",
        "📋 Меню",
        "Меню",
        "⚙️ Настройки",
        "Настройки",
    }:
        await open_menu(update, context)
        return

    if not is_private(update) and not context.user_data.get("await_search"):
        return
    if not chat.corpus:
        await open_menu(update, context)
        return

    kind = context.user_data.pop("await_search", None)
    svc = svc_for(chat)
    found = svc.search(text, kind)
    if not found:
        if kind or is_private(update):
            await _send(update, "Ничего не нашёл. Попробуйте иначе или /menu")
        return
    if not await _can_pick(update, context):
        return
    if len(found) == 1:
        _remember_entity(chat.chat_id, found[0], chat.corpus)
        await show_day(update, context, base_date(chat))
        return
    rows = []
    for ent in found[:12]:
        label = f"{ent.name} ({fmt.KIND_RU.get(ent.kind, ent.kind)})"
        rows.append([InlineKeyboardButton(label, callback_data=f"s:{ent.id}")])
    await update.message.reply_text(
        "Нашёл несколько вариантов:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.my_chat_member:
        return
    chat = update.effective_chat
    member = update.my_chat_member.new_chat_member
    if not chat or not member:
        return
    if member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}:
        store.ensure_chat(chat.id, chat.type, chat.title or "")
        await context.bot.send_message(
            chat.id,
            fmt.welcome_text(True),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.remove_reply_keyboard(),
        )
        await context.bot.send_message(
            chat.id,
            "🏛 Администратор, выберите корпус:",
            reply_markup=kb.corpus_keyboard(),
        )


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await watcher.poll_changes(hub, store, context.bot)


async def morning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await watcher.send_morning(hub, store, context.bot)


async def on_startup(app: Application) -> None:
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Запуск"),
                BotCommand("menu", "Меню"),
                BotCommand("help", "Справка"),
                BotCommand("stats", "Статистика (админ)"),
            ]
        )
    except Exception:
        log.exception("set_my_commands failed; continuing")

    async def _boot(context: ContextTypes.DEFAULT_TYPE) -> None:
        if all(s.by_kind.get("group") for s in hub.services.values()):
            return
        ok = await watcher.bootstrap(hub, store)
        if ok:
            for job in context.job_queue.get_jobs_by_name("bootstrap_retry"):
                job.schedule_removal()

    app.job_queue.run_once(_boot, when=1, name="bootstrap_once")
    app.job_queue.run_repeating(_boot, interval=90, first=60, name="bootstrap_retry")


async def on_shutdown(app: Application) -> None:
    await hub.close()


def build_app() -> Application:
    if not config.BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    request = HTTPXRequest(
        connect_timeout=40,
        read_timeout=40,
        write_timeout=40,
        pool_timeout=40,
    )
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .request(request)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    if app.job_queue is None:
        raise SystemExit("Install python-telegram-bot[job-queue]")
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(
        poll_job, interval=config.POLL_SECONDS, first=config.POLL_SECONDS
    )
    app.job_queue.run_daily(
        morning_job,
        time=time(hour=config.MORNING_HOUR, minute=config.MORNING_MINUTE, tzinfo=config.TZ),
    )
    return app


def main() -> None:
    import asyncio

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    app = build_app()
    log.info("starting SPT schedule bot (1+2 corpus)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
