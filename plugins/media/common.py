import asyncio
import os
import re
import signal
from contextlib import suppress
from pathlib import Path

WEB_LIMIT = 512_000_000
TELEGRAM_IN = 20_000_000
TELEGRAM_OUT = 50_000_000


class JobError(Exception):
    pass


class URLDenied(JobError):
    pass


def safe_name(value, default="файл"):
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", name).strip(" .")
    if not name:
        return default
    extension = Path(name).suffix
    stem = name[:-len(extension)] if extension else name
    stem = stem[:80].encode("utf-8")[:150].decode("utf-8", "ignore")
    return (stem.strip(" .") or default) + extension[:20]


async def run(*args, cwd=None, timeout=90):
    args = tuple(map(str, args))
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(cwd or "/tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }
    if args[0] == "rclone" and "RCLONE_CONFIG" in os.environ:
        env["RCLONE_CONFIG"] = os.environ["RCLONE_CONFIG"]
    process = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env, start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.CancelledError, TimeoutError) as exc:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise JobError(f"{args[0]} перевищив час обробки.") from None
    if process.returncode:
        lines = stderr.decode("utf-8", "replace").strip().splitlines()
        detail = lines[-1][:250] if lines else ""
        detail = re.sub(r"https?://\S+", "[посилання]", detail)
        raise JobError(f"{args[0]} · код {process.returncode}. {detail}".strip())
    return stdout.decode("utf-8", "replace")
