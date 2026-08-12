"""
Aegis | Server Guardian — точка входа.

Деплой на Render:
  1. Создай Web Service (не Background Worker — так проще держать бесплатный
     план живым: Render пингует HTTP-порт, бот не засыпает).
  2. Переменные окружения: DISCORD_BOT_TOKEN (обязательно), PORT (Render ставит сам).
  3. Start command: python main.py
  4. Если хочешь, чтобы данные (whitelist, бэкапы, логи) не терялись при
     передеплое — подключи Render Disk и укажи AEGIS_DB_PATH на путь внутри
     этого диска, например /var/data/aegis.db.
"""

import asyncio
import logging
import os
import threading

import discord
from discord.ext import commands
from aiohttp import web

import config
import database as db
import security
import bot_commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("aegis")

INTENTS = discord.Intents.default()
INTENTS.members = True          # нужно для join-события и работы с ролями участников
INTENTS.message_content = True  # нужно для антиспама (анализ текста сообщений)
INTENTS.moderation = True        # audit-log/ban события

bot = commands.Bot(command_prefix=config.COMMAND_PREFIX, intents=INTENTS, help_command=None)


@bot.event
async def on_ready():
    log.info(f"Aegis запущен как {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        log.info(f"Синхронизировано {len(synced)} slash-команд")
    except Exception as e:
        log.exception(f"Не удалось синхронизировать команды: {e}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="за безопасностью сервера 🛡️")
    )


@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info(f"Aegis добавлен на сервер: {guild.name} ({guild.id})")
    await db.get_guild_config(guild.id)  # создаём дефолтный конфиг сразу


async def load_extensions():
    await security.setup(bot)
    await bot_commands.setup(bot)


# ── keep-alive веб-сервер для Render Web Service ────────────────

async def _handle_health(request):
    return web.Response(text="Aegis is running.")


async def start_webserver():
    app = web.Application()
    app.router.add_get("/", _handle_health)
    app.router.add_get("/health", _handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Keep-alive веб-сервер слушает порт {port}")


async def main():
    if not config.BOT_TOKEN:
        raise RuntimeError("Переменная окружения DISCORD_BOT_TOKEN не установлена.")

    await db.init_db()
    await load_extensions()
    await start_webserver()
    await bot.start(config.BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
