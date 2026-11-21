import re
from pathlib import Path


def test_every_hub_button_has_hub():
    src = (Path(__file__).resolve().parents[1] / "bot.py").read_text()
    hubs = src[src.index("\nHUBS = {"):src.index("\nHUB_BUILDING")]
    keys = set(re.findall(r'^    "(\w+)": \("', hubs, re.M))
    for k in set(re.findall(r'"m:hub:(\w+)"', src)):
        assert k in keys, f"кнопка m:hub:{k} без розділу в HUBS"
