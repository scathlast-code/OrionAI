"""Оновлює розділ «🧾 Зміни» в docs/31-zhurnal.md з історії git (дата · заголовок злиття). Запуск: python3 tools/update_journal.py"""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
J = ROOT / "docs" / "31-zhurnal.md"
log = subprocess.run(["git", "log", "--date=format:%d.%m.%Y", "--pretty=%ad\t%s", "-n", "400", "origin/main"],
                     cwd=ROOT, capture_output=True, text=True).stdout.strip().splitlines()
out, day = [], None
for line in log:
    d, subj = line.split("\t", 1)
    if d != day:
        out.append(f"\n**{d}**")
        day = d
    out.append(f"- {subj}")
text = J.read_text("utf-8")
text = re.sub(r"<!-- AUTO:CHANGES -->.*<!-- /AUTO:CHANGES -->",
              "<!-- AUTO:CHANGES -->" + "\n".join(out) + "\n<!-- /AUTO:CHANGES -->", text, flags=re.S)
J.write_text(text, "utf-8")
print(f"{len(log)} змін записано")
