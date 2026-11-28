"""🩺 Сторож циклу подій: якщо бот «завис» (щось важке виконується просто в головному циклі й кнопки не відповідають),
записує в журнал, скільки секунд і ЩО саме його тримало (стек головного потоку). Так затримки знаходяться, а не вгадуються."""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger("orion.loopwatch")
STATE = {"beat": time.monotonic(), "worst": 0.0, "stalls": 0}


async def heartbeat(period=0.25):
    while True:
        STATE["beat"] = time.monotonic()
        await asyncio.sleep(period)


def _watch(main_ident, limit):
    reported = False
    while True:
        time.sleep(0.5)
        lag = time.monotonic() - STATE["beat"]
        if lag > limit and not reported:
            reported = True
            STATE["stalls"] += 1
            frame = sys._current_frames().get(main_ident)
            stack = "".join(traceback.format_stack(frame, limit=12)) if frame else "?"
            log.warning("🐢 бот завис на %.1f с — кнопки не відповідали. Що тримало:\n%s", lag, stack)
        elif lag <= limit and reported:
            reported = False
        STATE["worst"] = max(STATE["worst"], lag)


def start(limit=2.0):
    """Викликати зсередини циклу подій."""
    asyncio.get_running_loop().create_task(heartbeat())
    t = threading.Thread(target=_watch, args=(threading.get_ident(), limit), name="loopwatch", daemon=True)
    t.start()
    return t
