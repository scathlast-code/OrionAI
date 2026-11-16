"""Telegram-aware delivery. Strictly no more than three text messages."""
from io import BytesIO
import os
import cleanview
MAX_CHARS=3800

def split_paragraphs(text,limit=MAX_CHARS):
    result=[]; part=''
    for paragraph in text.splitlines(keepends=True):
        while len(paragraph)>limit:
            cut=paragraph.rfind(' ',0,limit)
            if cut<limit//3: cut=limit
            piece,paragraph=paragraph[:cut],paragraph[cut:].lstrip()
            if part: result.append(part);part=''
            result.append(piece)
        if len(part)+len(paragraph)>limit and part:
            result.append(part);part=''
        part+=paragraph
    if part: result.append(part)
    return result or ['']

def _u16(s):
    return len(s.encode('utf-16-le'))//2


def _tech_entities(payload, head, foot):
    """Технічний текст (хто відповів, спроби, заголовок завдання) — у сірій цитаті курсивом, щоб відповідь ШІ не губилася."""
    from telegram import MessageEntity
    ents=[]
    if head:
        h=head.rstrip('\n')
        ents+=[MessageEntity(MessageEntity.BLOCKQUOTE,0,_u16(h)),MessageEntity(MessageEntity.ITALIC,0,_u16(h))]
    if foot:
        off=_u16(payload)-_u16(foot)
        kind=MessageEntity.EXPANDABLE_BLOCKQUOTE if foot.count('\n')>=2 else MessageEntity.BLOCKQUOTE
        ents+=[MessageEntity(kind,off,_u16(foot)),MessageEntity(MessageEntity.ITALIC,off,_u16(foot))]
    return ents


async def send(bot,chat_id,text,head='',foot='',html=None,**kw):
    """head/foot — технічні рядки над і під відповіддю ШІ: показуються окремо, у цитаті.
    html — та сама відповідь, оформлена (жирне, списки, посилання); якщо Telegram її не прийме — надсилаємо звичайний text."""
    if html and 'parse_mode' not in kw:
        from html import escape as _e
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        h=head.strip()
        f=foot.strip()
        payload=(f'<blockquote><i>{_e(h)}</i></blockquote>\n' if h else '')+html.strip()+(
            (f'\n\n<blockquote expandable><i>{_e(f)}</i></blockquote>' if f.count('\n')>=2 else f'\n\n<blockquote><i>{_e(f)}</i></blockquote>') if f else '')
        if len(payload)<3900:
            local_kw=dict(kw)
            local_kw.setdefault('reply_markup',InlineKeyboardMarkup([[InlineKeyboardButton('🧹 Чистий текст',callback_data='view:toggle')]]))
            try:
                sent=await bot.send_message(chat_id,payload,parse_mode='HTML',disable_web_page_preview=True,**local_kw)
                cleanview.store(os.getenv('DB_PATH','/data/bot.db'),chat_id,sent.message_id,head+text+('\n\n'+foot if foot else ''))
                return
            except Exception:  # noqa: BLE001 — неправильний HTML від моделі: просто текст
                pass
    if head or foot:
        if 'parse_mode' in kw or len(split_paragraphs(head+text+('\n\n'+foot if foot else '')))>1:
            text=head+text+('\n\n'+foot if foot else '')   # довге або HTML — як раніше, одним текстом
        else:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            payload=head+text+('\n\n'+foot if foot else '')
            local_kw=dict(kw)
            local_kw.setdefault('reply_markup',InlineKeyboardMarkup([[InlineKeyboardButton('🧹 Чистий текст',callback_data='view:toggle')]]))
            try:
                sent=await bot.send_message(chat_id,payload,entities=_tech_entities(payload,head,foot),**local_kw)
            except Exception:  # noqa: BLE001 — старий клієнт чи незнайомий тип цитати: просто текст
                sent=await bot.send_message(chat_id,payload,**local_kw)
            cleanview.store(os.getenv('DB_PATH','/data/bot.db'),chat_id,sent.message_id,payload)
            return
    chunks=split_paragraphs(text)
    if len(chunks)>3:
        from telegram import InputFile
        data=BytesIO(text.encode('utf-8'));data.name='OrionAI_answer.txt'
        await bot.send_document(chat_id,InputFile(data,filename=data.name),caption='📄 Відповідь довша за три повідомлення — надсилаю файлом.')
        return
    if len(chunks)>1:
        await bot.send_message(chat_id,f'📨 Довга відповідь: {len(chunks)} частини.')
    for i,chunk in enumerate(chunks,1):
        head=f'[{i}/{len(chunks)}]\n' if len(chunks)>1 else ''
        payload=head+chunk
        # Toggle is only attached to plain text, not HTML-formatted messages.
        if 'parse_mode' not in kw and len(payload) < 3900:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            local_kw=dict(kw)
            local_kw['reply_markup']=InlineKeyboardMarkup([[InlineKeyboardButton('🧹 Чистий текст',callback_data='view:toggle')]])
            sent=await bot.send_message(chat_id,payload,**local_kw)
            cleanview.store(os.getenv('DB_PATH','/data/bot.db'),chat_id,sent.message_id,payload)
        else:
            await bot.send_message(chat_id,payload,**kw)
