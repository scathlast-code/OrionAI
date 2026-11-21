'Telegram handlers for reliability settings, safe error diagnostics and measured tips.'
import logging
from telegram.ext import CommandHandler, TypeHandler
from telegram import Update
from reliability import Reliability

log=logging.getLogger('orion.reliability.telegram')

def install(app,scope):
    store=Reliability(scope)
    async def observe(update,ctx):
        user=update.effective_user
        if not user or user.is_bot: return
        msg=update.effective_message
        if not msg and not update.callback_query:return
        text=(msg.text or '') if msg else ''
        feature=(text.split()[0].split('@')[0][1:].lower() if text.startswith('/') else 'chat')
        try:
            store.record(user.id,feature)
            # Only surface tips in normal text interactions; never interrupt commands or button callbacks.
            if msg and text and not text.startswith('/'):
                tip=store.suggestion(user.id,commit=True)
                if tip: await msg.reply_text('💡 Порада OrionAI: '+tip+'\nВимкнути: /tips off')
        except Exception:
            log.exception('Failed to track interaction (scope=%s)',scope)
    async def tips(update,ctx):
        uid=update.effective_user.id
        args=[x.lower() for x in ctx.args]
        if args and args[0] in ('on','off'):
            store.set_option(uid,'tips_enabled',args[0]=='on')
        if len(args)>=2 and args[0]=='popup' and args[1] in ('on','off'):
            store.set_option(uid,'popup_enabled',args[1]=='on')
        row=store.preferences(uid)
        if not row:
            store.record(uid,'tips')
            row=store.preferences(uid)
        await update.effective_message.reply_text('Поради: '+('увімкнено' if row[1] else 'вимкнено')+'\nКонтекстні підказки: '+('увімкнено' if row[2] else 'вимкнено')+'\n/tips on|off; /tips popup on|off\nПерсональні поради — після 14 днів та ≥10 взаємодій, максимум двічі за 7 днів. Контекстні підказки — налаштування інтерфейсу, ще не підключені до всіх екранів.')
    async def diagnostics(update,ctx):
        await update.effective_message.reply_text('Діагностика OrionAI: у разі помилки бот реєструє ID інциденту, компонент, тип і довідку. Для повної причини адміністратор перевіряє журнали. Підтримка: /diagnostics')
    async def on_error(update,ctx):
        # Безпечні «шуми» Telegram — не збої: стара кнопка після перезапуску, «повідомлення не змінилось»
        msg=str(ctx.error).lower()
        if any(x in msg for x in ('query is too old','query id is invalid','message is not modified','message to edit not found')):
            log.info('Telegram: %s (пропущено)',ctx.error)
            return
        try:
            uid=update.effective_user.id if isinstance(update,Update) and update.effective_user else 0
            incident_id,data=store.incident(uid,ctx.error,'telegram_update')
            if isinstance(update,Update) and update.effective_chat:
                await ctx.bot.send_message(update.effective_chat.id,store.incident_text(incident_id,data))
        except Exception:
            log.exception('Failed to report incident')
    app.add_handler(TypeHandler(Update,observe),group=-90)
    app.add_handler(CommandHandler('tips',tips))
    app.add_handler(CommandHandler('diagnostics',diagnostics))
    app.add_error_handler(on_error)
    return store
