from pathlib import Path
import re

root = Path(r"C:/Users/vidri/Downloads/say-my-name")
p = root / "app/asr_ui.py"
t = p.read_text(encoding="utf-8")

# main window taller
t = t.replace('self.geometry("480x640")\n        self.minsize(480, 640)\n        self.maxsize(480, 640)',
              'self.geometry("480x720")\n        self.minsize(480, 720)\n        self.maxsize(480, 720)', 1)

# settings window taller
t = t.replace('self.geometry("440x640")\n        self.minsize(440, 640)\n        self.maxsize(440, 640)',
              'self.geometry("440x740")\n        self.minsize(440, 740)\n        self.maxsize(440, 740)', 1)

# make main log area shrinkable so monitor expansion can scroll
# (CTkTextbox already scrolls; ensure monitor + log share space)
t = t.replace(
    'wrap = ctk.CTkFrame(self, fg_color="transparent")\n        wrap.pack(fill="both", expand=True, padx=28, pady=(16, 8))',
    'wrap = ctk.CTkFrame(self, fg_color="transparent")\n        wrap.pack(fill="both", expand=True, padx=28, pady=(8, 8))',
    1,
)

p.write_text(t, encoding="utf-8")
import ast
ast.parse(t)
print("windows resized")
for line in t.splitlines():
    if 'geometry("4' in line or 'minsize(4' in line:
        print(line.strip())
