"""
als_core.py

Shared .als primitives used across the tool: the Context passed to every step, the
depth-tracking block finder, device-name extraction, and project file discovery.
Imports nothing from the rest of the codebase, so every other module can build on it.
"""

import re
import gzip
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote


# Folders Ableton owns — none of them ever hold user .als files, so walking into them
# only wastes time. 'Backup' is Live's own auto-backup folder; the rest are the
# subfolders a Live Project uses for its collected content and project marker.
SKIP_DIRS = ("Backup", "Ableton Project Info", "Samples", "Presets")


# ═════════════════════════════════════════════════════════════
# LIVE VERSION GUARD
# ═════════════════════════════════════════════════════════════
#
# The whole tool — every SampleRef/MxPatchRef/plugin parser here — assumes the Live 11/12
# .als schema. Live 9/10 store sample references in a completely different <FileRef> layout:
# a <RelativePath> made of <RelativePathElement Dir="…"> elements, the filename in <Name>,
# and the absolute path buried in a <Data> Mac-alias hex blob — no <Path>, no
# <RelativePath Value="…">, no <OriginalFileSize>. Fed one of those, the ref parsers read
# every field as empty and silently misreport the project (Collect flags EVERY sample as one
# "missing" file). So anything older than MIN_SUPPORTED_LIVE is detected and refused up
# front rather than mangled. A version we can't read off the header is left to proceed.
MIN_SUPPORTED_LIVE = 11


def read_als_header(als_path: Path) -> str:
    """Decompress only the opening bytes of an .als — enough to hold the root <Ableton …> tag —
    without inflating the whole (often huge) set just to read its version."""
    try:
        with gzip.open(als_path, "rb") as f:
            return f.read(1024).decode("utf-8", "replace")
    except OSError:
        return ""


def detect_live_version(xml_or_header: str) -> int | None:
    """Major Ableton Live version, read off the Creator string in an .als header.
    'Ableton Live 9.7.5' → 9, 'Ableton Live 12.4.3' → 12. None when it can't be determined."""
    m = re.search(r'Creator="Ableton Live\s+(\d+)', xml_or_header)
    return int(m.group(1)) if m else None


# ═════════════════════════════════════════════════════════════
# CONTEXT
# ═════════════════════════════════════════════════════════════

@dataclass
class Context:
    """Config + runtime state passed to every pipeline step."""
    track_config: dict                                              # prefix → {color, ...} map (from track_config.ini)
    dedupe_devices: list             = field(default_factory=list)  # device names to deduplicate
    exclude_conversion_types: list   = field(default_factory=list)  # track types skipped by mixer-automation step
    exclude_midi_prefixes: list      = field(default_factory=list)  # track-name prefixes skipped by MIDI-affecting steps
    chain_suffix: str                = ''                           # suffix appended to cloned device chains
    transpose_semitones: int         = 0                            # MIDI transpose amount
    lane_height: int                 = 68                           # Track height
    als_path: Path | None            = None                         # current .als being processed (set per-file by runner)
    report_written: bool             = False                        # runtime flag: step_project_report sets this so the runner marks it ✓ even though the XML didn't change


# ═════════════════════════════════════════════════════════════
# ALS PRIMITIVES
# ═════════════════════════════════════════════════════════════

def find_blocks(xml_text: str, tag: str) -> list:
    """Find all <tag>...</tag> blocks via depth tracking. Returns (start, end, content) tuples."""
    results  = []
    open_pat = re.compile(r"<" + re.escape(tag) + r"[\s>]")
    cls_pat  = re.compile(r"</" + re.escape(tag) + r">")
    sc_pat   = re.compile(r"<" + re.escape(tag) + r"(?:\s[^>]*)?\/>")
    pos = 0
    while True:
        m = open_pat.search(xml_text, pos)
        if not m:
            break
        start = m.start()
        if sc := sc_pat.match(xml_text, start):
            results.append((start, sc.end(), xml_text[start:sc.end()]))
            pos = sc.end()
            continue
        depth, pos = 1, m.end()
        while depth > 0:
            mo = open_pat.search(xml_text, pos)
            mc = cls_pat.search(xml_text, pos)
            if not mc:
                break
            if mo and mo.start() < mc.start():
                depth += 1; pos = mo.end()
            else:
                depth -= 1; pos = mc.end()
        results.append((start, pos, xml_text[start:pos]))

    return results


def extract_device_name(block: str, tag: str = "") -> str | None:
    """Extract device name — checks VST2 PlugName, VST3/AU BrowserContentPath, then native tag."""

    if not tag:
        if m := re.match(r'<(\w+)\s', block.strip()):
            tag = m.group(1)

    is_external = tag in ("PluginDevice", "AuPluginDevice")
    prefix = "ext" if is_external else "int"

    # Slice off everything from <Branches> onward so nested chain names
    # don't pollute name searches with their own EffectiveName/UserName values
    shallow = block[:block.index('<Branches>')] if '<Branches>' in block else block

    patterns = [
        (r'<PlugName\s+Value="([^"]+)"',        1, None),
        (r'<EffectiveName\s+Value="([^"]+)"',   1, None),
    ]
    if is_external:
        # VST3/AU store the plugin name inside the browser path after the last : or #
        patterns.insert(1, (r'BrowserContentPath\s+Value="[^"]*[:#]([^"#:/]+)"', 1, unquote))

    for pat, group, transform in patterns:
        if m := re.search(pat, shallow):
            val = m.group(group)
            val = transform(val) if transform else val
            if re.match(r'FileId_\d+', val):
                continue  # skip internal Ableton file IDs, fall through to tag name
            return f"[{prefix}] {val}"

    # Fallback: use the XML tag itself (e.g. MultibandDynamics → [int] MultibandDynamics)
    return f"[{prefix}] {tag}" if tag else None


def find_als_files(root: Path) -> list:
    """
    Find all .als files recursively below root.
    Skips:  any file inside an Ableton-owned folder (SKIP_DIRS) — Live's own Backup/,
            plus the Samples/, Presets/ and Ableton Project Info/ folders a Project
            uses, none of which ever contain user sets
    Skips:  files ending in '_processed' (already handled by the script)
    """
    als_files = []
    for f in root.rglob("*.als"):
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        if f.stem.endswith("_processed"):
            continue
        als_files.append(f)

    return sorted(als_files)
