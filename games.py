"""
Aegis | Server Guardian — мини-игры на внутренней валюте сервера.

Экономика привязана к (guild_id, user_id) — у каждого сервера свой
баланс участника. Валюта существует только "для развлечения": не
конвертируется, не покупает реальных ролей/товаров (это можно
достроить отдельно через shop-таблицу при желании).
"""

import random
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db


def _fmt(amount: int) -> str:
    return f"{amount} {config.CURRENCY_ICON}"


class Games(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── баланс / ежедневная награда / лидерборд ───────────────────

    @app_commands.command(name="balance", description="Показать баланс")
    async def balance(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        bal = await db.get_balance(interaction.guild.id, target.id)
        await interaction.response.send_message(
            f"{'Ваш' if target == interaction.user else f'{target.display_name} —'} баланс: **{_fmt(bal)}**"
        )

    @app_commands.command(name="daily", description=f"Получить ежедневную награду ({config.DAILY_REWARD} {config.CURRENCY_ICON})")
    async def daily(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        last = await db.get_last_daily(guild_id, user_id)
        now = int(time.time())
        cooldown = config.DAILY_COOLDOWN_HOURS * 3600
        remaining = cooldown - (now - last)
        if remaining > 0:
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            await interaction.response.send_message(
                f"⏳ Уже получено. Следующая награда через {hours}ч {minutes}м.", ephemeral=True
            )
            return
        new_balance = await db.change_balance(guild_id, user_id, config.DAILY_REWARD)
        await db.set_last_daily(guild_id, user_id, now)
        await interaction.response.send_message(
            f"🎁 Получено: **{_fmt(config.DAILY_REWARD)}**. Баланс: **{_fmt(new_balance)}**"
        )

    @app_commands.command(name="leaderboard", description="Топ сервера по балансу")
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await db.get_leaderboard(interaction.guild.id, limit=10)
        if not rows:
            await interaction.response.send_message("Пока никто ничего не заработал.", ephemeral=True)
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{prefix} <@{r['user_id']}> — {_fmt(r['balance'])}")
        embed = discord.Embed(title="🏆 Топ сервера", description="\n".join(lines), color=config.EMBED_COLOR_OK)
        await interaction.response.send_message(embed=embed)

    # ── /coinflip ───────────────────────────────────────────────

    @app_commands.command(name="coinflip", description="Поставить на орла/решку")
    @app_commands.choices(side=[
        app_commands.Choice(name="Орёл", value="heads"),
        app_commands.Choice(name="Решка", value="tails"),
    ])
    async def coinflip(self, interaction: discord.Interaction, side: app_commands.Choice[str], bet: int):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        if bet < config.COINFLIP_MIN_BET:
            await interaction.response.send_message(f"⛔ Минимальная ставка: {_fmt(config.COINFLIP_MIN_BET)}", ephemeral=True)
            return
        balance = await db.get_balance(guild_id, user_id)
        if bet > balance:
            await interaction.response.send_message(f"⛔ Недостаточно средств. Баланс: {_fmt(balance)}", ephemeral=True)
            return

        result = random.choice(["heads", "tails"])
        won = result == side.value
        delta = bet if won else -bet
        new_balance = await db.change_balance(guild_id, user_id, delta)
        result_ru = "Орёл 🦅" if result == "heads" else "Решка 🪙"

        embed = discord.Embed(
            title="🪙 Монетка",
            description=f"Выпало: **{result_ru}**\n"
                        f"{'✅ Победа!' if won else '❌ Проигрыш.'} "
                        f"{'+' if won else ''}{delta} {config.CURRENCY_ICON}\n"
                        f"Баланс: **{_fmt(new_balance)}**",
            color=config.EMBED_COLOR_OK if won else config.EMBED_COLOR_ALERT,
        )
        await interaction.response.send_message(embed=embed)

    # ── /slots ──────────────────────────────────────────────────

    @app_commands.command(name="slots", description="Крутануть слот-машину")
    async def slots(self, interaction: discord.Interaction, bet: int):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        if bet < config.SLOTS_MIN_BET:
            await interaction.response.send_message(f"⛔ Минимальная ставка: {_fmt(config.SLOTS_MIN_BET)}", ephemeral=True)
            return
        balance = await db.get_balance(guild_id, user_id)
        if bet > balance:
            await interaction.response.send_message(f"⛔ Недостаточно средств. Баланс: {_fmt(balance)}", ephemeral=True)
            return

        reels = [random.choice(config.SLOTS_EMOJIS) for _ in range(3)]
        line = " | ".join(reels)

        if reels[0] == reels[1] == reels[2]:
            multiplier = config.SLOTS_PAYOUTS.get(reels[0], 2)
            winnings = bet * multiplier
            delta = winnings
            outcome = f"🎉 Джекпот! x{multiplier}"
        elif len(set(reels)) == 2:
            delta = bet // 2
            outcome = "🙂 Пара совпала — маленький выигрыш."
        else:
            delta = -bet
            outcome = "❌ Мимо."

        new_balance = await db.change_balance(guild_id, user_id, delta)
        embed = discord.Embed(
            title="🎰 Слоты",
            description=f"[ {line} ]\n\n{outcome}\n"
                        f"{'+' if delta >= 0 else ''}{delta} {config.CURRENCY_ICON}\n"
                        f"Баланс: **{_fmt(new_balance)}**",
            color=config.EMBED_COLOR_OK if delta > 0 else config.EMBED_COLOR_ALERT,
        )
        await interaction.response.send_message(embed=embed)

    # ── /dice — дуэль на костях с другим игроком ──────────────────

    @app_commands.command(name="dice", description="Дуэль на костях против другого участника (ставка на кону)")
    async def dice_duel(self, interaction: discord.Interaction, opponent: discord.Member, bet: int):
        guild_id = interaction.guild.id
        challenger = interaction.user

        if opponent.id == challenger.id or opponent.bot:
            await interaction.response.send_message("⛔ Выберите другого живого участника.", ephemeral=True)
            return
        if bet < config.COINFLIP_MIN_BET:
            await interaction.response.send_message(f"⛔ Минимальная ставка: {_fmt(config.COINFLIP_MIN_BET)}", ephemeral=True)
            return

        challenger_balance = await db.get_balance(guild_id, challenger.id)
        opponent_balance = await db.get_balance(guild_id, opponent.id)
        if bet > challenger_balance:
            await interaction.response.send_message(f"⛔ У вас недостаточно средств. Баланс: {_fmt(challenger_balance)}", ephemeral=True)
            return
        if bet > opponent_balance:
            await interaction.response.send_message(f"⛔ У {opponent.display_name} недостаточно средств для такой ставки.", ephemeral=True)
            return

        view = _DiceDuelView(challenger, opponent, bet, guild_id)
        await interaction.response.send_message(
            f"🎲 {challenger.mention} вызывает {opponent.mention} на дуэль на костях! "
            f"Ставка: **{_fmt(bet)}** с каждого.\n{opponent.mention}, принимаете?",
            view=view,
        )
        view.message = await interaction.original_response()


class _DiceDuelView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, bet: int, guild_id: int):
        super().__init__(timeout=config.DUEL_TIMEOUT_SECONDS)
        self.challenger = challenger
        self.opponent = opponent
        self.bet = bet
        self.guild_id = guild_id
        self.message: discord.Message | None = None
        self.resolved = False

    async def on_timeout(self):
        if self.resolved or self.message is None:
            return
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(content=f"⌛ {self.opponent.mention} не ответил вовремя. Дуэль отменена.", view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success, emoji="🎲")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("⛔ Только вызванный участник может ответить.", ephemeral=True)
            return
        self.resolved = True
        for item in self.children:
            item.disabled = True

        roll_challenger = random.randint(1, 6)
        roll_opponent = random.randint(1, 6)

        if roll_challenger == roll_opponent:
            result_text = f"🎲 {self.challenger.mention}: {roll_challenger} vs {self.opponent.mention}: {roll_opponent} — ничья! Ставки возвращены."
        else:
            winner, loser = (self.challenger, self.opponent) if roll_challenger > roll_opponent else (self.opponent, self.challenger)
            await db.change_balance(self.guild_id, winner.id, self.bet)
            await db.change_balance(self.guild_id, loser.id, -self.bet)
            result_text = (
                f"🎲 {self.challenger.mention}: {roll_challenger} vs {self.opponent.mention}: {roll_opponent}\n"
                f"🏆 Победитель: {winner.mention} (+{_fmt(self.bet)})"
            )

        await interaction.response.edit_message(content=result_text, view=self)

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger, emoji="✖️")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("⛔ Только вызванный участник может ответить.", ephemeral=True)
            return
        self.resolved = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=f"✖️ {self.opponent.mention} отклонил дуэль.", view=self)


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))
