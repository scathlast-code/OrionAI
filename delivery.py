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

async def send(bot,chat_id,text,**kw):
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
