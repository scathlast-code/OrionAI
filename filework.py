"""Bounded, no-execution file handling for Telegram attachments.
Archive extraction only into a private temporary directory; never trust member paths.
"""
from __future__ import annotations
import io, os, re, tarfile, zipfile
from pathlib import Path, PurePosixPath
from PIL import Image, ImageOps, ImageDraw

MAX_FILES = 10
MAX_INPUT = 20 * 1024 * 1024
MAX_EXTRACT = 80 * 1024 * 1024
MAX_MEMBERS = 200

FORMATS = {
 '.zip': ('ZIP', 'Читання, розпакування, створення', 'Вбудовано'),
 '.7z': ('7-Zip', 'Читання та розпакування', 'Потрібна бібліотека py7zr'),
 '.rar': ('RAR/RAR5', 'Читання та розпакування', 'Потрібна утиліта unrar/bsdtar; створення RAR не передбачено'),
 '.tar': ('TAR', 'Читання, розпакування', 'Вбудовано'),
 '.tar.gz': ('TAR.GZ', 'Читання, розпакування', 'Вбудовано'),
 '.tgz': ('TGZ', 'Читання, розпакування', 'Вбудовано'),
 '.txt': ('TXT', 'Читання, створення', 'UTF-8 або визначення кодування'),
 '.pdf': ('PDF', 'Читання текстового PDF', 'Скановані PDF потребують OCR'),
 '.docx': ('Word', 'Читання і створення', 'Потрібен python-docx'),
 '.epub': ('EPUB', 'Читання', 'DRM-захист не обходиться'),
 '.jpg': ('JPEG', 'Читання і колажі', 'Pillow'),
 '.jpeg': ('JPEG', 'Читання і колажі', 'Pillow'),
 '.png': ('PNG', 'Читання і колажі', 'Pillow'),
 '.webp': ('WebP', 'Читання і колажі', 'Pillow'),
 '.csv': ('CSV', 'Читання', 'Текстові таблиці'),
 '.xlsx': ('XLSX', 'Читання', 'Потрібен openpyxl'),
}

class FileProblem(ValueError): pass

def safe_name(name):
    s = re.sub(r'[^\w.()\- а-яА-ЯіїєґІЇЄҐ]', '_', Path(name.replace('\\','/')).name)
    return s[:120] or 'file'

def member_name(name):
    p = PurePosixPath(name.replace('\\','/'))
    if p.is_absolute() or '..' in p.parts or not p.parts or ':' in p.parts[0]:
        raise FileProblem('Небезпечний шлях у архіві')
    return p

def unpack(path, output):
    path, output = Path(path), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    total = 0; count = 0; extracted=[]
    def target(name, size):
        nonlocal total, count
        rel = member_name(name)
        count += 1; total += max(0,size)
        if count > MAX_MEMBERS or total > MAX_EXTRACT:
            raise FileProblem('Архів перевищує безпечний ліміт кількості або обсягу файлів')
        dest = output.joinpath(*rel.parts)
        if not dest.resolve().is_relative_to(output.resolve()):
            raise FileProblem('Небезпечний шлях у архіві')
        return dest
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as arc:
            for item in arc.infolist():
                if item.is_dir(): continue
                if (item.external_attr >> 16) & 0o170000 == 0o120000: raise FileProblem("Символічні посилання заборонені")
                dest=target(item.filename,item.file_size)
                dest.parent.mkdir(parents=True,exist_ok=True)
                with arc.open(item) as src, dest.open('wb') as dst:
                    copy_bounded(src,dst,item.file_size)
                extracted.append(dest)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as arc:
            for item in arc:
                if item.isdir(): continue
                if not item.isfile(): raise FileProblem('Архів містить посилання або спеціальні файли')
                dest=target(item.name,item.size)
                dest.parent.mkdir(parents=True,exist_ok=True)
                with arc.extractfile(item) as src, dest.open('wb') as dst:
                    copy_bounded(src,dst,item.size)
                extracted.append(dest)
    else:
        raise FileProblem('Цей тип архіву поки не підтримується для безпечного розпакування. Для 7Z/RAR потрібен окремий перевірений обробник.')
    return extracted

def copy_bounded(src,dst,expected):
    size=0
    while True:
        chunk=src.read(65536)
        if not chunk: break
        size+=len(chunk)
        if size>MAX_EXTRACT or size>max(0,expected): raise FileProblem('Невідповідність розміру в архіві')
        dst.write(chunk)

def pack(paths, output):
    if not 1 <= len(paths) <= MAX_FILES: raise FileProblem('Можна додати від 1 до 10 файлів')
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as arc:
        for i,path in enumerate(paths):
            p=Path(path)
            if not p.is_file() or p.stat().st_size>MAX_INPUT: raise FileProblem('Файл відсутній або завеликий')
            arc.write(p,arcname=f'{i+1:02d}_{safe_name(p.name)}')
    return output

def collage(paths, output):
    if not 2<=len(paths)<=MAX_FILES: raise FileProblem('Для колажу потрібно 2–10 зображень')
    from math import ceil, sqrt
    cols=ceil(sqrt(len(paths))); rows=ceil(len(paths)/cols); cell=420; margin=12
    canvas=Image.new('RGB',(cols*cell+(cols+1)*margin,rows*cell+(rows+1)*margin),'white')
    for i,path in enumerate(paths):
        with Image.open(path) as src:
            im=ImageOps.contain(src.convert('RGB'),(cell,cell))
            x=margin+(i%cols)*(cell+margin)+(cell-im.width)//2
            y=margin+(i//cols)*(cell+margin)+(cell-im.height)//2
            canvas.paste(im,(x,y))
    canvas.save(output,quality=92)
    return output

def help_text():
    return '\n'.join(f'{ext} — {title}: {cap}. {note}' for ext,(title,cap,note) in FORMATS.items())
