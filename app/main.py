#!/usr/bin/env python3
"""N64Recomp Tools — convert Nintendo 64 ROMs to native PC games,
as far as the N64 recomp ecosystem honestly allows.

N64 has no shared runtime + generic scaffold like the other systems:
N64Recomp needs per-game symbol data, and each game needs its own
runtime project. This app probes every ROM, converts the titles with
complete upstream support (today: Majora's Mask (USA), via the
Zelda64Recomp project), and flags the rest needs-review with the
reason - it never pretends a conversion happened.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402

from PySide6.QtCore import Qt, QThread, Signal  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

APP_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = APP_DIR / "settings.json"
STATUS_COL = 5

HELP_TEXT = """\
N64Recomp Tools — turn your own Nintendo 64 cartridges' ROM dumps
into native PC games where the upstream recomp ecosystem makes
that possible today.

WHY THIS TOOL IS DIFFERENT FROM THE OTHER CONVERTERS
  Every other system in the family has a shared runtime that any
  ROM can be scaffolded into. N64 does not. N64Recomp statically
  recompiles a game from its ROM *plus* per-game symbol data —
  function boundaries, overlay tables and RSP microcode layouts
  that come out of decompilation projects (as an ELF or a symbols
  file). There is no automatic way to invent that data for an
  arbitrary ROM, and each supported game needs its own runtime
  project built on N64ModernRuntime. So this tool converts the
  titles with complete upstream support and tells you plainly
  about the rest, instead of producing builds that cannot work.

WHAT CONVERTS TODAY
  * The Legend of Zelda: Majora's Mask (USA) — via the
    Zelda64Recomp project, which ships the symbol data and RSP
    microcode configuration for exactly that ROM. The conversion
    runs the full pipeline here: decompress your ROM, run
    N64Recomp and RSPRecomp over it (17k+ functions), build the
    runtime, package "<Game>-Linux.zip".
  Everything else probes fine (header, byte order, hashes) and is
  flagged "needs review" with the reason: no symbol data or
  runtime project exists for it upstream yet. If upstream ships
  support for more titles, this tool's supported list grows.

THE GAME ITSELF — SUPPLY YOUR OWN ROM
  The package contains the executable, its assets folder and a
  README. No ROM and no firmware is included, and none is needed
  beyond the ROM: the PIF/boot is handled in HLE by the runtime.
  On launch the game's own launcher asks you to choose your ROM —
  give it the standard retail Majora's Mask (USA) dump (NOT a
  decompressed one; decompression is only an internal build step
  here). It verifies the ROM before playing. Keep the assets/
  folder next to the executable.

WINDOWS
  Linux packages only. Upstream builds Windows with Microsoft
  Visual Studio (MSVC) exclusively — there is no MinGW/cross
  build to produce from here, so this tool does not offer a
  Windows target rather than shipping something untested.

NETPLAY
  There is none, anywhere in this ecosystem: N64Recomp,
  N64ModernRuntime and Zelda64Recomp have no netplay support.
  Nothing in this tool claims otherwise.

RETROACHIEVEMENTS
  Not integrated, and the reason is architectural: there is no
  native frame-callback / memory-peek surface in librecomp that
  a module could attach to (its mod API runs recompiled game
  code, not native add-ons), and RetroAchievements' N64 support
  would additionally need per-game memory maps. Rather than a
  toggle that does nothing, the feature is absent and documented
  here. (For reference, rcheevos' console ID for N64 is 2.)

COVER ART
  Box art is fetched automatically from the libretro-thumbnails
  project (Nintendo - Nintendo 64 set, mirrors + name variants +
  a listing match for oddly-named files) and included in each
  package under artwork/.

PROBE NOTES
  The probe reads the ROM header (internal name, cartridge code,
  region byte, entry point, header CRCs), identifies the byte
  order (.z64 native, .v64/.n64 byte-swapped — the extension can
  lie; the magic bytes don't), and hashes the normalized image.
  Save types are deliberately NOT shown: the N64 header does not
  record them (the game chooses at runtime; the runtime
  implements EEPROM, SRAM and FlashRAM saves).

CREDITS
  * N64Recomp by Wiseguy (Mr-Wiseguy) and contributors —
    MIT License, (c) 2024 Wiseguy.
    github.com/N64Recomp/N64Recomp
  * N64ModernRuntime (librecomp + ultramodern) by Wiseguy and
    contributors — GPL-3.0. github.com/N64Recomp/N64ModernRuntime
  * Zelda64Recomp by the Zelda64Recomp contributors — GPL-3.0.
    github.com/Zelda64Recomp/Zelda64Recomp
  * RT64 renderer by the RT64 contributors — MIT License,
    (c) 2024 RT64 Contributors. github.com/rt64/rt64
  * z64decompress by z64me and contributors — GPL-3.0.
    github.com/z64tools/z64decompress
  Cover art: libretro-thumbnails project.
"""


class Worker(QThread):
    log = Signal(str)
    status = Signal(int, str)
    finished_all = Signal()

    def __init__(self, items):
        super().__init__()
        self.items = items          # list of (row, job-dict)

    def run(self):
        for row, job in self.items:
            self.status.emit(row, "converting…")
            try:
                result = engine.run_job(job, log=self.log.emit)
                if result.get("status") == "done":
                    self.status.emit(row, "done")
                else:
                    self.log.emit(
                        f"FAILED {job.get('title')}: "
                        f"{result.get('error', 'unknown error')}")
                    self.status.emit(row, "failed")
            except Exception as e:  # noqa: BLE001
                self.log.emit(f"FAILED {job.get('title')}: {e}")
                self.status.emit(row, "failed")
        self.finished_all.emit()


class CoverWorker(QThread):
    done = Signal(str, object)

    def __init__(self, title):
        super().__init__()
        self.title = title

    def run(self):
        try:
            data = engine.fetch_cover(self.title)
        except Exception:  # noqa: BLE001
            data = None
        self.done.emit(self.title, data)


class InstallWorker(QThread):
    done = Signal(bool)

    def __init__(self, packages):
        super().__init__()
        self.packages = packages

    def run(self):
        try:
            subprocess.run(["pkexec", "apt-get", "update"], check=False)
            r = subprocess.run(["pkexec", "apt-get", "install", "-y"]
                               + self.packages, check=False)
            self.done.emit(r.returncode == 0)
        except Exception:  # noqa: BLE001
            self.done.emit(False)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("N64Recomp Tools")
        self.resize(1180, 760)
        self.items = []             # list of probe dicts
        self.worker = None
        self._cover_workers = set()
        self._cover_wanted = None
        self._build_ui()
        self._load_settings()
        self.refresh_toolbox()

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        tabs = QTabWidget()
        tabs.addTab(self._convert_tab(), "Convert")
        help_edit = QPlainTextEdit(HELP_TEXT)
        help_edit.setReadOnly(True)
        tabs.addTab(help_edit, "Help")
        self.setCentralWidget(tabs)

    def _convert_tab(self):
        root = QWidget()
        v = QVBoxLayout(root)
        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Title", "Format", "Size", "Region", "SHA-1", "Status"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemSelectionChanged.connect(self.show_selected)
        lv.addWidget(self.table)
        btns = QHBoxLayout()
        for label, fn in [("Add ROMs…", self.add_roms),
                          ("Add Folder…", self.add_folder),
                          ("Remove", self.remove_selected),
                          ("Clear", self.clear_queue)]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            btns.addWidget(b)
        lv.addLayout(btns)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addWidget(self._probe_box())
        rv.addWidget(self._settings_box())
        rv.addStretch(1)
        split.addWidget(right)
        split.setSizes([640, 480])
        v.addWidget(split, 3)

        run_row = QHBoxLayout()
        self.toolbox_label = QLabel("Toolbox: …")
        run_row.addWidget(self.toolbox_label)
        self.install_btn = QPushButton("Install missing packages")
        self.install_btn.clicked.connect(self.install_packages)
        run_row.addWidget(self.install_btn)
        run_row.addStretch(1)
        self.run_btn = QPushButton("Run Queue")
        self.run_btn.clicked.connect(self.run_queue)
        run_row.addWidget(self.run_btn)
        v.addLayout(run_row)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        v.addWidget(self.log_edit, 2)
        root._split = split
        return root

    def _probe_box(self):
        box = QGroupBox("ROM details")
        form = QFormLayout(box)
        self.p_title = QLabel("—")
        self.p_cart = QLabel("—")
        self.p_entry = QLabel("—")
        self.p_crc = QLabel("—")
        self.p_support = QLabel("—")
        self.p_support.setWordWrap(True)
        self.p_cover = QLabel("")
        self.p_cover.setAlignment(Qt.AlignCenter)
        form.addRow("Internal name:", self.p_title)
        form.addRow("Cartridge code:", self.p_cart)
        form.addRow("Entry point:", self.p_entry)
        form.addRow("CRC32 / SHA-1:", self.p_crc)
        form.addRow("Conversion:", self.p_support)
        form.addRow(self.p_cover)
        return box

    def _settings_box(self):
        box = QGroupBox("Settings (apply to the whole queue)")
        form = QFormLayout(box)
        tgt_row = QHBoxLayout()
        self.s_linux = QCheckBox("Linux")
        self.s_linux.setChecked(True)
        self.s_linux.setEnabled(False)
        self.s_windows = QCheckBox("Windows (MSVC-only upstream — "
                                   "not producible here)")
        self.s_windows.setChecked(False)
        self.s_windows.setEnabled(False)
        tgt_row.addWidget(self.s_linux)
        tgt_row.addWidget(self.s_windows)
        form.addRow("Build targets:", tgt_row)
        self.s_jobs = QSpinBox()
        self.s_jobs.setRange(1, 16)
        self.s_jobs.setValue(2)
        form.addRow("Build jobs:", self.s_jobs)
        out_row = QHBoxLayout()
        self.s_out = QLineEdit(str(APP_DIR / "output"))
        pick_o = QPushButton("Pick…")
        pick_o.clicked.connect(lambda: self._pick_dir(self.s_out))
        out_row.addWidget(self.s_out)
        out_row.addWidget(pick_o)
        form.addRow("Output folder:", out_row)
        return box

    # ------------------------------------------------------------ util
    def _pick_dir(self, edit):
        p = QFileDialog.getExistingDirectory(self, "Pick a folder")
        if p:
            edit.setText(p)

    def log_line(self, msg):
        self.log_edit.appendPlainText(str(msg))

    def refresh_toolbox(self):
        try:
            checks = engine.toolbox()
            missing = sorted({c["apt"] for c in checks
                              if not c["found"] and c.get("apt")})
            self._missing = missing
            bad = [c["tool"] for c in checks if not c["found"]]
            if not bad:
                self.toolbox_label.setText("Toolbox: ready")
                self.install_btn.setEnabled(False)
            else:
                self.toolbox_label.setText(
                    "Toolbox: missing " + ", ".join(bad))
                self.install_btn.setEnabled(bool(missing))
        except Exception as e:  # noqa: BLE001
            self.toolbox_label.setText(f"Toolbox: check failed ({e})")
            self._missing = []

    def install_packages(self):
        if not getattr(self, "_missing", None):
            return
        self.install_btn.setEnabled(False)
        self.log_line("Installing: " + ", ".join(self._missing))
        self._inst = InstallWorker(self._missing)
        self._inst.done.connect(self._install_done)
        self._inst.start()

    def _install_done(self, ok):
        self.log_line("Package install " + ("done." if ok else "FAILED."))
        self.refresh_toolbox()

    # ------------------------------------------------------------ queue
    def _add_paths(self, paths):
        for p in paths:
            try:
                probe = engine.probe_rom(str(p))
            except Exception as e:  # noqa: BLE001
                self.log_line(f"skip {p}: {e}")
                continue
            self.items.append(probe)
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._fill_row(row, probe,
                           "ready" if probe.get("supported")
                           else "needs review")
        self.table.resizeColumnsToContents()

    def _fill_row(self, row, probe, status):
        vals = [probe["title"], probe.get("format", "?"),
                f"{probe.get('size_mb', '?')} MB",
                probe.get("region") or "—",
                (probe.get("sha1") or "—")[:12] + "…", status]
        for c, val in enumerate(vals):
            self.table.setItem(row, c, QTableWidgetItem(str(val)))

    def _set_status(self, row, status):
        self.table.setItem(row, STATUS_COL, QTableWidgetItem(status))

    def add_roms(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add Nintendo 64 ROMs", "",
            "Nintendo 64 ROMs (*.z64 *.n64 *.v64 *.bin)")
        self._add_paths(paths)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add a folder of ROMs")
        if d:
            paths = []
            for pat in ("*.z64", "*.n64", "*.v64", "*.bin",
                        "*.Z64", "*.N64", "*.V64"):
                paths += sorted(Path(d).rglob(pat))
            self._add_paths(paths)

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.table.removeRow(r)
            del self.items[r]

    def clear_queue(self):
        self.table.setRowCount(0)
        self.items.clear()

    def show_selected(self):
        rows = self.table.selectedIndexes()
        if not rows:
            return
        row = rows[0].row()
        if row >= len(self.items):
            return
        probe = self.items[row]
        self.p_title.setText(probe.get("internal_name") or probe["title"])
        self.p_cart.setText(probe.get("cart_id") or "—")
        self.p_entry.setText(probe.get("entry") or "—")
        self.p_crc.setText(
            f"{probe.get('crc32_image', '—')} / "
            f"{(probe.get('sha1') or '—')[:16]}…")
        if probe.get("supported"):
            self.p_support.setText(
                "Supported — converts with the Zelda64Recomp "
                "pipeline (Majora's Mask (USA) symbol data).")
        else:
            self.p_support.setText(
                "Needs review: " +
                "; ".join(probe.get("review_reasons", []) or
                          ["not convertible"]))
        self.p_cover.setText("fetching art…")
        title = probe["title"]
        self._cover_wanted = title
        worker = CoverWorker(title)
        self._cover_workers.add(worker)
        worker.done.connect(self._cover_done)
        worker.finished.connect(
            lambda w=worker: self._cover_workers.discard(w))
        worker.start()

    def _cover_done(self, title, data):
        if title != self._cover_wanted:
            return
        if data:
            pix = QPixmap()
            if pix.loadFromData(data):
                self.p_cover.setPixmap(pix.scaled(
                    160, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return
        self.p_cover.setText("(no art found)")

    # ------------------------------------------------------------ run
    def current_settings(self):
        return {
            "output_dir": self.s_out.text().strip(),
            "targets": ["linux"],
            "jobs": self.s_jobs.value(),
        }

    def run_queue(self):
        if self.worker and self.worker.isRunning():
            return
        if not self.items:
            QMessageBox.information(self, "N64Recomp Tools",
                                    "Add at least one ROM first.")
            return
        s = self.current_settings()
        self._save_settings(s)
        jobs = []
        for row, probe in enumerate(self.items):
            job = dict(s)
            job["rom"] = probe["path"]
            job["title"] = probe["title"]
            jobs.append((row, job))
        self.run_btn.setEnabled(False)
        self.worker = Worker(jobs)
        self.worker.log.connect(self.log_line)
        self.worker.status.connect(self._set_status)
        self.worker.finished_all.connect(self._run_done)
        self.worker.start()

    def _run_done(self):
        self.run_btn.setEnabled(True)
        self.log_line("Queue finished.")

    # ------------------------------------------------------------ settings
    def _load_settings(self):
        try:
            s = json.loads(SETTINGS_FILE.read_text())
        except Exception:  # noqa: BLE001
            return
        self.s_jobs.setValue(int(s.get("jobs", 2)))
        if s.get("output_dir"):
            self.s_out.setText(s["output_dir"])

    def _save_settings(self, s=None):
        try:
            SETTINGS_FILE.write_text(
                json.dumps(s or self.current_settings(), indent=2))
        except Exception:  # noqa: BLE001
            pass


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
