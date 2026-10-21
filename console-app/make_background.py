"""Makes sce_sys/pic0.dds, the picture behind the PS5 app's tile when it's selected on the home screen, and
pic1.dds, the same picture while the app starts (the kit wants both), from
images/spela_bg_upscayl_3x_digital-art-4x.png. The console wants a 3840x2160 BC7 DDS without mipmaps; Pillow
can't write BC7, so texconv (winget install Microsoft.DirectXTex.Texconv) does that part. Run it after
changing the picture, then build:

    backend\\.venv\\Scripts\\python console-app/make_background.py
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

here = Path(__file__).parent
src = here.parent / "images" / "spela_bg_upscayl_3x_digital-art-4x.png"
img = ImageOps.fit(Image.open(src).convert("RGB"), (3840, 2160), Image.LANCZOS)
with tempfile.TemporaryDirectory() as tmp:
    png = Path(tmp) / "pic0.png"
    img.save(png)
    subprocess.run(["texconv", "-nologo", "-y", "-m", "1", "-f", "BC7_UNORM", "-dx10", "-o", str(here / "sce_sys"),
                    str(png)], check=True, stdout=subprocess.DEVNULL)
shutil.copyfile(here / "sce_sys" / "pic0.dds", here / "sce_sys" / "pic1.dds")
print("wrote sce_sys/pic0.dds and pic1.dds")
