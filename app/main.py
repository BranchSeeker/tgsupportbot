import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

DB_PATH = "tickets.db"


class TicketStates(StatesGroup):
    waiting_issue = State()
    waiting_app = State()
    waiting_platform = State()
    waiting_details = State()


@dataclass
class Config:
    bot_token: str
    owner_id: int
    admin_ids: set[int]


def load_config() -> Config:
    load_dotenv()

    token = os.getenv("BOT_TOKEN", "").strip()
    owner = os.getenv("OWNER_ID", "").strip()
    admins_raw = os.getenv("ADMIN_IDS", "").strip()

    if not token:
        raise ValueError("BOT_TOKEN is required in .env")
    if not owner:
        raise ValueError("OWNER_ID is required in .env")
    if not admins_raw:
        raise ValueError("ADMIN_IDS is required in .env")

    owner_id = int(owner)
    admin_ids = {int(i.strip()) for i in admins_raw.split(",") if i.strip()}
    admin_ids.add(owner_id)

    return Config(bot_token=token, owner_id=owner_id, admin_ids=admin_ids)


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            issue_text TEXT NOT NULL,
            app_name TEXT,
            platform TEXT,
            details TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            closed_at TEXT,
            closed_by INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def is_problem_not_working(text: str) -> bool:
    words = ("не работает", "not working", "не подключ", "не груз", "doesn't work")
    lowered = text.lower()
    return any(w in lowered for w in words)


def create_ticket(user_id: int, username: str | None, issue_text: str, app_name: str | None, platform: str | None, details: str | None) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO tickets (user_id, username, issue_text, app_name, platform, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, issue_text, app_name, platform, details, datetime.utcnow().isoformat()),
    )
    ticket_id = cur.lastrowid
    conn.commit()
    conn.close()
    return int(ticket_id)


def close_ticket(ticket_id: int, admin_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT status FROM tickets WHERE id = ?",
        (ticket_id,),
    )
    row = cur.fetchone()
    if not row or row[0] == "closed":
        conn.close()
        return False

    cur.execute(
        "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), admin_id, ticket_id),
    )
    conn.commit()
    conn.close()
    return True


def fetch_open_tickets() -> list[tuple]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, user_id, username, issue_text, app_name, platform, created_at FROM tickets WHERE status = 'open' ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return rows


def fetch_user_tickets(user_id: int) -> list[tuple]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, issue_text, status, created_at FROM tickets WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def admin_message(ticket_id: int, user_id: int, username: str | None, issue: str, app_name: str | None, platform: str | None, details: str | None) -> str:
    return (
        f"🆕 Новый тикет #{ticket_id}\n"
        f"👤 User ID: {user_id}\n"
        f"👤 Username: @{username if username else '-'}\n"
        f"📝 Проблема: {issue}\n"
        f"📱 Приложение: {app_name or '-'}\n"
        f"📲 Платформа: {platform or '-'}\n"
        f"ℹ️ Детали: {details or '-'}"
    )


def open_ticket_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🎫 Создать тикет")]],
        resize_keyboard=True,
    )


def make_close_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Закрыть тикет", callback_data=f"close:{ticket_id}"))
    return builder.as_markup()


async def notify_admins(bot: Bot, admin_ids: Iterable[int], text: str, ticket_id: int) -> None:
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=make_close_keyboard(ticket_id))
        except Exception as e:  # noqa: BLE001
            logging.warning("Cannot send notification to admin %s: %s", admin_id, e)


def register_handlers(dp: Dispatcher, config: Config) -> None:
    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "Привет! Я бот поддержки VPN. Нажмите кнопку ниже или используйте /ticket.",
            reply_markup=open_ticket_reply_keyboard(),
        )

    @dp.message(Command("ticket"))
    @dp.message(F.text == "🎫 Создать тикет")
    async def cmd_ticket(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(TicketStates.waiting_issue)
        await message.answer("Опишите проблему одним сообщением.")

    @dp.message(TicketStates.waiting_issue)
    async def issue_received(message: Message, state: FSMContext) -> None:
        issue = (message.text or "").strip()
        if not issue:
            await message.answer("Пожалуйста, отправьте текстовое описание проблемы.")
            return

        await state.update_data(issue=issue)

        if is_problem_not_working(issue):
            await state.set_state(TicketStates.waiting_app)
            await message.answer("Уточните, каким приложением пользуетесь (например: Outline, WireGuard, AmneziaVPN).")
            return

        ticket_id = create_ticket(message.from_user.id, message.from_user.username, issue, None, None, None)
        await state.clear()
        await message.answer(f"✅ Тикет #{ticket_id} создан. Скоро ответим.")

        text = admin_message(ticket_id, message.from_user.id, message.from_user.username, issue, None, None, None)
        await notify_admins(message.bot, config.admin_ids, text, ticket_id)

    @dp.message(TicketStates.waiting_app)
    async def app_received(message: Message, state: FSMContext) -> None:
        app_name = (message.text or "").strip()
        if not app_name:
            await message.answer("Напишите название приложения текстом.")
            return

        await state.update_data(app_name=app_name)
        await state.set_state(TicketStates.waiting_platform)
        await message.answer("На каком устройстве проблема: Android или iPhone?")

    @dp.message(TicketStates.waiting_platform)
    async def platform_received(message: Message, state: FSMContext) -> None:
        platform = (message.text or "").strip()
        if not platform:
            await message.answer("Напишите платформу (Android / iPhone).")
            return

        await state.update_data(platform=platform)
        await state.set_state(TicketStates.waiting_details)
        await message.answer("Есть ли дополнительные детали? Версия ОС, текст ошибки, когда началось и т.д.")

    @dp.message(TicketStates.waiting_details)
    async def details_received(message: Message, state: FSMContext) -> None:
        details = (message.text or "").strip()
        data = await state.get_data()
        issue = data.get("issue", "(без описания)")
        app_name = data.get("app_name")
        platform = data.get("platform")

        ticket_id = create_ticket(
            message.from_user.id,
            message.from_user.username,
            issue,
            app_name,
            platform,
            details or None,
        )
        await state.clear()
        await message.answer(f"✅ Тикет #{ticket_id} создан. Передал в поддержку.")

        text = admin_message(ticket_id, message.from_user.id, message.from_user.username, issue, app_name, platform, details)
        await notify_admins(message.bot, config.admin_ids, text, ticket_id)

    @dp.message(Command("mytickets"))
    async def my_tickets(message: Message) -> None:
        tickets = fetch_user_tickets(message.from_user.id)
        if not tickets:
            await message.answer("У вас пока нет тикетов.")
            return

        lines = ["Ваши тикеты:"]
        for tid, issue, status, created in tickets[:10]:
            lines.append(f"#{tid} [{status}] {issue[:60]} ({created[:19]})")
        await message.answer("\n".join(lines))

    @dp.message(Command("tickets"))
    async def list_tickets(message: Message) -> None:
        if message.from_user.id not in config.admin_ids:
            await message.answer("Команда только для админов.")
            return

        tickets = fetch_open_tickets()
        if not tickets:
            await message.answer("Открытых тикетов нет.")
            return

        lines = ["Открытые тикеты:"]
        for tid, uid, username, issue, app_name, platform, created in tickets[:20]:
            lines.append(
                f"#{tid} | user:{uid} @{username or '-'} | {issue[:40]} | app:{app_name or '-'} | os:{platform or '-'} | {created[:19]}"
            )
        await message.answer("\n".join(lines))

    @dp.message(Command("close"))
    async def close_by_command(message: Message, command: CommandObject) -> None:
        if message.from_user.id not in config.admin_ids:
            await message.answer("Команда только для админов.")
            return

        if not command.args or not command.args.isdigit():
            await message.answer("Использование: /close <ticket_id>")
            return

        ticket_id = int(command.args)
        if close_ticket(ticket_id, message.from_user.id):
            await message.answer(f"Тикет #{ticket_id} закрыт.")
        else:
            await message.answer(f"Тикет #{ticket_id} не найден или уже закрыт.")

    @dp.callback_query(F.data.startswith("close:"))
    async def close_by_button(callback) -> None:
        if callback.from_user.id not in config.admin_ids:
            await callback.answer("Только для админов", show_alert=True)
            return

        ticket_id = int(callback.data.split(":", 1)[1])
        if close_ticket(ticket_id, callback.from_user.id):
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer(f"Тикет #{ticket_id} закрыт")
            await callback.message.answer(f"Тикет #{ticket_id} закрыт админом {callback.from_user.id}")
        else:
            await callback.answer("Тикет уже закрыт или не найден", show_alert=True)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    init_db()

    bot = Bot(token=config.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    register_handlers(dp, config)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
