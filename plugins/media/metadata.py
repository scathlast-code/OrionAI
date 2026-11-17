import gzip
import json
import mimetypes
import shutil
import stat
import tarfile
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from email import encoders, policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from lxml import etree, html

from .common import JobError, run
from .media import FF, probe_media, read_html

REPORT_ONLY = {
    ".7z", ".rar", ".exe", ".dll", ".msi", ".apk", ".appimage", ".deb", ".rpm",
    ".elf", ".bin", ".jar", ".dmg", ".iso",
}
IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".heic"}
AUDIO_VIDEO = {
    ".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".flac", ".wav", ".m4b", ".aiff",
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".wma", ".wmv",
}
TEXT = {".txt", ".md", ".csv", ".tsv", ".srt", ".vtt", ".css", ".js"}
WEB = {".html", ".htm", ".mht", ".mhtml"}
ARCHIVES = {".zip", ".tar", ".tar.gz", ".tgz"}
LEGACY = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/tiff": ".tiff", "audio/ogg": ".ogg",
    "audio/mp4": ".m4a", "video/x-matroska": ".mkv",
}


def suffix(path):
    return ".tar.gz" if path.name.lower().endswith(".tar.gz") else path.suffix.lower()


def cleaned_name(path):
    extension = suffix(path)
    stem = path.name[:-len(extension)] if extension else path.name
    return path.with_name(stem + ".cleaned" + extension)


async def report(path):
    extension = suffix(path)
    parts = [f"Файл · {path.name}", f"Розмір · {path.stat().st_size:,} байтів"]
    try:
        data = json.loads(await run("exiftool", "-j", "-G1", "-a", "-s", "-u", path))[0]
        parts.extend(
            f"{key} · {value}" for key, value in data.items()
            if key != "SourceFile" and not key.startswith("System:")
        )
    except (JobError, OSError, ValueError, IndexError):
        parts.append("ExifTool не прочитав поля цього формату.")

    if extension in AUDIO_VIDEO:
        try:
            data = await probe_media(path)
            tags = data.get("format", {}).get("tags", {})
            parts.extend(f"Контейнер · {key} · {value}" for key, value in tags.items())
            for stream in data.get("streams", []):
                parts.extend(
                    f"Доріжка {stream['index']} · {key} · {value}"
                    for key, value in stream.get("tags", {}).items()
                )
        except (JobError, OSError, ValueError):
            pass
    elif extension in WEB:
        raw = path.read_bytes()
        if extension in {".mht", ".mhtml"}:
            message = BytesParser(policy=policy.default).parsebytes(raw)
            parts.extend(f"MIME {key} · {value}" for key, value in message.items())
            body = message.get_body(preferencelist=("html",))
            raw = body.get_payload(decode=True) if body is not None else b""
        document = read_html(raw or b"")
        parts.extend(
            html.tostring(tag, encoding="unicode", with_tail=False)
            for tag in document.xpath("//meta | /html/head/title")
        )
    elif extension == ".zip":
        try:
            with ZipFile(path) as archive:
                parts.append(f"Коментар ZIP · {archive.comment.decode('utf-8', 'replace')}")
                for item in archive.infolist()[:600]:
                    note = item.comment.decode("utf-8", "replace")
                    parts.append(
                        f"{item.filename} · {item.date_time} · {item.file_size} байтів "
                        f"· extra {len(item.extra)} байтів · коментар {note}"
                    )
        except BadZipFile:
            parts.append("Цей ZIP не вдалося прочитати.")
    elif extension in {".tar", ".tar.gz", ".tgz"}:
        try:
            with tarfile.open(path, "r:*") as archive:
                for index, item in enumerate(archive):
                    if index >= 600:
                        parts.append("У звіті наведені перші 600 вкладень.")
                        break
                    parts.append(
                        f"{item.name} · mtime {item.mtime} · uid {item.uid} · gid {item.gid}"
                    )
        except tarfile.TarError:
            parts.append("Цей TAR не вдалося прочитати.")
    elif extension in {".rar", ".7z"}:
        try:
            parts.append(await run("7z", "l", "-slt", "-p-", path))
        except (JobError, OSError) as exc:
            parts.append(str(exc))
    elif extension not in REPORT_ONLY:
        try:
            fields = await run("mat2", "--no-sandbox", "--show", path)
            if fields.strip():
                parts.append(fields.strip().replace(str(path), path.name))
        except (JobError, OSError):
            pass
    return "\n".join(parts)[:400_000] + "\n"


def strip_html(raw):
    document = read_html(raw)
    for tag in document.xpath("//meta"):
        if "charset" in tag.attrib:
            tag.set("charset", "utf-8")
        elif tag.get("http-equiv", "").lower() == "content-type":
            tag.set("content", "text/html; charset=utf-8")
        elif tag.get("name", "").lower() != "viewport":
            tag.drop_tree()
    for tag in document.xpath(
        '/html/head/title | //script[@type="application/ld+json"] | //link[@rel]'
    ):
        if tag.tag != "link" or set(tag.get("rel", "").lower().split()) & {
            "canonical", "author", "shortlink",
        }:
            tag.drop_tree()
    head = document.find("head")
    if head is None:
        head = etree.SubElement(document, "head")
        document.insert(0, head)
    if not head.xpath('meta[@charset] | meta[@http-equiv]'):
        head.insert(0, etree.Element("meta", charset="utf-8"))
    return html.tostring(document, encoding="utf-8", method="html")


@dataclass
class Budget:
    size: int = 0
    count: int = 0

    def take(self, size):
        self.size += size
        self.count += 1
        if self.size > 240_000_000 or self.count > 600:
            raise JobError("Вкладення перевищують ліміт розпакування.")


async def clean_mhtml(source, dest, budget, depth):
    message = BytesParser(policy=policy.default).parsebytes(source.read_bytes())
    if not message.is_multipart() and message.get_content_type() != "text/html":
        raise JobError("Не вдалося прочитати структуру MHTML.")
    allowed = {
        "mime-version", "content-type", "content-transfer-encoding",
        "content-location", "content-id", "content-disposition",
    }
    for index, part in enumerate(message.walk()):
        for key in set(part.keys()):
            if key.lower() not in allowed:
                del part[key]
        if part.is_multipart():
            continue
        mime = part.get_content_type()
        if mime in {"text/plain", "text/css", "text/javascript", "application/javascript"}:
            continue
        data = part.get_payload(decode=True)
        if data is None:
            raise JobError("Не вдалося прочитати частину MHTML.")
        if mime == "text/html":
            data = strip_html(data)
            part.set_param("charset", "utf-8")
        else:
            extension = MIME_EXTENSIONS.get(mime) or mimetypes.guess_extension(mime)
            if extension is None:
                raise JobError(f"Непідтримувана частина MHTML · {mime}")
            budget.take(len(data))
            with tempfile.TemporaryDirectory(dir=source.parent) as directory:
                child = Path(directory) / f"part{index}{extension}"
                child.write_bytes(data)
                result = await clean(child, budget, depth + 1)
                if suffix(result) != suffix(child):
                    raise JobError("Очищення змінює формат вкладення MHTML.")
                data = result.read_bytes()
        del part["Content-Transfer-Encoding"]
        part.set_payload(data)
        encoders.encode_base64(part)
    dest.write_bytes(message.as_bytes(policy=policy.SMTP))
    return dest


def archive_path(name):
    path = PurePosixPath(name)
    if (
        not path.parts or path.is_absolute() or ".." in path.parts
        or "\\" in name or ":" in name or any(ord(char) < 32 for char in name)
    ):
        raise JobError("В архіві є небезпечний або порожній шлях.")
    return path


async def clean_member(reader, name, size, parent, budget, depth):
    budget.take(size)
    with tempfile.TemporaryDirectory(dir=parent) as directory:
        path = Path(directory) / name.name
        with path.open("wb") as dest:
            shutil.copyfileobj(reader, dest)
        try:
            result = await clean(path, budget, depth + 1)
        except JobError as exc:
            raise JobError(f"Вкладення {name} · {exc}") from None
        if suffix(result) != suffix(path):
            name = name.with_suffix(suffix(result))
        data = result.read_bytes()
        if len(data) > size:
            budget.take(len(data) - size)
        return str(name), data


async def clean_archive(source, dest, budget, depth):
    seen = set()

    def unique(name):
        key = name.rstrip("/").casefold()
        if key in seen:
            raise JobError(f"В архіві дублюється шлях {name}.")
        seen.add(key)

    if suffix(source) == ".zip":
        with ZipFile(source) as incoming, ZipFile(dest, "w", ZIP_DEFLATED) as outgoing:
            for item in incoming.infolist():
                name = archive_path(item.filename)
                mode = stat.S_IFMT(item.external_attr >> 16)
                if item.flag_bits & 1 or mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise JobError("В архіві є зашифрований або спеціальний файл.")
                if name.parts[0] == "__MACOSX" or name.name in {".DS_Store", "Thumbs.db"}:
                    continue
                if item.is_dir():
                    budget.take(0)
                    label, data = str(name) + "/", b""
                else:
                    with incoming.open(item) as reader:
                        label, data = await clean_member(
                            reader, name, item.file_size, source.parent, budget, depth
                        )
                unique(label)
                entry = ZipInfo(label, date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 0
                entry.external_attr = 0x10 if item.is_dir() else 0x20
                outgoing.writestr(entry, data, compress_type=ZIP_DEFLATED)
        return dest

    with dest.open("wb") as raw:
        wrapper = (
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
            if suffix(source) in {".tar.gz", ".tgz"} else nullcontext(raw)
        )
        with (
            wrapper as stream, tarfile.open(source, "r:*") as incoming,
            tarfile.open(fileobj=stream, mode="w|") as outgoing,
        ):
            for item in incoming:
                if item.isdir() and item.name.rstrip("/") == ".":
                    continue
                name = archive_path(item.name)
                if not (item.isfile() or item.isdir()):
                    raise JobError("В архіві є посилання або спеціальний файл.")
                if name.parts[0] == "__MACOSX" or name.name in {".DS_Store", "Thumbs.db"}:
                    continue
                if item.isdir():
                    budget.take(0)
                    label, data = str(name), b""
                else:
                    with incoming.extractfile(item) as reader:
                        label, data = await clean_member(
                            reader, name, item.size, source.parent, budget, depth
                        )
                unique(label)
                entry = tarfile.TarInfo(label)
                entry.mtime = entry.uid = entry.gid = 0
                entry.mode = 0o755 if item.isdir() else 0o644
                entry.type = tarfile.DIRTYPE if item.isdir() else tarfile.REGTYPE
                entry.size = len(data)
                outgoing.addfile(entry, BytesIO(data))
    return dest


async def convert_legacy(source, extension):
    profile = source.parent / "office-profile"
    user = profile / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
        '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
        '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
        '</item></oor:items>', encoding="utf-8",
    )
    await run(
        "libreoffice", f"-env:UserInstallation={profile.as_uri()}",
        "--headless", "--convert-to", extension[1:],
        "--outdir", source.parent, source, cwd=source.parent, timeout=150,
    )
    result = source.with_suffix(extension)
    if not result.is_file():
        raise JobError("LibreOffice не перетворив цей документ.")
    return result


async def clean(source, budget=None, depth=0):
    if depth > 4:
        raise JobError("Перевищено глибину вкладеності.")
    budget = budget or Budget()
    extension = suffix(source)
    if extension in REPORT_ONLY:
        raise JobError("Для цього формату доступний звіт.")
    dest = cleaned_name(source)
    if extension in ARCHIVES:
        try:
            return await clean_archive(source, dest, budget, depth)
        except (BadZipFile, tarfile.TarError) as exc:
            raise JobError("Не вдалося прочитати архів.") from exc
    if extension in {".mht", ".mhtml"}:
        return await clean_mhtml(source, dest, budget, depth)
    if extension in {".html", ".htm"}:
        dest.write_bytes(strip_html(source.read_bytes()))
        return dest
    if extension in TEXT:
        shutil.copyfile(source, dest)
        return dest
    if extension in IMAGES:
        shutil.copyfile(source, dest)
        await run("exiftool", "-overwrite_original", "-all=", dest)
        return dest
    if extension in AUDIO_VIDEO:
        mp3 = ["-id3v2_version", "0", "-write_id3v1", "0"] if extension == ".mp3" else []
        await run(
            *FF, "-i", source, "-map", "0:V?", "-map", "0:a?", "-map", "0:s?",
            "-map_metadata", "-1", "-map_metadata:s", "-1",
            "-map_chapters", "-1", "-fflags", "+bitexact", "-c", "copy", *mp3, dest,
        )
        return dest
    if extension in LEGACY:
        converted = await convert_legacy(source, LEGACY[extension])
        return await clean(converted, budget, depth)
    shutil.copyfile(source, dest)
    await run("mat2", "--no-sandbox", "--unknown-members", "abort",
              "--inplace", dest, timeout=150)
    if not dest.is_file() or not dest.stat().st_size:
        raise JobError("Інструмент не повернув очищений документ.")
    return dest
