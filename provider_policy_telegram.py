"""Owner-only policy commands; no credentials or secret URLs exposed."""
import os
from telegram.ext import CommandHandler
import provider_policy_monitor as pm

def install(app,bot_name):
    async def handler(update,context):
        if not update.effective_user or not update.effective_chat or update.effective_chat.type!='private':return
        owners={int(v) for v in (os.getenv('OWNER_IDS','')+','+os.getenv('ADMIN_IDS','')).split(',') if v.strip().isdigit()}
        if update.effective_user.id not in owners:
            await update.effective_message.reply_text('Лише для власника.');return
        path=__import__('datadir').path('provider_policies.db','POLICY_MONITOR_DB')
        args=context.args or []
        if args and args[0]=='add' and len(args)==3:
            try:pm.register(path,args[1],args[2]);msg='Джерело додано. /policies scan встановить початковий знімок.'
            except ValueError as e:msg=f'Помилка: {e}'
        elif args and args[0]=='scan':
            rows=pm.scan(path)
            msg='\n'.join(f"{r['name']}: "+('ПОМИЛКА '+r['error'] if 'error' in r else 'ЗМІНА — перевірте умови!' if r['changed'] else 'Початковий знімок' if r['initial_baseline'] else 'Без змін') for r in rows) or 'Немає джерел.'
        elif args and args[0]=='alerts':
            alerts=pm.active_alerts(path)
            msg='\n'.join(f"⚠️ #{a['id']} {a['name']}: {a['description']} — джерела: {', '.join(pm.alert_sources(path,a['name']))}" for a in alerts) or 'Активних тривог немає.'
        elif args and args[0]=='ack' and len(args)==2 and args[1].isdigit():
            msg='Тривогу підтверджено як переглянуту.' if pm.acknowledge_alert(path,int(args[1])) else 'Тривогу не знайдено.'
        elif args and args[0]=='review'  and len(args)==2 and args[1].isdigit():
            msg='Позначено як переглянуте.' if pm.acknowledge(path,int(args[1])) else 'Подію не знайдено або вже переглянуто.'
        elif args and args[0]=='sources':
            rows=pm.sources(path)
            msg='\n'.join(f"{r['name']}: {r['url']} — "+('помилка перевірки' if r['last_error'] else 'перевірено' if r['checked_at'] else 'не перевірено') for r in rows) or 'Немає джерел.'
        else:
            rows=pm.changes(path)
            msg='Зміни правил (виявлено зміну вмісту, НЕ юридичний аналіз):\n'+('\n'.join(f"#{r['id']} {r['name']}: {r['status']}" for r in rows) or 'Немає змін.')+'\n\nКоманди: /policies sources | add НАЗВА HTTPS_URL | scan | alerts | ack ID | review ID\nПісля зміни перевірте дату чинності, тариф, ліміти, API та необхідність оновлення ключів. Нічого не приймається автоматично.'
        pending=pm.active_alerts(path)
        if pending and (not args or args[0] not in ('alerts','ack')):
            msg += f"\n\n⚠️ Невирішених тривог: {len(pending)}. /policies alerts"
        await update.effective_message.reply_text(msg[:3900])
    app.add_handler(CommandHandler('policies',handler))
