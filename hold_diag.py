"""Log every keyboard event to hold_diag.log — run from terminal, hold ctrl+space, release."""

import time
from pathlib import Path

import keyboard

LOG = Path(__file__).parent / "hold_diag.log"


def w(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="", flush=True)


LOG.write_text("", encoding="utf-8")
w(f"hotkey test start, keyboard ok")

held = set()


def norm(name: str) -> str:
    n = (name or "").lower()
    if n.startswith("ctrl"):
        return "ctrl"
    if n.startswith("alt"):
        return "alt"
    if n.startswith("shift"):
        return "shift"
    return n


def on_event(e):
    k = norm(e.name)
    if e.event_type == "down":
        held.add(k)
    else:
        held.discard(k)
    w(f"{e.event_type:4} name={e.name!r:12} norm={k!r:8} held={sorted(held)}")


keyboard.hook(on_event)
w("hooks registered — HOLD and RELEASE ctrl+space for 12s")
t0 = time.time()
while time.time() - t0 < 12:
    time.sleep(0.1)
w("done — check log")
keyboard.unhook_all()
