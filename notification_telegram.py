"""Telegram commands and deep-sleep gate for each independently managed bot."""
import logging
from telegram import Update
from telegram.ext import CommandHandler, TypeHandler, ApplicationHandlerStop
import notification_modes as modes

log=logging.getLogger('orion.notifications')

def install(app,scope,admin_ids):
    def is_admin(uid):return uid in admin_ids
    async def guard(update,context):
        if not modes.is_sleeping(scope):return
        user=update.effective_user
        text=(update.effective_message.text or '').strip() if update.effective_message else ''
        # Only administrators may use the recovery command. No reply to other updates.
        if user and is_admin(user.id) and text.split(' ',1)[0].split('@')[0].lower()=='/wake':return
        # Власнику пояснюю, чому бот мовчить, інакше це виглядає як поломка. Стороннім не відповідаю.
        if user and is_admin(user.id):
            try:
                if update.callback_query:
                    await update.callback_query.answer('🌙 Бот у глибокому сні. Надішліть /wake, щоб розбудити.', show_alert=True)
                elif update.effective_message and update.effective_chat and update.effective_chat.type=='private':
                    await update.effective_message.reply_text('🌙 Бот у глибокому сні й не виконує запити. Розбудити: /wake')
            except Exception:
                log.debug('sleep notice failed',exc_info=True)
        raise ApplicationHandlerStop

    async def settings(update,context):
        uid=update.effective_user.id
        b=modes.bot_mode(scope);u=modes.user_mode(scope,uid)
        await update.effective_message.reply_text(
            f'🔔 Сповіщення ({scope})\nЗвук Mini App: {u["sound"]}\nВібрація Mini App: {u["vibration"]}\n'
            f'Тиха година: {"увімкнена" if b["quiet_enabled"] else "вимкнена"} ({b["quiet_start"]}–{b["quiet_end"]}, {b["timezone"]})\n'
            f'Глибокий сон: {"увімкнено" if b["deep_sleep"] else "вимкнено"}\n'
            'Команди: /sound soft|double|melody|urgent|low; /vibration short|double|triple|long|sos; '
            '/quiet on HH:MM HH:MM або /quiet off (адмін); /sleep (адмін); /wake (адмін).\n'
            'Звуки та вібрація — налаштування для Mini App, не системного Telegram push.')

    async def sound(update,context):
        uid=update.effective_user.id
        try:modes.set_user_mode(scope,uid,'sound',context.args[0]); msg='Звук збережено для Mini App.'
        except (IndexError,ValueError):msg='Оберіть: '+', '.join(modes.SOUNDS)
        await update.effective_message.reply_text(msg)

    async def vibration(update,context):
        uid=update.effective_user.id
        try:modes.set_user_mode(scope,uid,'vibration',context.args[0]);msg='Вібрацію збережено для Mini App.'
        except (IndexError,ValueError):msg='Оберіть: '+', '.join(modes.VIBRATIONS)
        await update.effective_message.reply_text(msg)

    async def quiet(update,context):
        if not is_admin(update.effective_user.id):return
        try:
            args=context.args
            if args==['off']:modes.set_quiet(scope,False)
            elif len(args)==3 and args[0]=='on':modes.set_quiet(scope,True,args[1],args[2])
            else:raise ValueError('Usage')
            msg='Налаштування тихої години збережено. Для звичайних Telegram push застосовуйте disable_notification при відправленні.'
        except ValueError:msg='Формат: /quiet on 23:00 08:00 або /quiet off'
        await update.effective_message.reply_text(msg)

    async def sleep(update,context):
        if not is_admin(update.effective_user.id):return
        modes.set_sleep(scope,True)
        await update.effective_message.reply_text('🌙 Глибокий сон активовано. Звичайні Telegram-запити блокуються. Адміністратор може виконати /wake.')

    async def wake(update,context):
        if not is_admin(update.effective_user.id):return
        modes.set_sleep(scope,False)
        await update.effective_message.reply_text('☀️ Бота пробуджено.')

    app.add_handler(TypeHandler(Update,guard),group=-105)
    for command,fn in [('notifications',settings),('sound',sound),('vibration',vibration),('quiet',quiet),('sleep',sleep),('wake',wake)]:
        app.add_handler(CommandHandler(command,fn))
