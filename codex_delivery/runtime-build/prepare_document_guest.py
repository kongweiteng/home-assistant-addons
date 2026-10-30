"""Extend the signed minimal guest with fixed offline document/Node assets.

This runs only in a trusted image build. Installing a builder package alone is
insufficient: Python extensions, their shared libraries, fonts and Node's Debian
assets must actually be present in the hashed guest initramfs.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess


root = Path('/guest/root')
assert (root / 'usr/bin/python3').is_file()


def copy_file(source):
    source = Path(source)
    destination = root / source.relative_to('/')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=True)


def copy_tree(source):
    source = Path(source)
    if source.is_dir():
        shutil.copytree(source, root / source.relative_to('/'), dirs_exist_ok=True, symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))


for source in ('/usr/lib/python3/dist-packages', '/usr/share/fonts/opentype/noto',
               '/usr/share/fonts/truetype/dejavu', '/usr/share/fonts/truetype/wqy',
               '/usr/share/nodejs', '/usr/lib/nodejs'):
    copy_tree(source)
copy_file('/usr/bin/node')

# ldd is only run on signed Debian package assets, never candidate binaries.
programs = [Path('/usr/bin/node'), *Path('/usr/lib/python3/dist-packages').rglob('*.so')]
libraries = set()
for executable in programs:
    result = subprocess.run(['ldd', str(executable)], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        match = re.search(r'(/[^ ()]+)', line)
        if match:
            libraries.add(match.group(1))
for library in sorted(libraries):
    copy_file(library)

for name in ('fonts-noto-cjk', 'fonts-wqy-zenhei', 'fonts-dejavu-core', 'python3-pil', 'python3-openpyxl',
             'python3-reportlab', 'nodejs', 'libnode108'):
    copyright_path = Path('/usr/share/doc') / name / 'copyright'
    if copyright_path.is_file():
        copy_file(copyright_path)

packages = subprocess.run(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\n'],
                          check=True, capture_output=True, text=True).stdout
manifest = root / 'opt/runtime/packages.json'
manifest.parent.mkdir(parents=True, exist_ok=True)
manifest.write_text(json.dumps({'version': 1, 'packages': [dict(zip(('name', 'version'), line.split('\t', 1)))
    for line in packages.splitlines()]}, sort_keys=True, separators=(',', ':')))

subprocess.run(['chroot', str(root), '/usr/bin/python3', '-I', '-B', '-c',
    "from PIL import Image,ImageDraw,ImageFont,ImageChops; import openpyxl, reportlab; "
    "im=Image.new('RGB',(600,300),'white'); "
    "font=ImageFont.truetype('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',32); "
    "ImageDraw.Draw(im).text((20,20),'中文金额：42 元',font=font,fill='black'); "
    "box=ImageChops.difference(im,Image.new('RGB',im.size,'white')).getbbox(); "
    "assert box and box[2]-box[0]>180 and box[3]-box[1]>20; "
    "from reportlab.pdfbase import pdfmetrics; from reportlab.pdfbase.ttfonts import TTFont; "
    "pdfmetrics.registerFont(TTFont('WorkCJK','/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',subfontIndex=0)); "
    "from io import BytesIO; from reportlab.pdfgen.canvas import Canvas; "
    "pdf=BytesIO(); c=Canvas(pdf); c.setFont('WorkCJK',16); c.drawString(40,740,'中文金额：42 元'); c.save(); "
    "assert pdf.getvalue().startswith(b'%PDF-'); "
    "book=openpyxl.Workbook(); book.active.append(['净支出',42]); xlsx=BytesIO(); book.save(xlsx); "
    "assert openpyxl.load_workbook(BytesIO(xlsx.getvalue())).active['B1'].value==42"], check=True)
subprocess.run(['chroot', str(root), '/usr/bin/node', '--eval',
                "require('node:assert').equal(20+22,42); require('node:fs');"], check=True)
