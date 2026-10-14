from telegram import Update
from telegram.ext import ContextTypes

async def fuckyou_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("no fuck you nigga", parse_mode="Markdown")
