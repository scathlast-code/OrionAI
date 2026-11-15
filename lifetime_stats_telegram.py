"""Lifetime numeric statistics. /stats = self; /stats users = owner-only."""
import os
import lifetime_stats
from telegram.ext import CommandHandler


def _owners():
    raw=os.environ.get('OWNER_IDS','')+','+os.environ.get('ADMIN_IDS','')
    return {int(v.strip()) for v in raw.split(',') if v.strip().isdigit()}


def _format(t):
    return (f"Запитів: {t['requests']}\nЧас виконання (зафіксований): {t['duration_ms']/1000:.1f} с\n"
            f"Токенів: {t['input_tokens']} вхідних / {t['output_tokens']} вихідних\n"
            f"Витрати: {t['cost_microunits']/1000000:.6f} облікових одиниць\n"
            f"Помилок: {t['failures']}")


def install(app, bot_name):
    async def cmd(update, context):
        user=update.effective_user
        if user is None or update.effective_chat is None or update.effective_chat.type!='private': return
        path=__import__('datadir').path('lifetime_stats.db','LIFETIME_STATS_DB')
        args=context.args or []
        if args and args[0]=='users':
            if user.id not in _owners():
                return await update.message.reply_text('Недостатньо прав для статистики інших користувачів.')
            rows=lifetime_stats.by_user(path,bot=bot_name)
            body='\n'.join(f"{r['user_id']}: {r['requests']} запитів" for r in rows)
            return await update.message.reply_text(('Користувачі ('+bot_name+'):\n'+(body or 'Немає даних'))[:3900])
        if args and args[0]=='all':
            if user.id not in _owners():
                return await update.message.reply_text('Недостатньо прав.')
            target=None
        else:
            target=user.id
        totals=lifetime_stats.totals(path,bot=bot_name,user_id=target)
        modes=lifetime_stats.by_mode(path,bot=bot_name,user_id=target)
        lines=[f"Статистика за весь час — {bot_name}",_format(totals),'Режими:']
        lines += [f"{mode}: {v['requests']} запитів" for mode,v in modes.items()]
        lines.append('Інкогніто: тільки числові показники; зміст не зберігається в цьому журналі.')
        await update.message.reply_text('\n'.join(lines)[:3900])
    app.add_handler(CommandHandler('stats',cmd))
