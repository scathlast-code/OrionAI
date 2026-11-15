"""Telegram-native persistent access administration; no hosting dashboard needed.

Only bootstrap owners (ALLOWED_USERS) can change access. Granting manager access
never grants administrative privileges; manager application currently owner-only.
"""
import access_grants
from telegram.ext import CommandHandler


def install(app, scope, owner_ids):
    owners = {int(x) for x in owner_ids}

    async def users(update, context):
        uid = update.effective_user.id if update.effective_user else None
        if uid not in owners:
            await update.effective_message.reply_text('Керування доступом доступне лише власнику.')
            return
        args = context.args or []
        usage = ('/users list [orion|manager]\n'
                 '/users grant <orion|manager> <Telegram_ID>\n'
                 '/users revoke <orion|manager> <Telegram_ID>\n'
                 'Дозволи зберігаються в SQLite, не в Railway.\n'
                 'Увага: manager наразі допускає до інтерфейсу лише власників ALLOWED_USERS.')
        if not args:
            await update.effective_message.reply_text(usage)
            return
        cmd = args[0].lower()
        try:
            if cmd == 'list':
                target = args[1] if len(args)>1 else scope
                rows = access_grants.list_grants(target)
                lines = [f'{r[0]}: {access_grants.status(target,r[0])}' for r in rows[:60]]
                await update.effective_message.reply_text(f'Доступи {target}:\n'+'\n'.join(lines or ['Немає записів']))
                return
            if cmd in ('grant','revoke') and len(args)==3:
                target = args[1].lower()
                subject = int(args[2])
                if subject in owners and cmd == 'revoke':
                    await update.effective_message.reply_text('Не можна відкликати bootstrap-доступ власника через бота.')
                    return
                access_grants.set_grant(target,subject,cmd=='grant',actor_id=uid)
                await update.effective_message.reply_text(f'{"Дозволено" if cmd=="grant" else "Відкликано"}: {target}, Telegram ID {subject}.\nСтан: {access_grants.status(target,subject)}')
                return
        except (ValueError, OverflowError) as exc:
            await update.effective_message.reply_text('Некоректна команда або Telegram ID.')
            return
        await update.effective_message.reply_text(usage)

    app.add_handler(CommandHandler('users',users))
