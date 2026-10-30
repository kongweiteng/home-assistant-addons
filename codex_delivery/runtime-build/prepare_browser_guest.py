"""Copy signed Debian Chromium and its dependencies into the isolated guest."""
import json
from pathlib import Path
import re
import shutil
import subprocess

root = Path('/guest/root')
assert (root/'usr/bin/node').is_file()
# The browser is a separate runtime, not a document guest with extra rights.
# Keep its standard-library supervisor and one fixed CJK/Latin font set.
shutil.rmtree(root/'usr/lib/python3/dist-packages', ignore_errors=True)
shutil.rmtree(root/'usr/share/fonts', ignore_errors=True)
for font in ('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
             '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
             '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'):
    path = Path(font); destination = root/path.relative_to('/')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
source = Path('/usr/lib/chromium')
assert (source/'chromium').is_file()
shutil.copytree(source, root/'usr/lib/chromium', dirs_exist_ok=True, symlinks=False)
# Browser startup uses the fixed ELF directly; no user shell/profile or desktop
# wrapper configuration is imported. Namespace sandbox stays enabled.
libraries = set()
for program in source.rglob('*'):
    if not program.is_file(): continue
    with program.open('rb') as item:
        if item.read(4) != b'\x7fELF': continue
    result = subprocess.run(['ldd',str(program)],capture_output=True,text=True)
    if result.returncode and 'not a dynamic executable' not in result.stderr:
        raise RuntimeError('browser_dependency_scan')
    for line in result.stdout.splitlines():
        match = re.search(r'(/[^ ()]+)',line)
        if match: libraries.add(Path(match.group(1)))
for library in sorted(libraries):
    destination = root/library.relative_to('/')
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(library,destination,follow_symlinks=True)
for directory in ('/usr/share/chromium','/etc/fonts'):
    source = Path(directory)
    if source.is_dir():
        shutil.copytree(source,root/source.relative_to('/'),dirs_exist_ok=True,symlinks=False)
packages = subprocess.run(['dpkg-query','-W','-f=${binary:Package}\t${Version}\n'],
    check=True,capture_output=True,text=True).stdout
(root/'opt/runtime/browser-packages.json').write_text(json.dumps({'version':1,'packages':[
    dict(zip(('name','version'),line.split('\t',1))) for line in packages.splitlines()]},sort_keys=True,separators=(',',':')))
subprocess.run(['chroot',str(root),'/usr/lib/chromium/chromium','--version'],check=True)
