# N64Recomp Tools

Convert Nintendo 64 ROMs to native PC games — as far as the N64
recomp ecosystem honestly allows.

## Why this tool is different from the other converters

Every other system in the family has a shared runtime that any ROM
can be scaffolded into. N64 does not. **N64Recomp** statically
recompiles a game from its ROM *plus per-game symbol data* —
function boundaries, overlay tables and RSP microcode layouts that
come out of decompilation projects (as an ELF or a symbols file).
There is no automatic way to invent that data for an arbitrary ROM,
and each supported game needs its own runtime project built on
**N64ModernRuntime** (librecomp + ultramodern + RT64).

So this tool:

- **Probes** any .z64/.n64/.v64 ROM: header (internal name,
  cartridge code, region, entry point, CRCs), byte order (the magic
  bytes, not the extension — GameCube-edition dumps are often .v64
  inside an .n64 name), and normalized SHA-1/CRC32.
- **Converts** the titles with complete upstream support. Today
  that is exactly one: **The Legend of Zelda: Majora's Mask
  (USA)**, via the Zelda64Recomp project. The conversion runs the
  real pipeline on your machine: decompress the ROM
  (z64decompress), run N64Recomp + RSPRecomp, build the runtime
  with Clang/CMake, package `<Game>-Linux.zip`.
- **Flags everything else "needs review"** with the specific
  reason, instead of producing builds that cannot work.

## What you get for a converted game

The package holds the executable, its `assets/` folder, a README,
and fetched box art under `artwork/`. No ROM and no firmware is
included, and none is needed beyond the ROM itself — the PIF/boot
process is handled in HLE by the runtime. On launch, the game's own
launcher asks you to choose your ROM: give it the **standard retail
Majora's Mask (USA)** dump (not a decompressed one — decompression
is only an internal build step). It verifies the ROM before it
plays. Keep `assets/` next to the executable.

## Known gaps (documented, not hidden)

- **Windows:** Linux packages only. Upstream builds Windows with
  MSVC exclusively; there is no cross build to produce here.
- **Netplay:** does not exist anywhere in this ecosystem.
- **RetroAchievements:** not integrated. librecomp exposes no
  native frame/memory hook a module could attach to (its mod API
  runs recompiled game code), so there is no honest way to bolt
  the family's RA module onto this runtime today.
- **Save types** are not shown in the probe: the N64 header does
  not record them. The runtime implements EEPROM/SRAM/FlashRAM
  saves as each game requires.

## Requirements

Linux with: git, CMake, Ninja, Clang, lld, SDL2 and GTK 3 dev
packages, Vulkan headers, glslangValidator. The Convert tab's
toolbox row checks them and can install the missing apt packages:

```
sudo apt install -y git cmake ninja-build clang lld libsdl2-dev \
    libgtk-3-dev libvulkan-dev glslang-tools
```

The first conversion also clones the frameworks into
`framework-cache/` and builds the recompiler once; later
conversions reuse them. A conversion takes a while (the runtime
build is the long step).

## Credits

- **N64Recomp** by Wiseguy (Mr-Wiseguy) and contributors —
  MIT License, © 2024 Wiseguy. https://github.com/N64Recomp/N64Recomp
- **N64ModernRuntime** (librecomp + ultramodern) by Wiseguy and
  contributors — GPL-3.0. https://github.com/N64Recomp/N64ModernRuntime
- **Zelda64Recomp** by the Zelda64Recomp contributors — GPL-3.0.
  https://github.com/Zelda64Recomp/Zelda64Recomp
- **RT64** renderer by the RT64 contributors — MIT License,
  © 2024 RT64 Contributors. https://github.com/rt64/rt64
- **z64decompress** by z64me and contributors — GPL-3.0.
  https://github.com/z64tools/z64decompress
- Cover art: the libretro-thumbnails project.

Not published to GitHub yet — the converter family publishes
together when all of its tools are done.
