#!/usr/bin/env python3
"""N64Recomp Tools - engine (UI-free core + CLI).

The N64 member of the recomp converter family, and the family's honest
exception. Every other system has a shared runtime + launcher that any
ROM can be scaffolded into. N64 does not: N64Recomp (by Wiseguy and
contributors) statically recompiles a game from a ROM *plus per-game
symbol data* (an ELF from a decompilation project, or an equivalent
symbols file), and each game needs its own runtime project built on
N64ModernRuntime (librecomp + ultramodern + RT64). There is no generic
scaffold upstream and no way to invent symbol data for an arbitrary
ROM - function boundaries, overlay tables and RSP microcode configs
are per-game reverse-engineering results.

What this tool therefore does, truthfully:
  * Probes any N64 ROM (header, byte order, hashes) and classifies it.
  * Converts the titles that have complete upstream support today -
    currently exactly one in the wild: The Legend of Zelda: Majora's
    Mask (USA), via the Zelda64Recomp project (bundled symbols file +
    RSP microcode configs). The conversion runs the real pipeline:
    decompress the ROM, run N64Recomp + RSPRecomp, build the runtime
    with CMake/Clang, package the result.
  * Flags everything else needs-review with the specific reason, so
    the queue never pretends a conversion happened.

No RetroAchievements and no netplay: the runtime ecosystem has no
netplay at all, and no native frame/peek hook surface a family-style
RA module could attach to (librecomp's mod API runs recompiled game
code, not native add-ons). Both gaps are documented in the Help tab
rather than faked. No BIOS/PIF file is needed - ultramodern
reimplements the PIF in HLE. Windows packages are not produced:
upstream builds Windows with MSVC only; this tool produces the Linux
build.

CLI:
    python3 engine.py toolbox
    python3 engine.py probe --rom <file.z64>
    python3 engine.py run --job <job.json>
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
FRAMEWORK_CACHE = APP_DIR / "framework-cache"
ART_CACHE = APP_DIR / "art-cache"

ZELDA_URL = "https://github.com/Zelda64Recomp/Zelda64Recomp.git"
ZELDA_PIN = "b65c482ed672258eb684ab94a4e8c8aa662615ee"
Z64DECOMPRESS_URL = "https://github.com/z64tools/z64decompress.git"
Z64DECOMPRESS_PIN = "e2b3707271994a2a1b3afc6c3997a7cf6b479765"

# Titles with complete upstream support. Keyed by the SHA-1 of the
# byte-order-normalized retail ROM image. Majora's Mask (USA): the
# Zelda64Recomp project ships the symbols file, overlay list and RSP
# configs for exactly this ROM (its BUILDING.md names this SHA-1).
SUPPORTED = {
    "d6133ace5afaa0882cf214cf88daba39e266c078": {
        "game": "The Legend of Zelda: Majora's Mask (USA)",
        "boxart": "Legend of Zelda, The - Majora's Mask (USA)",
        "project": "zelda64",
        "config": "us.rev1.toml",
        "rsp_configs": ["aspMain.us.rev1.toml", "njpgdspMain.us.rev1.toml"],
        "uncompressed_name": "mm.us.rev1.rom_uncompressed.z64",
        "exe": "Zelda64Recompiled",
    },
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log_line(log, msg):
    if log is not None:
        log(msg)


def _run(cmd, log, cwd=None, timeout=7200, env=None):
    """Run a command, streaming lines to log. Returns exit code."""
    log_line(log, "+ " + " ".join(str(c) for c in cmd))
    proc = subprocess.Popen(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", env=env)
    try:
        for line in proc.stdout:
            log_line(log, line.rstrip("\n"))
    except Exception:
        proc.kill()
        raise
    proc.wait(timeout=timeout)
    return proc.returncode


def _cmake_env():
    env = dict(os.environ)
    # The converter-family lesson: never let a stray tools dir shadow
    # the system SDL2/CMake packages during configure.
    env["CC"] = "clang"
    env["CXX"] = "clang++"
    return env


def _cmd_ver(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=20)
        first = (out.stdout or out.stderr).strip().splitlines()
        return first[0].strip() if first else "found"
    except Exception:
        return None


def _pkg_ver(pkg):
    try:
        out = subprocess.run(["pkg-config", "--modversion", pkg],
                             capture_output=True, text=True, timeout=20)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Toolbox
# ---------------------------------------------------------------------------

APT_LINE = ("sudo apt install -y git cmake ninja-build clang lld "
            "libsdl2-dev libgtk-3-dev libvulkan-dev glslang-tools "
            "python3-pip")


def toolbox():
    """[{tool, found, needed, apt}] rows; apt is the Debian package
    that provides the tool (None when not apt-managed)."""
    checks = []
    v = _cmd_ver(["git", "--version"])
    checks.append({"tool": "git", "found": v, "apt": "git",
                   "needed": "cloning the frameworks"})
    v = _cmd_ver(["cmake", "--version"])
    checks.append({"tool": "cmake", "apt": "cmake",
                   "found": v and v.replace("cmake version ", ""),
                   "needed": "building N64Recomp + the runtime"})
    v = _cmd_ver(["ninja", "--version"])
    checks.append({"tool": "ninja", "found": v, "apt": "ninja-build",
                   "needed": "build driver"})
    v = _cmd_ver(["clang", "--version"])
    checks.append({"tool": "clang", "found": v, "apt": "clang",
                   "needed": "the runtime is developed/tested with Clang"})
    v = _cmd_ver(["ld.lld", "--version"])
    checks.append({"tool": "lld", "found": v, "apt": "lld",
                   "needed": "linker used by the runtime build"})
    v = _pkg_ver("sdl2")
    checks.append({"tool": "SDL2 (pkg-config sdl2)", "found": v,
                   "apt": "libsdl2-dev",
                   "needed": "windowing/input/audio for the runtime"})
    v = _pkg_ver("gtk+-3.0")
    checks.append({"tool": "GTK 3 (pkg-config gtk+-3.0)", "found": v,
                   "apt": "libgtk-3-dev",
                   "needed": "native file dialogs in the runtime"})
    checks.append({"tool": "Vulkan headers", "apt": "libvulkan-dev",
                   "found": os.path.exists("/usr/include/vulkan/vulkan.h"),
                   "needed": "RT64 graphics backend"})
    v = _cmd_ver(["glslangValidator", "--version"])
    checks.append({"tool": "glslangValidator", "found": v,
                   "apt": "glslang-tools",
                   "needed": "RT64 shader compilation"})
    try:
        import PIL  # noqa: F401
        pil = True
    except Exception:
        pil = False
    checks.append({"tool": "Pillow (cover thumbnails)", "found": pil,
                   "apt": None, "needed": "GUI cover previews"})
    return checks


# ---------------------------------------------------------------------------
# ROM probe
# ---------------------------------------------------------------------------

MAGICS = {
    b"\x80\x37\x12\x40": "z64",
    b"\x37\x80\x40\x12": "v64",
    b"\x40\x12\x37\x80": "n64",
}

REGION_BYTES = {
    0x00: "Japan", 0x01: "North America", 0x02: "Europe",
    0x44: "Germany", 0x45: "North America", 0x46: "France",
    0x49: "Italy", 0x4A: "Japan", 0x50: "Europe", 0x53: "Spain",
    0x55: "Australia", 0x59: "Scandinavia",
}


def normalize_image(raw, fmt):
    """Byte-order-normalize a ROM image to .z64 (big-endian) order."""
    if fmt == "z64":
        return raw
    if fmt == "v64":  # swap bytes within each 16-bit pair
        out = bytearray(len(raw))
        out[0::2] = raw[1::2]
        out[1::2] = raw[0::2]
        return bytes(out)
    out = bytearray(raw)  # n64: reverse each 32-bit word
    for i in range(0, len(raw) - 3, 4):
        out[i:i + 4] = raw[i:i + 4][::-1]
    return bytes(out)


def title_from_filename(name):
    stem = re.sub(r"\.(z64|n64|v64|bin)$", "", name, flags=re.I)
    return stem.strip()


def probe_rom(path):
    """Parse an N64 ROM image. Save types are NOT read: they are not
    recorded anywhere in the N64 header (the game decides at runtime;
    librecomp implements EEPROM/SRAM/FlashRAM), so this probe does not
    claim one."""
    p = Path(path)
    result = {"path": str(p), "ok": False, "title": title_from_filename(p.name)}
    try:
        raw = p.read_bytes()
    except OSError as e:
        result["error"] = f"cannot read file: {e}"
        result["needs_review"] = True
        return result
    result["size"] = len(raw)
    result["size_mb"] = round(len(raw) / (1024 * 1024), 1)
    fmt = MAGICS.get(raw[:4])
    reasons = []
    if fmt is None:
        reasons.append(
            "not a standard N64 ROM image (unrecognized magic bytes) - "
            "cheat-device firmware dumps and other non-ROM files cannot "
            "be converted")
        result.update({"format": "unknown", "needs_review": True,
                       "review_reasons": reasons, "supported": False})
        return result
    data = normalize_image(raw, fmt)
    result["format"] = fmt
    result["sha1"] = hashlib.sha1(data).hexdigest()
    result["crc32_image"] = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
    result["internal_name"] = data[0x20:0x34].decode(
        "ascii", errors="replace").rstrip()
    result["entry"] = f"0x{int.from_bytes(data[0x08:0x0C], 'big'):08x}"
    result["crc1"] = f"{int.from_bytes(data[0x10:0x14], 'big'):08x}"
    result["crc2"] = f"{int.from_bytes(data[0x14:0x18], 'big'):08x}"
    result["cart_id"] = data[0x3B:0x3E].decode("ascii", errors="replace")
    region_byte = data[0x3E]
    result["region"] = REGION_BYTES.get(
        region_byte, f"unknown (0x{region_byte:02x})")
    if fmt != "z64":
        reasons.append(
            f"byte-swapped {fmt} container - the tool normalizes it to "
            f".z64 order before hashing and building")
    sup = SUPPORTED.get(result["sha1"])
    result["supported"] = sup is not None
    if sup is not None:
        result["support"] = sup
    else:
        reasons.append(
            "no conversion support yet: N64Recomp needs per-game symbol "
            "data (decomp ELF or symbols file) plus a per-game runtime "
            "project, and upstream only ships those for a handful of "
            "titles - none exists for this one")
    result["needs_review"] = not result["supported"]
    result["review_reasons"] = reasons
    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# Cover art (libretro-thumbnails Nintendo_-_Nintendo_64) - family policy:
# GitHub mirror first, canonical host second, retries on network errors,
# no retry on 404, No-Intro name variants, then a fuzzy match against
# the set's real file listing (same logic as the Recomp Covers tool).
# ---------------------------------------------------------------------------

COVER_SYSTEM = "Nintendo_-_Nintendo_64"
COVER_HOSTS = [
    "https://raw.githubusercontent.com/libretro-thumbnails/"
    "Nintendo_-_Nintendo_64/master/Named_Boxarts",
    "https://thumbnails.libretro.com/Nintendo%20-%20Nintendo%2064"
    "/Named_Boxarts",
]
_ILLEGAL = dict.fromkeys(map(ord, '\\/:*?"<>|'), None)
LISTING_TTL = 7 * 24 * 3600
_REGION_WORDS = {
    "usa", "europe", "japan", "world", "australia", "brazil", "korea",
    "canada", "france", "germany", "spain", "italy", "netherlands",
    "sweden", "denmark", "finland", "norway", "asia", "taiwan",
    "china", "russia", "mexico", "portugal", "greece", "india",
    "israel", "uk", "scandinavia", "latin america", "new zealand",
}


def _lr_encode(name):
    return name.translate(_ILLEGAL).replace("&", "_")


def _cover_names(title):
    base = title.strip()
    names = [base]
    no_brackets = re.sub(r"\s*\[[^\]]*\]", "", base).strip()
    if no_brackets != base:
        names.append(no_brackets)
    norm = re.sub(r"\((JUE|JU|UE|U|J)\)", "(World)", no_brackets)
    norm = re.sub(r"\(V(\d+)\.(\d+)\)", r"(Rev \2)", norm)
    if norm != no_brackets:
        names.append(norm)
    core = base.split(" (")[0].strip()
    if core and core not in names:
        names.append(core)
    for suffix in (" (USA)", " (USA, Europe)", " (World)"):
        cand = core + suffix
        if cand not in names:
            names.append(cand)
    return names


def _valid_cover(raw):
    return bool(raw) and len(raw) >= 512 and \
        raw[:8] == b"\x89PNG\r\n\x1a\n"


def _get_once(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent":
                                                "N64RecompTools/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, None


def _get_retry(url, attempts=3, timeout=25):
    for i in range(attempts):
        data, status = _get_once(url, timeout=timeout)
        if data is not None:
            return data
        if status == 404:
            return None
        if i + 1 < attempts:
            time.sleep(2 * (i + 1))
    return None


def _set_listing(log=None):
    dest = ART_CACHE / "n64-listing.json"
    stale = []
    try:
        blob = json.loads(dest.read_text())
        stale = blob.get("names", [])
        if time.time() - blob.get("ts", 0) < LISTING_TTL and stale:
            return stale
    except Exception:
        pass
    if not shutil.which("git"):
        return stale
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="n64-list-"))
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1",
             "--filter=tree:0", "--no-checkout",
             "https://github.com/libretro-thumbnails/" + COVER_SYSTEM,
             str(tmp / "repo")],
            check=True, capture_output=True, timeout=300)
        out = subprocess.run(
            ["git", "-C", str(tmp / "repo"), "ls-tree", "-r",
             "--name-only", "HEAD"],
            check=True, capture_output=True, timeout=300).stdout
        names = [ln[len("Named_Boxarts/"):-len(".png")]
                 for ln in out.decode(errors="replace").splitlines()
                 if ln.startswith("Named_Boxarts/")
                 and ln.endswith(".png")]
        if names:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps({"ts": time.time(),
                                        "names": names}))
            return names
    except Exception as e:
        log_line(log, f"cover: set listing failed ({e})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return stale


def _core(name):
    return _lr_encode(name).split(" (")[0].strip().lower()


def _regions(name):
    found = set()
    for group in re.findall(r"\(([^)]*)\)", name):
        for tok in re.split(r"[,+]", group):
            tok = tok.strip().lower()
            if tok in _REGION_WORDS:
                found.add(tok)
    return found


def _fuzzy_candidates(title, log=None):
    core, want = _core(title), _regions(title)
    scored, partial = [], []
    for n in _set_listing(log):
        if _core(n) != core:
            continue
        regs = _regions(n)
        score = 100 - 4 * len(regs - want) - len(n)
        low = n.lower()
        if any(t in low for t in ("(proto)", "(beta)", "[hack",
                                  "(sample)", "(demo)", "(unl)")):
            score -= 25
        if want and not want.issubset(regs):
            partial.append((score + 12 * len(want & regs), n))
            continue
        scored.append((score, n))
    pool = scored or partial
    pool.sort(key=lambda t: -t[0])
    return [n for _, n in pool[:6]]


def fetch_cover(title, log=None):
    """Box art PNG bytes for a title, or None. Cached in art-cache."""
    ART_CACHE.mkdir(parents=True, exist_ok=True)
    cache = ART_CACHE / (_lr_encode(title).replace(" ", "_") + ".png")
    if cache.exists() and _valid_cover(cache.read_bytes()):
        return cache.read_bytes()

    def attempt(name):
        for base in COVER_HOSTS:
            url = (base + "/" + urllib.parse.quote(
                _lr_encode(name) + ".png", safe=""))
            data = _get_retry(url)
            if data and _valid_cover(data):
                try:
                    cache.write_bytes(data)
                except OSError:
                    pass
                log_line(log, f"cover: fetched {name}.png "
                              f"({len(data)} bytes)")
                return data
        return None

    for name in _cover_names(title):
        data = attempt(name)
        if data:
            return data
    for cand in _fuzzy_candidates(title, log):
        data = attempt(cand)
        if data:
            log_line(log, f"cover: listing match {cand}.png")
            return data
    log_line(log, f"cover: no box art found for {title}")
    return None


def _png_to_tga(png_bytes, dest):
    """Some family launchers stage TGA box art. N64 packages use
    front.png only; kept for parity with the sibling engines."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        im.save(str(dest), "TGA")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Framework management
# ---------------------------------------------------------------------------

def _git_head(d):
    try:
        out = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _clone(url, dest, log, submodules=False):
    if dest.exists():
        shutil.rmtree(dest)
    cmd = ["git", "clone", "--depth", "1", url, str(dest)]
    if _run(cmd, log) != 0:
        raise RuntimeError(f"git clone failed: {url}")
    if submodules:
        if _run(["git", "-C", str(dest), "submodule", "update", "--init",
                 "--recursive", "--depth", "1"], log) != 0:
            raise RuntimeError(f"submodule update failed: {dest}")


def _check_pin(d, pin, name, log):
    head = _git_head(d)
    if head and head != pin:
        log_line(log, f"note: {name} is at {head[:12]}, this tool was "
                      f"verified against {pin[:12]} - continuing")


def _n64recomp_ok(d):
    return (d / "build" / "N64Recomp").is_file() and \
        (d / "build" / "RSPRecomp").is_file()


# The recompiler is built from the copy nested inside the runtime
# submodule (Zelda64Recomp/lib/N64ModernRuntime/N64Recomp) - the
# version the runtime project itself records. This pairing matters:
# N64Recomp upstream HEAD emits a section-table format (a
# std::optional got_ram_addr field) that the pinned runtime's
# librecomp headers do not have, so code generated by HEAD fails to
# compile at the final link step (verified 2026-11-03). The nested
# copy's output matches the runtime exactly.
N64RECOMP_PIN = "c1a6dc93bfa5977de0ea256562058be4f1b73353"


def _nested_recomp(zelda_dir):
    return zelda_dir / "lib" / "N64ModernRuntime" / "N64Recomp"


def _zelda_ok(d):
    return (d / "Zelda64RecompSyms" / "mm.us.rev1.syms.toml").is_file() \
        and (d / "lib" / "rt64" / "CMakeLists.txt").is_file() \
        and (d / "lib" / "N64ModernRuntime" / "librecomp").is_dir()


def _z64dec_ok(d):
    return (d / "z64decompress").is_file()


def ensure_frameworks(log=None):
    """Clone + build the host tools once, into framework-cache.
    Returns (n64recomp_dir, zelda_dir, z64dec_dir)."""
    FRAMEWORK_CACHE.mkdir(parents=True, exist_ok=True)
    zelda_dir = FRAMEWORK_CACHE / "Zelda64Recomp"
    dec_dir = FRAMEWORK_CACHE / "z64decompress"

    if not _zelda_ok(zelda_dir):
        log_line(log, "Fetching Zelda64Recomp (runtime project + "
                      "Majora's Mask symbol data)...")
        if not (zelda_dir / ".git").is_dir():
            _clone(ZELDA_URL, zelda_dir, log, submodules=True)
        else:
            _run(["git", "-C", str(zelda_dir), "submodule", "update",
                  "--init", "--recursive", "--depth", "1"], log)
        _check_pin(zelda_dir, ZELDA_PIN, "Zelda64Recomp", log)
        if not _zelda_ok(zelda_dir):
            raise RuntimeError("Zelda64Recomp checkout is incomplete "
                               "(symbols or runtime submodules missing)")
    n64_dir = _nested_recomp(zelda_dir)
    if not _n64recomp_ok(n64_dir):
        log_line(log, "Building N64Recomp (the static recompiler, "
                      "runtime-paired copy)...")
        _check_pin(n64_dir, N64RECOMP_PIN, "N64Recomp (nested)", log)
        if _run(["cmake", "-S", str(n64_dir), "-B", str(n64_dir / "build"),
                 "-DCMAKE_BUILD_TYPE=Release", "-G", "Ninja"],
                log, env=_cmake_env()) != 0:
            raise RuntimeError("N64Recomp configure failed")
        if _run(["cmake", "--build", str(n64_dir / "build"), "-j", "4"],
                log) != 0:
            raise RuntimeError("N64Recomp build failed")
    if not _z64dec_ok(dec_dir):
        log_line(log, "Fetching z64decompress (ROM decompressor)...")
        if not (dec_dir / ".git").is_dir():
            _clone(Z64DECOMPRESS_URL, dec_dir, log)
        _check_pin(dec_dir, Z64DECOMPRESS_PIN, "z64decompress", log)
        if _run(["make"], log, cwd=dec_dir) != 0:
            raise RuntimeError("z64decompress build failed")
    return n64_dir, zelda_dir, dec_dir


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def _camel(title):
    words = re.sub(r"[^A-Za-z0-9 ]", "", title).split()
    return "".join(w.capitalize() for w in words) or "Game"


README_TMPL = """{title} - native PC recompilation (N64Recomp + N64ModernRuntime)

HOW TO PLAY
  Run {exe}. The game's own launcher opens first: choose your ROM
  when asked. It must be the standard retail Majora's Mask (USA)
  ROM - the exact cartridge dump this build was made from (the game
  verifies it). Do NOT use a decompressed ROM here, and no ROM is
  included in this package - supply your own dump of your own
  cartridge.

  Saves, controller settings and options are handled by the launcher
  and stored in your user config folder, like the original
  Zelda64Recomp release.

NOT AVAILABLE IN THIS BUILD
  * Netplay: the N64 recomp ecosystem has no netplay support.
  * RetroAchievements: there is no integration (the runtime exposes
    no native hook a module could attach to). Play with
    RetroArch/emulation if achievements are a must for this game.
  These are documented gaps, not settings you failed to find.

CREDITS
  Built with N64Recomp Tools, using:
  * N64Recomp by Wiseguy (Mr-Wiseguy) and contributors -
    MIT License, (c) 2024 Wiseguy.
  * N64ModernRuntime (librecomp + ultramodern) by Wiseguy and
    contributors - GPL-3.0.
  * Zelda64Recomp (runtime project, Majora's Mask symbol data and
    RSP microcode configuration) by the Zelda64Recomp contributors -
    GPL-3.0 (see the project for full license text).
  * RT64 renderer by the RT64 contributors - MIT License,
    (c) 2024 RT64 Contributors.
  * z64decompress by z64me (@z64.me) and contributors - GPL-3.0.
  This package contains no game data and no firmware.
"""


def run_job(job, log=None):
    """job: {rom, output_dir, jobs?, targets?}. Converts one ROM.
    Only Majora's Mask (USA) is convertible today (see SUPPORTED);
    anything else fails fast with the probe's reasons. Linux is the
    only target: upstream's Windows build is MSVC-only."""
    rom = Path(job["rom"])
    out_dir = Path(job.get("output_dir") or (APP_DIR / "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = str(job.get("jobs") or 2)
    targets = [t.lower() for t in (job.get("targets") or ["linux"])]
    result = {"status": "failed", "zips": [], "failed_targets": []}

    probe = probe_rom(rom)
    if not probe.get("ok"):
        result["error"] = probe.get("error", "probe failed")
        log_line(log, f"ERROR: {result['error']}")
        return result
    title = probe["title"]
    if not probe["supported"]:
        result["error"] = ("unsupported ROM: " +
                           "; ".join(probe["review_reasons"]))
        log_line(log, "ERROR: " + result["error"])
        return result
    sup = probe["support"]
    if "windows" in targets:
        log_line(log, "note: Windows packages are not producible for "
                      "N64 (the runtime's Windows build is MSVC-only); "
                      "building Linux only")
        result["failed_targets"].append("windows")
    camel = _camel(sup["game"])

    try:
        n64_dir, zelda_dir, dec_dir = ensure_frameworks(log)
    except Exception as e:
        result["error"] = f"framework setup failed: {e}"
        log_line(log, "ERROR: " + result["error"])
        return result

    proj = out_dir / f"{camel}-src"
    if proj.exists():
        shutil.rmtree(proj)
    log_line(log, f"Staging project at {proj} ...")

    def _ignore(dirpath, names):
        # Root-level recompiler binaries are re-copied below; the
        # nested lib/N64ModernRuntime/N64Recomp tree must survive -
        # the runtime build includes its headers and runs the root
        # binary for its patches step. (A flat ignore_patterns on the
        # name "N64Recomp" would delete that whole subtree.)
        drop = set()
        for n in names:
            if n in (".git", "build-cmake", "shadercache",
                     "RecompiledFuncs"):
                drop.add(n)
            elif n.endswith((".z64", ".n64")):
                drop.add(n)
            elif Path(dirpath) == zelda_dir and \
                    n in ("N64Recomp", "RSPRecomp"):
                drop.add(n)
        return drop

    shutil.copytree(zelda_dir, proj, ignore=_ignore)

    # Compat patch (found building against clang 18 / gcc 13 headers,
    # 2026-11-03): the generated recomp_overlays.inl uses std::nullopt
    # but the registration TUs never include <optional> - older
    # libstdc++ leaked it transitively, current ones do not, and the
    # build dies at the final step with "no member named 'nullopt'".
    for rel in ("src/main/register_overlays.cpp",
                "src/main/register_patches.cpp"):
        f = proj / rel
        if f.is_file():
            text = f.read_text(errors="replace")
            if "#include <optional>" not in text:
                f.write_text("#include <optional>\n" + text)
                log_line(log, f"compat patch: <optional> include "
                              f"added to {rel}")

    # The recompiler consumes the DECOMPRESSED ROM; the player-facing
    # build still runs the standard retail ROM at runtime.
    raw = rom.read_bytes()
    retail = proj / "retail.z64"
    retail.write_bytes(normalize_image(raw, probe["format"]))
    uncomp = proj / sup["uncompressed_name"]
    if _run([dec_dir / "z64decompress", retail, uncomp], log) != 0 \
            or not uncomp.is_file():
        result["error"] = "ROM decompression failed"
        log_line(log, "ERROR: " + result["error"])
        return result
    retail.unlink()

    shutil.copy2(n64_dir / "build" / "N64Recomp", proj / "N64Recomp")
    shutil.copy2(n64_dir / "build" / "RSPRecomp", proj / "RSPRecomp")
    os.chmod(proj / "N64Recomp", 0o755)
    os.chmod(proj / "RSPRecomp", 0o755)

    log_line(log, "Running N64Recomp (static recompilation)...")
    if _run([proj / "N64Recomp", sup["config"]], log, cwd=proj) != 0:
        result["error"] = "N64Recomp failed"
        log_line(log, "ERROR: " + result["error"])
        return result
    for rsp_cfg in sup["rsp_configs"]:
        log_line(log, f"Running RSPRecomp ({rsp_cfg})...")
        if _run([proj / "RSPRecomp", rsp_cfg], log, cwd=proj) != 0:
            result["error"] = f"RSPRecomp failed on {rsp_cfg}"
            log_line(log, "ERROR: " + result["error"])
            return result

    log_line(log, "Configuring the runtime build (Clang + Ninja)...")
    if _run(["cmake", "-S", str(proj), "-B", str(proj / "build-cmake"),
             "-DCMAKE_CXX_COMPILER=clang++", "-DCMAKE_C_COMPILER=clang",
             "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"],
            log, env=_cmake_env()) != 0:
        result["error"] = "runtime configure failed"
        log_line(log, "ERROR: " + result["error"])
        return result
    log_line(log, f"Building {sup['exe']} (this is the long step)...")
    if _run(["cmake", "--build", str(proj / "build-cmake"),
             "--target", sup["exe"], "-j", jobs], log) != 0:
        result["error"] = "runtime build failed"
        log_line(log, "ERROR: " + result["error"])
        return result

    exe = proj / "build-cmake" / sup["exe"]
    if not exe.is_file() or exe.stat().st_size < 5_000_000:
        result["error"] = "build finished but the executable is " \
                          "missing or suspiciously small"
        log_line(log, "ERROR: " + result["error"])
        return result
    log_line(log, f"Built {exe.name}: {exe.stat().st_size:,} bytes")

    cover = fetch_cover(sup.get("boxart") or sup["game"], log=log)
    if not cover:
        cover = fetch_cover(title, log=log)
    stage = out_dir / camel
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "artwork" / sup["game"]).mkdir(parents=True)
    shutil.copy2(exe, stage / sup["exe"])
    os.chmod(stage / sup["exe"], 0o755)
    if (proj / "assets").is_dir():
        shutil.copytree(proj / "assets", stage / "assets")
    (stage / "README.txt").write_text(README_TMPL.format(
        title=sup["game"], exe=sup["exe"]))
    if cover:
        (stage / "artwork" / sup["game"] / "front.png").write_bytes(cover)
    zip_path = out_dir / f"{camel}-Linux.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                info = zipfile.ZipInfo(str(f.relative_to(out_dir)))
                info.compress_type = zipfile.ZIP_DEFLATED
                if f.name == sup["exe"]:
                    info.external_attr = 0o755 << 16
                z.writestr(info, f.read_bytes())
    log_line(log, f"Packaged {zip_path} ({zip_path.stat().st_size:,} "
                  f"bytes)")
    result.update({"status": "done", "zips": [str(zip_path)],
                   "project": str(proj), "probe": probe})
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "toolbox":
        rows = toolbox()
        for r in rows:
            state = r["found"] if r["found"] else "MISSING"
            print(f"{r['tool']}: {state}  ({r['needed']})")
        print("Install line:", APT_LINE)
        return 0
    if cmd == "probe":
        rom = None
        for i, a in enumerate(argv):
            if a == "--rom" and i + 1 < len(argv):
                rom = argv[i + 1]
        if not rom:
            print("usage: engine.py probe --rom <file>")
            return 2
        print(json.dumps(probe_rom(rom), indent=1))
        return 0
    if cmd == "run":
        job_path = None
        for i, a in enumerate(argv):
            if a == "--job" and i + 1 < len(argv):
                job_path = argv[i + 1]
        if not job_path:
            print("usage: engine.py run --job <job.json>")
            return 2
        job = json.loads(Path(job_path).read_text())
        res = run_job(job, log=print)
        print("RESULT:", res["status"],
              res.get("zips", []), res.get("error", ""))
        return 0 if res["status"] == "done" else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
