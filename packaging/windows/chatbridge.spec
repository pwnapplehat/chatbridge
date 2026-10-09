# PyInstaller spec: three programs sharing one runtime folder (chatbridge-gui.exe GUI, chatbridge.exe CLI, chatbridge-sync.exe hidden watcher).
#   pyinstaller --noconfirm --distpath dist-win packaging/windows/chatbridge.spec
from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis, MERGE

root = Path(SPECPATH).resolve().parents[1]
icon = str(root / "chatbridge" / "data" / "chatbridge.ico")
datas = [(str(root / "chatbridge" / "data"), "chatbridge/data")]
excludes = ["gi", "PySide6", "PyQt5", "PyQt6", "numpy", "pytest", "mypy", "ruff"]

specs = [
    ("chatbridge-gui", "entry_gui.py", False),
    ("chatbridge", "entry_cli.py", True),
    ("chatbridge-sync", "entry_sync.py", False),
]
analyses = [
    Analysis([str(Path(SPECPATH) / script)], pathex=[str(root)], datas=datas, hiddenimports=["psutil"], excludes=excludes)
    for _, script, _ in specs
]
MERGE(*[(a, script.removesuffix(".py"), name) for a, (name, script, _) in zip(analyses, specs)])

exes, binaries, zipfiles, datafiles = [], [], [], []
for analysis, (name, _, console) in zip(analyses, specs):
    pyz = PYZ(analysis.pure)
    exes.append(EXE(pyz, analysis.scripts, [], exclude_binaries=True, name=name, console=console, icon=icon, upx=False))
    binaries += analysis.binaries
    zipfiles += analysis.zipfiles
    datafiles += analysis.datas
COLLECT(*exes, binaries, zipfiles, datafiles, name="ChatBridge", upx=False)
