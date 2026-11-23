import asyncio
import time
from decimal import Decimal
import discord
from src.config import ALLOWED_USER_ID, DISCORD_BOT_TOKEN, FINANCE_CHANNEL_ID
from src.db import (
    delete_transaction,
    get_all_balances,
    get_reference_data,
    pool,
    record_transaction,
)
from src.llm_parser import parse_message

def format_idr(amount: Decimal) -> str:
    return f"Rp {amount:,.0f}".replace(",", ".")

class UndoTransactionView(discord.ui.View):
    def __init__(self, transaction_id: int):
        super().__init__(timeout=300)
        self.transaction_id = transaction_id

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def undo_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message(
                "You are not authorized to undo this transaction.", ephemeral=True
            )
            return

        reverted = await asyncio.to_thread(delete_transaction, self.transaction_id)
        button.disabled = True

        if not reverted:
            await interaction.response.edit_message(
                content="⚠️ Transaction was already undone or not found.", view=self
            )
            return

        embed = discord.Embed(
            title="↩️ Transaction Undone",
            description=(
                f"Reverted **{format_idr(reverted['amount'])}** on **{reverted['account']}**.\n"
                f"Restored Balance: **{format_idr(reverted['restored_balance'])}**"
            ),
            color=discord.Color.light_grey(),   
        )
        await interaction.response.edit_message(embed=embed, view=self)

class TallyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

    async def on_ready(self):
        assert self.user is not None
        print(f"✅ Tally is online as {self.user} (ID: {self.user.id})")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.author.id != ALLOWED_USER_ID:
            return
        if message.channel.id != FINANCE_CHANNEL_ID:
            return

        async with message.channel.typing():
            try:
                accounts, categories = await asyncio.to_thread(get_reference_data)
                parsed = await asyncio.to_thread(
                    parse_message, message.content, accounts, categories
                )

                if parsed.intent == "check_balance":
                    balances = await asyncio.to_thread(get_all_balances)
                    total = sum((row["balance"] for row in balances), Decimal("0"))

                    embed = discord.Embed(
                        title="💰 Account Balances",
                        color=discord.Color.blurple(),
                    )

                    for row in balances:
                        embed.add_field(
                            name=f"{row['name']} ({row['account_type']})",
                            value=format_idr(row["balance"]),
                            inline=True,
                        )

                    embed.set_footer(text=f"Total Net Balance: {format_idr(total)}")
                    await message.reply(embed=embed, mention_author=False)
                    return

                if (
                    parsed.intent == "record_transaction"
                    and parsed.tx_type
                    and parsed.amount
                    and parsed.amount > 0
                    and parsed.account
                ):
                    result = await asyncio.to_thread(
                        record_transaction,
                        parsed.tx_type,
                        Decimal(str(parsed.amount)),
                        parsed.account,
                        parsed.category or "Other",
                        parsed.note or message.content[:60],
                        message.content,
                    )

                    is_income = result["type"] == "income"
                    sign = "+" if is_income else "-"

                    color = (
                        discord.Color.green() if is_income else discord.Color.red()
                    )
                    title = (
                        "📈 Income Recorded" if is_income else "💸 Expense Recorded"
                    )

                    embed = discord.Embed(title=title, color=color)
                    embed.add_field(
                        name="Amount",
                        value=f"**{sign}{format_idr(result['amount'])}**",
                        inline=True,
                    )
                    embed.add_field(
                        name="Account", value=result["account"], inline=True
                    )
                    embed.add_field(
                        name="Category", value=result["category"], inline=True
                    )

                    if result["note"]:
                        embed.add_field(
                            name="Note", value=result["note"], inline=False
                        )
                    embed.set_footer(
                        text=f"New {result['account']} Balance: {format_idr(result['new_balance'])}"
                    )

                    view = UndoTransactionView(result["transaction_id"])
                    await message.reply(embed=embed, view=view, mention_author=False)
                    return

                hint = (
                    parsed.reply_hint
                    or "I couldn't catch the amount or account. Try: `lunch 30k from BCA` or `check balances`."
                )

                await message.reply(f"🤔 {hint}", mention_author=False)
            
            except Exception as exc:
                await message.reply(
                    f"❌ Error processing message: `{exc}`", mention_author=False
                )

        t0 = time.perf_counter()
        accounts, categories = await asyncio.to_thread(get_reference_data)

        t1 = time.perf_counter()
        parsed = await asyncio.to_thread(parse_message, message.content, accounts, categories)

        t2 = time.perf_counter()
        print(f"[Timing] DB fetch: {t1 - t0:.2f}s | Gemini: {t2 - t1:.2f}s")

if __name__ == "__main__":
    bot = TallyBot()
    try:
        bot.run(DISCORD_BOT_TOKEN)
    finally:
        pool.close()
