"""
ableton_project_processor.py

A swiss army knife for processing Ableton projects — batch clean tracks, ungroup,
strip unused devices, sort & recolor, quantize/transpose MIDI, convert mixer automation
to utility, and generate detailed per-project reports with external plugin aggregation,
all at the XML level without opening Live once.

Operates on raw decompressed XML inside .als gzip archives.
Original files are never overwritten — a new _processed.als is always created.

Usage:
    python ableton_project_processor.py

© 2026 Hodel33
"""
import gzip
import re
import sys
import os
import io
import configparser
from pathlib import Path
import xml.etree.ElementTree as ET
from collections import defaultdict
from urllib.parse import unquote


CONFIG_LOCATION = "config.ini"


# ═════════════════════════════════════════════════════════════
# SHARED UTILITIES
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


def find_all_devices(xml_text: str) -> list:
    """
    Find all user-inserted devices (native + external) in the project.
    Scopes search to <DeviceChain><Devices> blocks only.

    Ableton track structure:
        <AudioTrack> / <MidiTrack> / <MainTrack>
            <FreezeSequencer>          ← internal engine, SIBLING of DeviceChain
                <AudioSequencer>       ← NOT a user device, excluded by this scope
            </FreezeSequencer>
            <DeviceChain>              ← user device chain lives here
                <Devices>              ← only place we scan
                    <Eq8 Id="...">     ← real user device ✓
                    ...
                </Devices>
            </DeviceChain>

    By finding only top-level DeviceChain blocks (not rack-internal ones),
    we naturally exclude FreezeSequencer, Mixer, MidiSequencer etc.
    Rack containers (DrumGroupDevice, AudioEffectGroupDevice etc.) are returned
    as single top-level entries — their internal chains are never descended into,
    since pos advances past the entire rack block after it is added to results.
    Device identity is confirmed by the <On><LomId/><Manual Value= structure
    present in every real Ableton device, which naturally filters out any
    non-device XML elements that match the tag pattern.

    Returns (start, end, content, tag_name) tuples with global offsets.
    """
    results    = []
    tag_pat    = re.compile(r'<([A-Z][A-Za-z0-9]+)\s+Id="\d+"')
    device_pat = re.compile(r'<On>\s*<LomId\b[^/]*/>\s*<Manual\s+Value=', re.DOTALL)

    # Find all DeviceChain blocks, keep only top-level ones (not rack-internal)
    all_chains = find_blocks(xml_text, "DeviceChain")
    top_chains = [
        (s, e, c) for s, e, c in all_chains
        if not any(os < s and e < oe for os, oe, _ in all_chains)
    ]

    for dc_start, _, dc_content in top_chains:
        # Find Devices blocks inside this DeviceChain, keep top-level only
        all_dev_blocks = find_blocks(dc_content, "Devices")
        top_dev_blocks = [
            (s, e, c) for s, e, c in all_dev_blocks
            if not any(os < s and e < oe for os, oe, _ in all_dev_blocks)
        ]

        for d_rel_start, _, d_content in top_dev_blocks:
            d_start = dc_start + d_rel_start

            pos = 0
            while m := tag_pat.search(d_content, pos):
                tag       = m.group(1)
                rel_start = m.start()

                blocks = find_blocks(d_content[rel_start:], tag)
                if not blocks:
                    pos = m.end()
                    continue
                b_start, b_end, content = blocks[0]

                # Every real Ableton device has <On><LomId/><Manual Value= structure
                if device_pat.search(content):
                    g_start = d_start + rel_start + b_start
                    g_end   = d_start + rel_start + b_end
                    results.append((g_start, g_end, content, tag))

                pos = rel_start + b_end

    return results


def get_track_ranges(xml_text: str) -> list:
    """Return (start, end, name) for every track in the project.
    
    Handles both Live 11 (<MasterTrack>) and Live 12 (<MainTrack>).
    """
    tags = ["AudioTrack", "MidiTrack", "ReturnTrack", "GroupTrack", "MasterTrack", "MainTrack"]
    tracks = []

    # Collect all tracks then sort by byte offset to match visual order in Ableton
    all_tracks = []
    for tag in tags:
        for start, end, content in find_blocks(xml_text, tag):
            all_tracks.append((start, end, content, tag))

    idx = 1
    for start, end, content, tag in sorted(all_tracks, key=lambda x: x[0]):
        if tag in ("MasterTrack", "MainTrack"):
            tracks.append((start, end, "Master"))
            continue
        m = re.search(r'<(?:UserName|EffectiveName)\s+Value="([^"]+)"', content)
        name = m.group(1) if m else tag
        tracks.append((start, end, f"#{idx:02d} {name}"))
        idx += 1

    return tracks


def get_excluded_track_ranges(xml_text: str, context: dict, track_config: dict) -> list:
    """Return byte ranges for tracks whose prefix (or parent group prefix) is in exclude_midi_prefixes."""
    exclude = set(context.get('exclude_midi_prefixes', []))
    if not exclude:
        return []

    track_data = []
    for tag in ("MidiTrack", "AudioTrack", "GroupTrack"):
        for start, end, content in find_blocks(xml_text, tag):
            m = re.search(r'<(?:UserName|EffectiveName)\s+Value="([^"]+)"', content)
            name = m.group(1) if m else ""
            raw = re.sub(r'^#\d+\s+', '', name)
            first_word = raw.split()[0] if raw.split() else ""
            prefix = first_word if (first_word.isupper() and first_word in track_config) else first_word[:2]
            xid_m = re.search(r'<(?:MidiTrack|AudioTrack|GroupTrack)\s+Id="(\d+)"', content)
            gid_m = re.search(r'<TrackGroupId\s+Value="(\d+)"', content)
            track_data.append({
                'start': start, 'end': end, 'prefix': prefix,
                'xid': xid_m.group(1) if xid_m else None,
                'gid': gid_m.group(1) if gid_m else None
            })

    id_to_prefix = {t['xid']: t['prefix'] for t in track_data if t['xid']}

    ranges = []
    for t in track_data:
        parent_prefix = id_to_prefix.get(t['gid'], '') if t['gid'] else ''
        if t['prefix'] in exclude or parent_prefix in exclude:
            ranges.append((t['start'], t['end']))

    return ranges


def sub_outside_ranges(pattern, repl, xml_text: str, excluded: list) -> str:
    """Apply re.sub only to segments of xml_text outside excluded byte ranges."""
    parts, prev = [], 0
    for start, end in sorted(excluded):
        parts.append(re.sub(pattern, repl, xml_text[prev:start]))
        parts.append(xml_text[start:end])
        prev = end
    parts.append(re.sub(pattern, repl, xml_text[prev:]))
    return "".join(parts)


def track_of(offset: int, track_ranges: list) -> str:
    """Return the track name that contains the given offset."""
    for start, end, name in track_ranges:
        if start <= offset <= end:
            return name
        
    return "Unknown"


def splice_out(xml_text: str, blocks: list) -> str:
    """Remove blocks (sorted descending by start) from raw XML string."""
    for block in sorted(blocks, key=lambda b: b["start"], reverse=True):
        s, e = block["start"], block["end"]
        # Also eat the preceding newline+indent to avoid blank lines
        while s > 0 and xml_text[s - 1] in (" ", "\t"):
            s -= 1
        if s > 0 and xml_text[s - 1] == "\n":
            s -= 1
        xml_text = xml_text[:s] + xml_text[e:]

    return xml_text


def find_als_files(root: Path) -> list:
    """
    Find all .als files exactly one subfolder level below root.
    Scans:  root/AnyFolder/*.als
    Skips:  root/AnyFolder/Backup/*.als  (any deeper nesting)
    Skips:  root/*.als                   (files directly in root)
    """
    als_files = []
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        for f in folder.iterdir():
            if f.is_file() and f.suffix.lower() == ".als" and not f.stem.endswith("_processed"):
                als_files.append(f)

    return sorted(als_files)


def print_main_header():
    """Clear the terminal and print the ASCII art header."""
    os.system("cls" if os.name == "nt" else "clear") # cls on Windows, clear on macOS/Linux
    print("\033[H", end="")  # move cursor to top-left
    print("\033[38;5;208m")  # orange-ish color
    print(r'''
░█▀█░█▀▄░█░░░█▀▀░▀█▀░█▀█░█▀█░░░█▀█░█▀▄░█▀█░▀▀█░█▀▀░█▀▀░▀█▀░░░█▀█░█▀▄░█▀█░█▀▀░█▀▀░█▀▀░█▀▀░█▀█░█▀▄
░█▀█░█▀▄░█░░░█▀▀░░█░░█░█░█░█░░░█▀▀░█▀▄░█░█░░░█░█▀▀░█░░░░█░░░░█▀▀░█▀▄░█░█░█░░░█▀▀░▀▀█░▀▀█░█░█░█▀▄
░▀░▀░▀▀░░▀▀▀░▀▀▀░░▀░░▀▀▀░▀░▀░░░▀░░░▀░▀░▀▀▀░▀▀░░▀▀▀░▀▀▀░░▀░░░░▀░░░▀░▀░▀▀▀░▀▀▀░▀▀▀░▀▀▀░▀▀▀░▀▀▀░▀░▀
    ''')
    print("© 2026 Hodel33")
    print("‾" * 96)
    print("\033[0;0m")


def print_pipeline_header():
    """Print the processing settings section header."""
    print("************      PROCESSING SETTINGS      ************")
    print()


def print_pipeline_info(root: Path):
    """Print active pipeline steps and prompt the user to confirm before processing."""
    print(f"  Project folder : {root}")
    print(f"  Active steps   :")
    print()
    for step_id, step_fn, description in PIPELINE:
        print(f"    [+] {description}")

    print()
    
    if any(step_id == "convert_mixer_automation_to_utility" for step_id, _, _ in PIPELINE):
        print("  Note: project must have at least one Utility device anywhere to use as clone template.")
        print()

    print()

    user_input = input("Press ENTER to start processing (q + ENTER to exit): ").strip().lower()
    if user_input == "q":
        exit()

def load_config(config_file=CONFIG_LOCATION):
    """Load and return the config.ini file as a ConfigParser object."""

    config = configparser.ConfigParser()
    config.optionxform = str
    if not config.read(config_file, encoding='utf-8'):
        raise FileNotFoundError(f"{config_file} not found")
    
    return config


def load_pipeline(config):
    """Build and return the ordered list of enabled pipeline steps from config."""
    
    all_steps = [
        ("remove_empty_tracks",                 step_remove_empty_tracks,                   "Remove empty tracks"),
        ("remove_muted_tracks",                 step_remove_muted_tracks,                   "Remove muted tracks"),
        ("ungroup_tracks",                      step_ungroup_tracks,                        "Ungroup all grouped tracks"),
        ("remove_unused_return_tracks",         step_remove_unused_return_tracks,           "Remove unused return tracks"),
        ("remove_disabled_devices",             step_remove_disabled_devices,               "Remove disabled devices"),
        ("remove_non_automated_devices",        step_remove_non_automated_devices,          "Remove non-automated insert devices"),
        ("deduplicate_devices",                 step_deduplicate_devices,                   "Deduplicate specific devices per track"),
        ("convert_mixer_automation_to_utility", step_convert_mixer_automation_to_utility,   "Convert Mixer Vol/Pan automation to Utility device"),
        ("sort_color_tracks",                   step_sort_color_tracks,                     "Sort & Recolor tracks/clips"),
        ("duplicate_device_chain",              step_duplicate_device_chain,                "Duplicate device chains to new tracks"),
        ("quantize_midi_notes",                 step_quantize_midi_notes,                   "Quantize all MIDI notes to 1/16"), 
        ("transpose_midi_notes",                step_transpose_midi_notes,                  "Transpose all MIDI notes"),
        ("set_track_heights",                   step_set_track_heights,                     "Set all track heights to a custom size"), 
        ("get_project_report",                  step_project_report,                        "Export full project report to txt"),
    ]

    if 'PIPELINE' not in config:
        raise ValueError("Missing [PIPELINE] section")

    cleaned = {k: v.split('#')[0].strip() for k, v in config['PIPELINE'].items()}
    enabled = {k: v == 'true' for k, v in cleaned.items()}
    pipeline = [s for s in all_steps if enabled.get(s[0], False)]

    return pipeline


def load_settings(config):
    """Load global settings from [SETTINGS] section."""
    
    if 'SETTINGS' not in config:
        return {}
    
    # Strip comments + convert to int, list or str where appropriate
    settings = {}

    for k, v in config['SETTINGS'].items():
        clean = v.split('#')[0].strip()
        if clean == '':
            settings[k] = []
        elif ',' in clean:
            settings[k] = [item.strip() for item in clean.split(',')]
        else:
            try:
                settings[k] = int(clean)
            except ValueError:
                settings[k] = clean
        
    return settings


def load_track_config(config):
    """Load track prefixes, sort order, and colors from config."""
    
    if 'TRACK_PREFIXES' not in config:
        raise ValueError("Missing [TRACK_PREFIXES] section")
    
    if 'DEF' not in config['TRACK_PREFIXES']:
        raise ValueError("[TRACK_PREFIXES] is missing required 'DEF' fallback entry")

    track_config = {}
    for prefix, value in config['TRACK_PREFIXES'].items():
        clean_value = value.split('#')[0].strip()
        try:
            sort_order, color_idx = map(int, clean_value.split(','))
        except (ValueError, TypeError):
            raise ValueError(f"[TRACK_PREFIXES] invalid format for '{prefix}' — expected 'sort_order, color_idx'")
        track_config[prefix] = {'sort': sort_order, 'color': color_idx}
    
    return track_config


def get_track_info(xml_text: str) -> list[dict]:
    """Extract all tracks: pos, content, name, type and color."""
    track_tags = ["AudioTrack", "MidiTrack", "ReturnTrack", "GroupTrack", "MasterTrack", "MainTrack"]
    tracks = []
    
    for tag in track_tags:
        for start, end, content in find_blocks(xml_text, tag):
            name_match = re.search(r'<(?:UserName|EffectiveName)\s+Value="([^"]*)"', content)
            color_match = re.search(r'<Color\s+Value="(\d+)"', content)
            name = name_match.group(1) if name_match else tag
            color = color_match.group(1) if color_match else "0"

            tracks.append({
                'start': start,
                'end': end, 
                'content': content,
                'name': name,
                'type': tag,
                'color': color
            })
    
    return tracks


def set_track_color(track_content: str, color_idx: int) -> str:
    """Set <Color Value="N"/> - Ableton track color tag."""

    color_pat = r'<Color\s+Value\s*=\s*"(\d+)"'
    if re.search(color_pat, track_content):
        return re.sub(color_pat, f'<Color Value="{color_idx}"', track_content, count=1)
    
    name_pat = r'</Name>'

    return re.sub(name_pat, f'</Name>\n      <Color Value="{color_idx}"/>', track_content, count=1)


def set_clip_colors(track_content: str, color_idx: int) -> str:
    """Set <Color Value="N"/> inside ALL clips (MidiClip + AudioClip) to match track color."""
    clip_pat = r'(<(?:MidiClip|AudioClip)\b[^>]*>.*?)<Color\s+Value\s*=\s*"\d+"'
    return re.sub(
        clip_pat,
        lambda m: f'{m.group(1)}<Color Value="{color_idx}"',
        track_content,
        flags=re.DOTALL
    )


def validate_xml(xml_text: str, original: str | None = None) -> list[str]:
    """Check processed XML for corruption causes introduced by processing."""
    errors = []

    # Check TrackSendHolder count vs ReturnTrack count
    return_count = len(find_blocks(xml_text, "ReturnTrack"))
    source_tracks = [
        t_content
        for tag in ("AudioTrack", "MidiTrack", "GroupTrack")
        for _, _, t_content in find_blocks(xml_text, tag)
    ]
    for t_content in source_tracks:
        holder_count = len(find_blocks(t_content, "TrackSendHolder"))
        if holder_count != return_count:
            errors.append(f"TrackSendHolder count ({holder_count}) doesn't match ReturnTrack count ({return_count})")
            break

    # Check for duplicate track Ids — Ableton requires globally unique Ids across all tracks.
    # Duplicates here cause the "non-unique list ids" corruption error on project load.
    # Note: device Ids (StereoGain, PluginDevice etc.) are context-scoped and legitimately repeat.
    track_id_pattern = re.compile(
        r'<(?:AudioTrack|MidiTrack|ReturnTrack|GroupTrack|MasterTrack)\s+Id="(\d+)"'
    )
    track_ids = track_id_pattern.findall(xml_text)
    seen, dupes = set(), set()
    for i in track_ids:
        if i in seen:
            dupes.add(i)
        seen.add(i)
    if dupes:
        errors.append(f"Duplicate track Id values found: {sorted(dupes, key=int)[:10]}")

    # Check NextPointeeId is above all used IDs
    max_id    = max((int(i) for i in re.findall(r'Id="(\d+)"', xml_text)), default=0)
    next_id_m = re.search(r'<NextPointeeId\s+Value="(\d+)"', xml_text)
    if next_id_m and int(next_id_m.group(1)) <= max_id:
        # Only flag if this is a NEW issue — not pre-existing in the original file
        if original is not None:
            orig_max   = max((int(i) for i in re.findall(r'Id="(\d+)"', original)), default=0)
            orig_nxt_m = re.search(r'<NextPointeeId\s+Value="(\d+)"', original)
            if not (orig_nxt_m and int(orig_nxt_m.group(1)) <= orig_max):
                errors.append(f"NextPointeeId ({next_id_m.group(1)}) is not above max Id ({max_id})")
        else:
            errors.append(f"NextPointeeId ({next_id_m.group(1)}) is not above max Id ({max_id})")

    # Check no NEW dangling PointeeIds
    def get_dangling(xml):
        target_ids = set(re.findall(r'<(?:Automation|Modulation)Target\s+Id="(\d+)"', xml))
        return {pid for pid in re.findall(r'<PointeeId\s+Value="(\d+)"', xml) if pid not in target_ids}

    # Flag any dangling PointeeIds already present in the original file (pre-existing corruption)
    if original is None:
        existing_dangling = get_dangling(xml_text)
        if existing_dangling:
            errors.append(f"Pre-existing dangling PointeeIds (not caused by script): {len(existing_dangling)} total")

    # Only flag PointeeIds that our script introduced — not pre-existing ones in the original file
    else:
        new_dangling = get_dangling(xml_text) - get_dangling(original)
        if new_dangling:
            errors.append(f"NEW dangling PointeeIds introduced by script: {len(new_dangling)} total")

    # Check XML is not truncated
    if "</LiveSet>" not in xml_text:
        errors.append("Missing </LiveSet> — file appears truncated")

    return errors


def cleanup_project(xml_text: str) -> str:
    """
    Silent post-processing pass — always runs before validation.

    Fixes pre-existing or step-induced issues that are safe to auto-correct:
      1. Remove AutomationEnvelopes whose PointeeId has no living AutomationTarget
      2. Bump NextPointeeId above the highest Id in the project
    """
    # Remove dead automation envelopes (orphaned by removed tracks/devices)
    surviving = set(re.findall(r'<AutomationTarget\s+Id="(\d+)"', xml_text))
    dead      = [
        {"start": s, "end": e}
        for s, e, c in find_blocks(xml_text, "AutomationEnvelope")
        if (pid := re.search(r'<PointeeId\s+Value="(\d+)"', c)) and pid.group(1) not in surviving
    ]
    if dead:
        xml_text = splice_out(xml_text, dead)

    # Fix NextPointeeId counter if it has fallen behind the highest Id
    xml_text = update_next_pointee_id(xml_text)

    return xml_text


def update_next_pointee_id(xml_text: str) -> str:
    """Bump NextPointeeId to one above the highest Id currently in the project."""
    max_id = max((int(i) for i in re.findall(r'Id="(\d+)"', xml_text)), default=0)
    return re.sub(
        r'(<NextPointeeId\s+Value=")[^"]+(")',
        lambda m: f'{m.group(1)}{max_id + 1}{m.group(2)}',
        xml_text
    )



# ═════════════════════════════════════════════════════════════
# DEBUG
# ═════════════════════════════════════════════════════════════

def debug_raw_dump(als_path: Path) -> None:

    with gzip.open(als_path, "rb") as f:
        xml_text = f.read().decode("utf-8")

    print("\nAll devices found (native + external):")
    for start, end, content, tag in find_all_devices(xml_text):
        name  = extract_device_name(content, tag) or tag
        track = track_of(start, get_track_ranges(xml_text))
        print(f"  [{tag}] '{name}' on track '{track}'")


def debug_colors(als_path):
    """Show ALL tracks: positions + names + colors."""
    print(f"\n--- ALL TRACKS: POSITIONS + NAMES + COLORS ---")
    with gzip.open(als_path, "rb") as f:
        xml = f.read().decode("utf-8")
    
    tracks = get_track_info(xml)
    print(f"Found {len(tracks)} tracks:\n")
    
    for i, track in enumerate(tracks, 1):
        prefix = track['name'][:2].upper() if len(track['name']) >= 2 else "??"
        print(f"  {i:2d} | Pos {track['start']:>10,}→{track['end']:>10,} | {track['type']:<12s} | {track['name']:<12} [{prefix}] | Color={track['color']}")


def debug_track_heights(als_path):
    """Show all LaneHeight values across the project."""
    with gzip.open(als_path, "rb") as f:
        xml = f.read().decode("utf-8")

    tracks = get_track_info(xml)
    print(f"\n--- TRACK LANE HEIGHTS ---")
    print(f"{'#':>3} | {'Name':<20} | {'Type':<12} | {'Height':>6}")
    print("-" * 52)

    heights = []
    for i, track in enumerate(tracks, 1):
        match = re.search(r'<LaneHeight\s+Value\s*=\s*"(\d+)"', track['content'])
        height = int(match.group(1)) if match else 0
        heights.append(height)
        print(f"  {i:>2} | {track['name']:<20} | {track['type']:<12} | {height:>6}")

    print("-" * 52)
    print(f"  Min: {min(heights)}  |  Max: {max(heights)}  |  Avg: {sum(heights)//len(heights)}")


# ═════════════════════════════════════════════════════════════
# CLEANING STEPS
# Signature: step(xml_text, context) -> (xml_text, log_lines)
# ═════════════════════════════════════════════════════════════

def step_deduplicate_devices(xml_text: str, context: dict) -> tuple:
    """Keep only the first instance of each named device per track.
    Target names are set via dedupe_devices in config and matched as
    case-insensitive substrings — e.g. 'saus' matches 'Sausage Fattener'.
    """
    targets = context["dedupe_devices"]
    tracks  = get_track_ranges(xml_text)

    seen, to_remove = set(), []

    for start, end, content, tag in find_all_devices(xml_text):
        name = extract_device_name(content, tag)
        track = track_of(start, tracks)

        if not name or not any(t.lower() in name.lower() for t in targets):
            continue
        key = (track, name)
        if key in seen:
            to_remove.append({"start": start, "end": end, "name": name, "track": track})
        else:
            seen.add(key)

    if not to_remove:
        return xml_text, ["No duplicates found."]

    log = []
    for r in to_remove:
        parts     = r['track'].split(' ', 1)
        track_str = f"{parts[0]} '{parts[1]}'" if len(parts) == 2 else f"'{r['track']}'"
        name_bare = r['name'].split('] ', 1)
        tag_str   = name_bare[0] + ']' if len(name_bare) == 2 else ''
        dev_name  = name_bare[1] if len(name_bare) == 2 else r['name']
        log.append(f"  Removed duplicate {tag_str} '{dev_name}' on track {track_str}")

    return splice_out(xml_text, to_remove), log


def step_project_report(xml_text: str, context: dict) -> tuple:
    """
    Export a full project report to _report.txt — read-only, never modifies the project.

    PROJECT SUMMARY  — Creator, BPM, time signature, key/scale, locators, track counts,
                       return tracks, clips, automations, muted/frozen/unnamed/duplicate
                       tracks, device counts, disabled devices.
    EXTERNAL PLUGINS — alphabetical list of all external plugins used.
    FULL DEVICE LIST — nested device tree per track with on/off and automation counts.
    """
    TRACK_TYPES = {
        'MidiTrack': 'MIDI', 'AudioTrack': 'Audio', 'ReturnTrack': 'Return',
        'GroupTrack': 'Group', 'MasterTrack': 'Master', 'MainTrack': 'Master',
    }

    def collect_devices(element, depth=0):
        results = []
        for child in element:
            if child.tag == 'Devices':
                for device in child:
                    block = ET.tostring(device, encoding='unicode')
                    if '<On>' in block and '<Manual Value=' in block:
                        results.append((depth, device.tag, device))
                        results.extend(collect_devices(device, depth + 1))
            else:
                results.extend(collect_devices(child, depth))
        return results

    def is_enabled(el):
        manual = el.find('On/Manual')
        return manual is None or manual.get('Value', 'true').lower() == 'true'

    def count_automation(el, auto_ids):
        targets = {t.get('Id') for t in el.findall('.//AutomationTarget')}
        return len(targets & auto_ids)

    root     = ET.parse(io.StringIO(xml_text)).getroot()
    auto_ids = {el.get('Value') for el in root.findall('.//PointeeId')}

    # Collect all tracks in true document order
    TRACK_TAGS = set(TRACK_TYPES.keys())
    tracks_el  = root.find('.//Tracks')
    all_tracks = [(c.tag, c) for c in (tracks_el or []) if c.tag in TRACK_TAGS]
    for tag in ('MasterTrack', 'MainTrack'):
        master = root.find(f'.//{tag}')
        if master is not None:
            all_tracks.append((tag, master))
            break
    non_master_tracks = [(tag, t) for tag, t in all_tracks if tag not in ('MasterTrack', 'MainTrack')]

    # Fetch duplicate/unnamed counts        
    track_names    = [t.find('.//Name/EffectiveName').get('Value', '') 
                      for _, t in all_tracks 
                      if t.find('.//Name/EffectiveName') is not None]
    duplicate_names = {n: track_names.count(n) for n in set(track_names) if n and track_names.count(n) > 1}
    duplicate_count = len(duplicate_names)
    unnamed_count   = sum(1 for n in track_names if not n or re.match(r'^\d+-', n))

    # Build group hierarchy and numbering
    group_ids, track_numbers = {}, {}
    for idx, (tag, t) in enumerate(all_tracks, 1):
        xid = t.get('Id', '')
        gid = next((child.get('Value', '-1') for child in t if child.tag == 'TrackGroupId'), '-1')
        if xid:
            group_ids[xid]     = gid
            track_numbers[xid] = idx

    def get_depth(xid):
        depth, gid = 0, group_ids.get(xid, '-1')
        while gid and gid != '-1' and gid in group_ids:
            depth += 1
            gid = group_ids.get(gid, '-1')
        return depth

    # ── Gather summary stats ──────────────────────────────────────────────────
    tempo_el = root.find('.//Tempo/Manual')
    tempo    = tempo_el.get('Value', '?') if tempo_el is not None else '?'

    creator_m = re.search(r'<Ableton\b[^>]*\bCreator="([^"]+)"', xml_text[:500])
    creator   = creator_m.group(1) if creator_m else '?'

    type_counts = {}
    for tag, _ in all_tracks:
        label = TRACK_TYPES.get(tag, tag)
        type_counts[label] = type_counts.get(label, 0) + 1

    # Collect all device data once — reused for ext_plugins, device counts and device tree
    track_devices = {i: collect_devices(t) for i, (_, t) in enumerate(all_tracks)}

    # Collect all external plugin names across the project
    ext_plugins = {}
    for i, (tag, track) in enumerate(all_tracks):
        for _, dtag, el in track_devices[i]:
            if dtag in ('PluginDevice', 'AuPluginDevice'):
                block  = ET.tostring(el, encoding='unicode')
                name_d = extract_device_name(block, dtag)
                if name_d:
                    bare = name_d.replace('[ext] ', '')
                    ext_plugins[bare] = ext_plugins.get(bare, 0) + 1

    frozen_count = sum(
        1 for _, t in all_tracks
        if any(child.tag == 'Freeze' and child.get('Value') == 'true' for child in t)
    )

    automation_count = len(set(re.findall(r'<PointeeId\s+Value="(\d+)"', xml_text)))

    muted_count = 0
    for _, t in all_tracks:
        mixer = t.find('.//Mixer')
        if mixer is None:
            continue
        speaker = mixer.find('Speaker')
        if speaker is None:
            continue
        manual = speaker.find('Manual')
        if manual is not None and manual.get('Value') == 'false':
            muted_count += 1

    int_devices, ext_devices, disabled_devices = 0, 0, 0
    for i, (_, track) in enumerate(all_tracks):
        for _, dtag, el in track_devices[i]:
            if dtag in ('PluginDevice', 'AuPluginDevice'):
                ext_devices += 1
            else:
                int_devices += 1
            if not is_enabled(el):
                disabled_devices += 1

    midi_clips  = len(re.findall(r'<MidiClip\b', xml_text))
    audio_clips = len(re.findall(r'<AudioClip\b', xml_text))

    return_names = [
        t.find('.//Name/EffectiveName').get('Value', '')
        for tag, t in all_tracks
        if tag == 'ReturnTrack' and t.find('.//Name/EffectiveName') is not None
    ]

    # Time signature
    ts_num = root.find('.//TimeSignature/TimeSignatures/AutomationEvent')
    time_sig = '4/4'
    if ts_num is not None:
        num = ts_num.get('Numerator', '4')
        den = ts_num.get('Denominator', '4')
        time_sig = f'{num}/{den}'

    # Locators
    locators = []
    for el in root.findall('.//Locators/Locator'):
        name_el = el.find('Name')
        name    = name_el.get('Value', '') if name_el is not None else el.get('Name', '')
        if name:
            locators.append(name)

    # ── Build report lines ────────────────────────────────────────────────────
    lines = []

    # ── PROJECT SUMMARY ──────────────────────────────────────────────────────
    lines.append('═' * 60)
    lines.append(f'  PROJECT SUMMARY')
    lines.append('═' * 60)
    W = 17  # fixed label width — adjust this single value to shift all colons together
    lines.append(f'  {"Creator":<{W}}: {creator}')
    lines.append(f'  {"BPM":<{W}}: {float(tempo):.2f}')
    lines.append(f'  {"Time signature":<{W}}: {time_sig}')
    if locators:
            pad          = ' ' * (W + 10)
            max_width    = 80 - len(pad)
            lines_out, current = [], ''
            for loc in locators:
                test = f'{current}, {loc}' if current else loc
                if current and len(test) > max_width:
                    lines_out.append(current + ',')
                    current = loc
                else:
                    current = test
            lines_out.append(current)
            lines.append(f'  {"Locators":<{W}}: {len(locators)}   ({lines_out[0]}')
            for l in lines_out[1:]:
                lines.append(f'{pad}{l}')
            lines[-1] += ')'

    lines.append('')
    lines.append(f'  {"Total tracks":<{W}}: {len(non_master_tracks)}')
    for label in ['Group', 'Audio', 'MIDI', 'Return']:
        count = type_counts.get(label, 0)
        if count:
            lines.append(f'    {label:<{W-2}}: {count}')

    if return_names:
        lines.append('')
        lines.append(f'  {"Return tracks":<{W}}: {len(return_names)}')
        for name in return_names:
            lines.append(f'    {name:<{W-2}}')

    lines.append('')
    lines.append(f'  {"Clips":<{W}}: {midi_clips} MIDI / {audio_clips} Audio')
    lines.append(f'  {"Automations":<{W}}: {automation_count}')

    if frozen_count:
        lines.append(f'  {"Frozen tracks":<{W}}: {frozen_count}')
    if muted_count:
        lines.append(f'  {"Muted tracks":<{W}}: {muted_count}')
    if unnamed_count:
        lines.append(f'  {"Unnamed tracks":<{W}}: {unnamed_count}') 
    if duplicate_count:
            names_sorted = sorted(duplicate_names.items(), key=lambda x: x[0])
            name_strs    = [f"'{n}'x{c}" for n, c in names_sorted]
            pad          = ' ' * (W + 10)
            max_width    = 80 - len(pad)
            lines_out, current = [], ''
            for name in name_strs:
                test = f'{current}, {name}' if current else name
                if current and len(test) > max_width:
                    lines_out.append(current + ',')
                    current = name
                else:
                    current = test
            lines_out.append(current)
            lines.append(f'  {"Duplicate names":<{W}}: {duplicate_count}   ({lines_out[0]}')
            for l in lines_out[1:]:
                lines.append(f'{pad}{l}')
            lines[-1] += ')'

    lines.append('')
    lines.append(f'  {"Total devices":<{W}}: {int_devices + ext_devices}')
    lines.append(f'    {"Native":<{W-2}}: {int_devices}')
    lines.append(f'    {"External":<{W-2}}: {ext_devices}')
    if disabled_devices:
        lines.append('')
        lines.append(f'  {"Disabled devices":<{W}}: {disabled_devices}')

    lines.append('')

    # ── EXTERNAL PLUGINS ──────────────────────────────────────────────────────
    lines.append('═' * 60)
    lines.append(f'  EXTERNAL PLUGINS')
    lines.append('═' * 60)
    for name in sorted(ext_plugins.keys(), key=lambda x: x.lower()):
        lines.append(f'    {name}')
    lines.append('')

    # ── FULL DEVICE LIST ──────────────────────────────────────────────────────
    lines.append('═' * 60)
    lines.append('  FULL DEVICE LIST')
    lines.append('═' * 60)

    for i, (tag, track) in enumerate(all_tracks):
        devices  = track_devices[i]
        if not devices:
            continue
        name_el  = track.find('.//Name/EffectiveName')
        eff_name = name_el.get('Value', track.tag) if name_el is not None else track.tag
        xid      = track.get('Id', '')
        t_indent = '  ' * get_depth(xid)
        num      = '' if tag in ('MasterTrack', 'MainTrack') else f'#{track_numbers.get(xid, 0):02d} '
        lines.append('')
        lines.append(f'{t_indent}  [{TRACK_TYPES.get(tag, tag)}] {num}{eff_name}')
        lines.append(f'{t_indent}  {"─" * 40}')
        for depth, dtag, el in devices:
            indent     = t_indent + '    ' + ('  ' * depth)
            block      = ET.tostring(el, encoding='unicode')
            name_d     = extract_device_name(block, dtag) or f'[int] {dtag}'
            state      = '' if is_enabled(el) else ' [Off]'
            auto_count = count_automation(el, auto_ids)
            auto       = f' [Auto:{auto_count}]' if auto_count else ''
            lines.append(f'{indent}{name_d}{state}{auto}')

    lines.append('')
    lines.append('═' * 60)

    # ── Save report to txt ────────────────────────────────────────────────────
    als_path  = context.get('als_path')
    out_path  = als_path.parent / f'{als_path.stem}_report.txt'
    out_path.write_text('\n'.join(lines), encoding='utf-8')

    # ── Short terminal summary ────────────────────────────────────────────────
    print(f'\n  {"═" * 96}')

    print(f'  {creator}')
    print(f'  BPM {float(tempo):.2f}  |  Time sig {time_sig}')
    print()

    track_parts = ' | '.join(f'{type_counts[label]} {label}' for label in ['Group', 'Audio', 'MIDI', 'Return'] if label in type_counts)
    TW = 12  # fixed label width for terminal summary — adjust to align all colons
    frozen_str = f'  [{frozen_count} Frozen]' if frozen_count else ''
    print(f'  {"Tracks":<{TW}}: {len(non_master_tracks)} total  ({track_parts}){frozen_str}')
    print(f'  {"Clips":<{TW}}: {midi_clips + audio_clips} total  ({midi_clips} MIDI | {audio_clips} Audio)')
    print(f'  {"Devices":<{TW}}: {int_devices + ext_devices} total  ({int_devices} Native | {ext_devices} External)  [{len(ext_plugins)} unique external plugins]')
    print(f'  {"Automations":<{TW}}: {automation_count}')

    flags = []
    if muted_count:      flags.append(f'{muted_count} muted tracks')
    if unnamed_count:    flags.append(f'{unnamed_count} unnamed tracks')
    if duplicate_count:  flags.append(f'{duplicate_count} duplicate track names')
    if disabled_devices: flags.append(f'{disabled_devices} disabled devices')
    if flags:
        print(f'\n  ⚠  {",  ".join(flags)}')

    print(f'  {"═" * 96}\n')

    context['_report_written'] = True
    return xml_text, [f'Report saved → {out_path.name}']


def step_remove_unused_return_tracks(xml_text: str, context: dict) -> tuple:
    """
    A return track is considered unused if NO source track (Audio, MIDI, Group)
    has an active send routed to it with a value above Ableton's minimum.

    Specifically, a return track at index N is removed if every TrackSendHolder
    with Id="N" across all source tracks meets at least one of these conditions:
      - <Active Value="false" />  — the send is muted/disabled
      - <Manual Value="0.0003162277571" /> — Ableton's -inf dB (knob fully left)

    When a return track is removed, the function also:
      1. Removes ALL TrackSendHolder blocks not pointing to a surviving return track.
         This includes both the removed return's holders AND any pre-existing orphaned
         holders from return tracks deleted outside this script.
      2. Re-indexes remaining TrackSendHolder Ids to be sequential (0, 1, 2...)
         so Ableton's send count matches the remaining return track count exactly.
    """
    MINUS_INF = 0.0003162277571

    return_tracks = []
    for start, end, content in find_blocks(xml_text, "ReturnTrack"):
        m = re.search(r'<(?:UserName|EffectiveName)\s+Value="([^"]+)"', content)
        return_tracks.append({"start": start, "end": end, "name": m.group(1) if m else "Return"})

    if not return_tracks:
        return xml_text, ["No return tracks found."]

    # Find which return indices have at least one active non-zero send
    active = set()
    for tag in ["AudioTrack", "MidiTrack", "GroupTrack"]:
        for _, _, track in find_blocks(xml_text, tag):
            for _, _, holder in find_blocks(track, "TrackSendHolder"):
                m_id  = re.search(r'Id="(\d+)"', holder)
                m_val = re.search(r'<Manual\s+Value="([^"]+)"', holder)
                m_on  = re.search(r'<Active\s+Value="(true|false)"', holder)
                if not m_id:
                    continue
                idx      = int(m_id.group(1))
                val      = float(m_val.group(1)) if m_val else 0.0
                is_on    = (m_on.group(1) == "true") if m_on else True
                if is_on and val > MINUS_INF:
                    active.add(idx)

    remove = {i for i in range(len(return_tracks)) if i not in active}
    if not remove:
        return xml_text, ["No unused return tracks found."]

    # Remove ReturnTrack blocks
    xml_text = splice_out(xml_text, [return_tracks[i] for i in remove])

    kept = {i for i in range(len(return_tracks)) if i not in remove}

    holders = []
    for tag in ["AudioTrack", "MidiTrack", "GroupTrack", "ReturnTrack"]:
        for t_start, _, t_content in find_blocks(xml_text, tag):
            for h_rel_start, h_rel_end, c in find_blocks(t_content, "TrackSendHolder"):
                m = re.search(r'Id="(\d+)"', c)
                if m and int(m.group(1)) not in kept: # remove orphans too
                    holders.append({"start": t_start + h_rel_start, "end": t_start + h_rel_end})

    xml_text = splice_out(xml_text, holders)

    # Re-index remaining TrackSendHolder Ids sequentially
    remap     = {old: new for new, old in enumerate(i for i in range(len(return_tracks)) if i not in remove)}
    xml_text  = re.sub(
        r'<TrackSendHolder\s+Id="(\d+)"',
        lambda m: f'<TrackSendHolder Id="{remap.get(int(m.group(1)), int(m.group(1)))}"',
        xml_text
    )

    log = [f"  Removed unused return track '{return_tracks[i]['name']}'" for i in remove]

    return xml_text, log


def step_remove_disabled_devices(xml_text: str, context: dict) -> tuple:
    """
    Remove any device (native or external) that has been disabled in Ableton.

    A device is considered disabled if its <On> block contains: <Manual Value="false" />
    This covers all device types — native Ableton devices (EQ, Compressor, Reverb etc.)
    and external VST devices alike.

    Guards:
      - Always preserve the first device in each chain — if the sound source is
        disabled it will be logged but not removed, so the producer can see it.
      - Always keep at least one enabled StereoGain on Master as template for convert step.
      - Never remove a device whose On/Off is automated — it's intentionally toggled.
    """
    automated_ids = set(re.findall(r'<PointeeId\s+Value="(\d+)"', xml_text))
    tracks = get_track_ranges(xml_text)
    to_remove = []
    master_stereo_gain_kept = False

    for start, end, content, tag in find_all_devices(xml_text):
        on_blocks = find_blocks(content, "On")
        if not on_blocks:
            continue
        _, _, on_content = on_blocks[0]
        is_enabled = not re.search(r'<Manual\s+Value="false"', on_content)

        track = track_of(start, tracks)
        if track == "Unknown":
            continue
        name = extract_device_name(content, tag) or tag

        # Guard — keep first enabled StereoGain on Master as convert template
        if tag == "StereoGain" and track == "Master" and is_enabled and not master_stereo_gain_kept:
            master_stereo_gain_kept = True
            continue

        if is_enabled:
            continue

        # Guard — skip if On/Off is automated (device is intentionally toggled)
        on_target = re.search(r'<AutomationTarget\s+Id="(\d+)"', on_content)
        if on_target and on_target.group(1) in automated_ids:
            continue

        to_remove.append({"start": start, "end": end, "name": name, "track": track})

    # Build a lookup of the true first device (lowest byte offset) per track
    # so we can protect it regardless of where it lands in the removals list
    first_device_start = {}
    for s, _, _, _ in find_all_devices(xml_text):
        track = track_of(s, tracks)
        if track not in first_device_start or s < first_device_start[track]:
            first_device_start[track] = s

    by_track = defaultdict(list)
    for r in to_remove:
        by_track[r["track"]].append(r)

    safe_to_remove = []
    for track_name, removals in by_track.items():
        # Skip the first device in the chain — log it but never remove it
        safe_to_remove.extend(
            r for r in removals
            if r["start"] != first_device_start.get(track_name)
        )
    to_remove = safe_to_remove

    if not to_remove:
        return xml_text, ["No disabled devices found."]

    log = []
    for r in to_remove:
        parts     = r['track'].split(' ', 1)
        track_str = f"{parts[0]} '{parts[1]}'" if len(parts) == 2 else f"'{r['track']}'"
        name_bare = r['name'].split('] ', 1)
        tag_str   = name_bare[0] + ']' if len(name_bare) == 2 else ''
        dev_name  = name_bare[1] if len(name_bare) == 2 else r['name']
        log.append(f"  Removed disabled {tag_str} '{dev_name}' on track {track_str}")

    return splice_out(xml_text, to_remove), log


def step_remove_non_automated_devices(xml_text: str, context: dict) -> tuple:
    """
    Remove insert devices that have no automation anywhere in the project.

    How automation detection works:
      Every automatable parameter inside a device contains an
      <AutomationTarget Id="N"> node. The project's <AutomationEnvelopes>
      sections store <PointeeId Value="N"/> for every parameter that actually
      has automation data. A device is considered automated if at least one
      of its AutomationTarget Ids appears as a PointeeId anywhere in the project.
      If none match, the device has no automation and is safe to remove.

    Guards:
      - Always preserve the first device in each chain — it's the sound source.
        Checked by byte offset, not removals index, so middle-chain devices
        without automation are still correctly removed.
      - Never fully empty a track's device chain — Ableton drops track-level
        automation (volume, pan, sends) on load if <Devices> is completely empty.
      - Master track: always preserve one StereoGain (Utility) as convert template.
    """
    automated_ids = set(re.findall(r'<PointeeId\s+Value="(\d+)"', xml_text))

    tracks    = get_track_ranges(xml_text)
    to_remove = []

    for start, end, content, tag in find_all_devices(xml_text):
        device_target_ids = set(re.findall(r'<(?:Automation|Modulation)Target\s+Id="(\d+)"', content))
        if not device_target_ids.isdisjoint(automated_ids):
            continue  # has automation — keep it

        track = track_of(start, tracks)
        if track == "Unknown":
            continue

        name = extract_device_name(content, tag) or tag
        to_remove.append({"start": start, "end": end, "name": name, "track": track, "tag": tag})

    # Build a lookup of the true first device (lowest byte offset) per track
    # so we can protect it regardless of where it lands in the removals list
    first_device_start = {}
    for s, _, _, _ in find_all_devices(xml_text):
        track = track_of(s, tracks)
        if track not in first_device_start or s < first_device_start[track]:
            first_device_start[track] = s

    by_track = defaultdict(list)
    for r in to_remove:
        by_track[r["track"]].append(r)

    safe_to_remove = []
    for track_name, removals in by_track.items():
        removals_sorted = sorted(removals, key=lambda r: r["start"])

        if track_name == "Master":
            # Always keep the first StereoGain on Master as template for the convert step
            sg_idx = next(
                (i for i, r in enumerate(removals_sorted) if r["tag"] == "StereoGain"), None
            )
            if sg_idx is not None:
                removals_sorted = [r for i, r in enumerate(removals_sorted) if i != sg_idx]

        # Skip the first device in the chain — it's the sound source and must never be removed
        safe_to_remove.extend(
            r for r in removals_sorted
            if r["start"] != first_device_start.get(track_name)
        )

    to_remove = safe_to_remove

    if not to_remove:
        return xml_text, ["No non-automated devices found."]
    
    log = []
    for r in to_remove:
        parts     = r['track'].split(' ', 1)
        track_str = f"{parts[0]} '{parts[1]}'" if len(parts) == 2 else f"'{r['track']}'"
        name_bare = r['name'].split('] ', 1)
        tag_str   = name_bare[0] + ']' if len(name_bare) == 2 else ''
        dev_name  = name_bare[1] if len(name_bare) == 2 else r['name']
        log.append(f"  Removed non-automated {tag_str} '{dev_name}' on track {track_str}")

    return splice_out(xml_text, to_remove), log


def step_convert_mixer_automation_to_utility(xml_text: str, context: dict) -> tuple:
    """
    Convert Mixer Volume/Pan automation into a Utility (StereoGain) insert device.

    For each track with active Volume or Pan automation on its Mixer:
      1. Clones an existing StereoGain (Utility) from the project as a template
      2. Appends the cloned Utility to the end of the track's device chain
      3. AutomationEnvelope PointeeIds are remapped to the Utility's parameters:
         - Volume → Gain     (no conversion — dB values transfer as-is;
                              Mixer's +6 dB max fits within Utility Gain's range -69 to +35 db)
         - Pan    → Balance  (no conversion — both use range -1.0 to +1.0)

    GroupTracks with empty device chains use self-closing <Devices /> in the XML —
    a pre-pass expands these to <Devices></Devices> before any offset calculations.

    Return and Master tracks are included by default. To exclude either, set
    exclude_conversion_types in config to RTN, MST or both.
    """
    automated_ids = set(re.findall(r'<PointeeId\s+Value="(\d+)"', xml_text))

    if not automated_ids:
        return xml_text, ["No automation found in project."]

    template_blocks = find_blocks(xml_text, "StereoGain")
    if not template_blocks:
        return xml_text, ["No existing Utility (StereoGain) found to clone — cannot insert."]

    _, _, template = template_blocks[0]

    # ── Pre-pass: expand self-closing <Devices /> to <Devices></Devices> ──────
    # GroupTracks with no devices use self-closing form — inserting into them
    # would corrupt the XML without this expansion first
    xml_text = re.sub(r'<Devices\s*/>', '<Devices></Devices>', xml_text)

    # Track types excluded from conversion, mapped from config shorthands to XML tags
    _TYPE_MAP     = {'RTN': {'ReturnTrack'}, 'MST': {'MasterTrack', 'MainTrack'}}
    raw = context.get('exclude_conversion_types', [])
    exclude_types = set(raw if isinstance(raw, list) else [raw] if raw else [])

    # ── Collect work items ──────────────────────────────────────────────────────
    work_items = []
    for track_tag in ["AudioTrack", "MidiTrack", "ReturnTrack", "GroupTrack", "MasterTrack", "MainTrack"]:
        for t_start, _, t_content in find_blocks(xml_text, track_tag):

            # Skip track types excluded in config (Return or/and Master tracks)
            if any(track_tag in _TYPE_MAP.get(e, set()) for e in exclude_types):
                continue

            track_name = track_of(t_start, get_track_ranges(xml_text))

            mixer_blocks = find_blocks(t_content, "Mixer")
            if not mixer_blocks:
                continue
            _, _, mixer = mixer_blocks[0]

            vol_id = pan_id = None
            for param, attr in [("Volume", "vol_id"), ("Pan", "pan_id")]:
                pb = find_blocks(mixer, param)
                if pb:
                    m = re.search(r'<AutomationTarget\s+Id="(\d+)"', pb[0][2])
                    if m and m.group(1) in automated_ids:
                        if attr == "vol_id": vol_id = m.group(1)
                        else:                pan_id = m.group(1)

            if not vol_id and not pan_id:
                continue

            dev_blocks = find_blocks(t_content, "Devices")
            if not dev_blocks:
                continue
            dev_rel_start, dev_rel_end, _ = dev_blocks[0]
            closing_tag = "</Devices>"
            insert_pos  = t_start + dev_rel_end - len(closing_tag)
            work_items.append((insert_pos, vol_id, pan_id, track_name))

    if not work_items:
        return xml_text, ["No Mixer Volume/Pan automation found."]

    # ── PHASE 1: build all clones & collect remaps (no xml_text mutation yet) ──
    id_count   = len(re.findall(r'Id="\d+"', template))
    base       = max((int(i) for i in re.findall(r'Id="(\d+)"', xml_text)), default=0) + 1
    insertions = []
    remaps     = []
    log        = []

    for insert_pos, vol_id, pan_id, track_name in work_items:
        counter = iter(range(base, base + id_count))
        cloned  = re.sub(r'(?<=Id=")\d+(?=")', lambda _: str(next(counter)), template)
        cloned  = re.sub(r'(<Gain>.*?<Manual\s+Value=")[^"]+(")',    r'\g<1>0\g<2>', cloned, flags=re.DOTALL)
        cloned  = re.sub(r'(<Balance>.*?<Manual\s+Value=")[^"]+(")', r'\g<1>0\g<2>', cloned, flags=re.DOTALL)
        cloned  = re.sub(r'(<UserName\s+Value=")[^"]+(")', r'\g<1>Track Automation\g<2>', cloned)

        gain_blocks    = find_blocks(cloned, "Gain")
        balance_blocks = find_blocks(cloned, "Balance")
        if not gain_blocks or not balance_blocks:
            log.append(f"  Skipped '{track_name}' — could not find Gain/Balance in cloned Utility")
            base += id_count
            continue

        gain_id_m    = re.search(r'<AutomationTarget\s+Id="(\d+)"', gain_blocks[0][2])
        new_pan_id_m = re.search(r'<AutomationTarget\s+Id="(\d+)"', balance_blocks[0][2])
        if not gain_id_m or not new_pan_id_m:
            log.append(f"  Skipped '{track_name}' — could not extract Gain/Balance AutomationTarget Ids")
            base += id_count
            continue

        insertions.append((insert_pos, cloned))
        if vol_id:
            remaps.append((vol_id, gain_id_m.group(1)))
            parts     = track_name.split(' ', 1)
            track_str = f"{parts[0]} '{parts[1]}'" if len(parts) == 2 else f"'{track_name}'"
            log.append(f"  Moved Volume automation → Utility Gain on {track_str}")
        if pan_id:
            remaps.append((pan_id, new_pan_id_m.group(1)))
            parts     = track_name.split(' ', 1)
            track_str = f"{parts[0]} '{parts[1]}'" if len(parts) == 2 else f"'{track_name}'"
            log.append(f"  Moved Pan automation → Utility Balance on {track_str}")

        base += id_count + 1

    # ── PHASE 2: single-pass string rebuild (forward order, no offset drift) ──
    parts, prev = [], 0
    for pos, cloned in sorted(insertions):
        parts.append(xml_text[prev:pos])
        parts.append("\n" + cloned + "\n")
        prev = pos
    parts.append(xml_text[prev:])
    xml_text = "".join(parts)

    # ── PHASE 3: apply all PointeeId remaps ────────────────────────────────────
    for old_id, new_id in remaps:
        xml_text = xml_text.replace(f'<PointeeId Value="{old_id}"', f'<PointeeId Value="{new_id}"')

    xml_text = update_next_pointee_id(xml_text)

    return xml_text, log


def step_remove_empty_tracks(xml_text: str, context: dict) -> tuple:
    """
    Remove tracks that have no clips anywhere — no MIDI clips, no audio clips,
    no Session View clips. Checks AudioTrack and MidiTrack only.
    GroupTracks, ReturnTracks and MasterTrack are never removed.
    """
    to_remove = []

    tracks = get_track_ranges(xml_text)
    for track_tag in ["AudioTrack", "MidiTrack"]:
        for t_start, t_end, t_content in find_blocks(xml_text, track_tag):
            has_midi  = bool(re.search(r'<MidiClip\s',  t_content))
            has_audio = bool(re.search(r'<AudioClip\s', t_content))
            if not has_midi and not has_audio:
                to_remove.append({"start": t_start, "end": t_end, "name": track_of(t_start, tracks)})

    if not to_remove:
        return xml_text, ["No empty tracks found."]
    
    # Guard — never remove all tracks, always keep at least 1 (the first in document order)
    all_track_starts = [s for s, _, _ in find_blocks(xml_text, "AudioTrack")] + \
                       [s for s, _, _ in find_blocks(xml_text, "MidiTrack")]
    if len(to_remove) >= len(all_track_starts):
        first_start = min(r['start'] for r in to_remove)
        to_remove   = [r for r in to_remove if r['start'] != first_start]

    group_id_to_name = {}
    for s, e, c in find_blocks(xml_text, "GroupTrack"):
        gid = re.search(r'<GroupTrack\s+Id="(\d+)"', c)
        if gid:
            group_id_to_name[gid.group(1)] = track_of(s, tracks)

    log = [f"  Removed empty track {r['name'].split(' ', 1)[0]} '{r['name'].split(' ', 1)[1]}'" for r in to_remove]
    xml_text = splice_out(xml_text, to_remove)

    for _ in range(10):
        group_ids_in_use = set(re.findall(r'<TrackGroupId\s+Value="(\d+)"', xml_text))
        empty_groups     = []
        for s, e, c in find_blocks(xml_text, "GroupTrack"):
            gid = re.search(r'<GroupTrack\s+Id="(\d+)"', c)
            if gid and gid.group(1) not in group_ids_in_use:
                empty_groups.append({"start": s, "end": e, "gid": gid.group(1)})
        if not empty_groups:
            break
        for r in empty_groups:
            name  = group_id_to_name.get(r['gid'], 'Group')
            parts = name.split(' ', 1)
            log.append(f"  Removed empty group {parts[0]} '{parts[1]}'" if len(parts) == 2 else f"  Removed empty group '{name}'")
        xml_text = splice_out(xml_text, empty_groups)

    return xml_text, log


def step_remove_muted_tracks(xml_text: str, context: dict) -> tuple:
    """
    Remove tracks that are muted/deactivated.
    Mute state is stored as <Manual Value="false"/> inside the track's Mixer <Speaker> block.
    Checks AudioTrack and MidiTrack only — ReturnTrack and MasterTrack are never removed.
    """
    to_remove = []

    tracks = get_track_ranges(xml_text)
    for track_tag in ["AudioTrack", "MidiTrack"]:
        for t_start, t_end, t_content in find_blocks(xml_text, track_tag):
            mixer_blocks = find_blocks(t_content, "Mixer")
            if not mixer_blocks:
                continue
            _, _, mixer = mixer_blocks[0]
            speaker_blocks = find_blocks(mixer, "Speaker")
            if not speaker_blocks:
                continue
            _, _, speaker = speaker_blocks[0]
            if not re.search(r'<Manual\s+Value="false"', speaker):
                continue
            to_remove.append({"start": t_start, "end": t_end, "name": track_of(t_start, tracks)})

    if not to_remove:
        return xml_text, ["No muted tracks found."]
    
    # Guard — never remove all tracks, always keep at least 1 (the first in document order)
    all_track_starts = [s for s, _, _ in find_blocks(xml_text, "AudioTrack")] + \
                       [s for s, _, _ in find_blocks(xml_text, "MidiTrack")]
    if len(to_remove) >= len(all_track_starts):
        first_start = min(r['start'] for r in to_remove)
        to_remove   = [r for r in to_remove if r['start'] != first_start]

    group_id_to_name = {}
    for s, e, c in find_blocks(xml_text, "GroupTrack"):
        gid = re.search(r'<GroupTrack\s+Id="(\d+)"', c)
        if gid:
            group_id_to_name[gid.group(1)] = track_of(s, tracks)

    log = [f"  Removed muted track {r['name'].split(' ', 1)[0]} '{r['name'].split(' ', 1)[1]}'" for r in to_remove]
    xml_text = splice_out(xml_text, to_remove)

    for _ in range(10):
        group_ids_in_use = set(re.findall(r'<TrackGroupId\s+Value="(\d+)"', xml_text))
        empty_groups     = []
        for s, e, c in find_blocks(xml_text, "GroupTrack"):
            gid = re.search(r'<GroupTrack\s+Id="(\d+)"', c)
            if gid and gid.group(1) not in group_ids_in_use:
                empty_groups.append({"start": s, "end": e, "gid": gid.group(1)})
        if not empty_groups:
            break
        for r in empty_groups:
            name  = group_id_to_name.get(r['gid'], 'Group')
            parts = name.split(' ', 1)
            log.append(f"  Removed empty group {parts[0]} '{parts[1]}'" if len(parts) == 2 else f"  Removed empty group '{name}'")
        xml_text = splice_out(xml_text, empty_groups)

    return xml_text, log


def step_sort_color_tracks(xml_text: str, context: dict) -> tuple:
    """
    Sort tracks and set colors based on 2-letter prefixes in track names.

    Groups and their children are treated as atomic units during sorting.
    Children are sorted by their own prefix order within each group.
    Children without a recognized prefix inherit their parent group's color.
    """

    track_config = context['track_config']

    tracks = get_track_info(xml_text)
    if len(tracks) < 2:
        return xml_text, ["Less than 2 tracks — nothing to sort"]

    # Assign prefix/sort/color to every track — Return/Master by track type,
    # wide groups by exact uppercase first word, individual tracks by exact 2-letter prefix
    for track in tracks:

        if track['type'] == 'ReturnTrack':
            prefix = 'RTN'
        elif track['type'] in ('MasterTrack', 'MainTrack'):
            prefix = 'MST'
        else:
            raw_name = re.sub(r'^#\d+\s+', '', track['name']) # strip #XX numbering before prefix matching
            first_word = raw_name.split()[0] if raw_name.split() else "??"
            if first_word.isupper() and first_word in track_config:
                prefix = first_word
            else:
                prefix = first_word[:2] if len(first_word) >= 2 else "??"

        config              = track_config.get(prefix, track_config["DEF"])
        track['prefix']     = prefix
        track['sort_order'] = config['sort']
        track['color']      = config['color']

    # Helper: extract XML Id and TrackGroupId from track content
    def get_xml_id(content):
        m = re.search(r'<(?:AudioTrack|MidiTrack|GroupTrack|ReturnTrack|MasterTrack|MainTrack)\s+Id="(\d+)"', content)
        return m.group(1) if m else None

    def get_group_id(content):
        m = re.search(r'<TrackGroupId\s+Value="(\d+)"', content)
        return m.group(1) if m else None
    
    def collect_unit(track, position_ordered):
        """Recursively collect a group and all its descendants, sorted by prefix."""
        xid = get_xml_id(track['content'])
        children = []
        for t in position_ordered:
            if get_group_id(t['content']) == xid:
                if t['type'] == 'GroupTrack':
                    children.append(collect_unit(t, position_ordered))
                else:
                    children.append([t])
        children.sort(key=lambda u: u[0]['sort_order'])
        unit = [track]
        for child in children:
            unit.extend(child)
        return unit

    # Build Id → track map for parent lookups
    xml_id_map = {get_xml_id(t['content']): t for t in tracks if get_xml_id(t['content'])}

    # Children inherit sort order, color and prefix from their parent group
    for track in tracks:
        gid = get_group_id(track['content'])
        if gid and gid in xml_id_map:
            parent = xml_id_map[gid]
            if track['prefix'] not in track_config:     # only inherit color if no own prefix
                track['color'] = parent['color']

    # Sort tracks by document position (establishes slot boundaries)
    position_ordered = sorted(tracks, key=lambda t: t['start'])

    # Build atomic sort units — top-level groups recursively, standalone tracks as single units
    units, seen = [], set()
    for track in position_ordered:
        tid = get_xml_id(track['content'])
        if tid in seen:
            continue
        if track['type'] == 'GroupTrack' and get_group_id(track['content']) is None:
            unit = collect_unit(track, position_ordered)
            for t in unit:
                seen.add(get_xml_id(t['content']))
            units.append(unit)
        elif get_group_id(track['content']) is None:
            seen.add(tid)
            units.append([track])

    # Sort units by sort_order, flatten back to track list
    sorted_flat = [t for unit in sorted(units, key=lambda u: u[0]['sort_order']) for t in unit]

    # Skip if already in correct order
    if all(p['start'] == s['start'] for p, s in zip(position_ordered, sorted_flat)):
        return xml_text, ["Tracks already in correct order"]

    # Rebuild XML: place each track's content into its sorted slot
    parts, prev, log = [], 0, []
    for slot, content_track in zip(position_ordered, sorted_flat):
        content_track['content'] = set_track_color(content_track['content'], content_track['color'])
        if content_track['type'] not in ("ReturnTrack", "MasterTrack", "MainTrack"):
            content_track['content'] = set_clip_colors(content_track['content'], content_track['color'])
        parts.append(xml_text[prev:slot['start']])
        parts.append(content_track['content'])
        prev = slot['end']

        display_prefix = content_track['prefix'] if content_track['prefix'] in track_config else 'DEF'
        log.append(f"  [{display_prefix}] '{content_track['name']}' → color {content_track['color']}")

    parts.append(xml_text[prev:])
    new_xml = "".join(parts)

    return new_xml, log


def step_duplicate_device_chain(xml_text: str, context: dict) -> tuple:
    """
    Clone the device chain of every Audio and MIDI track into a new empty track
    inserted directly below the original. All clips and automation envelopes are
    stripped — only the device chain is kept.

    Master, Return and Group tracks are never duplicated.
    """
    suffix = context.get('chain_suffix', '')
    base = max((int(i) for i in re.findall(r'Id="(\d+)"', xml_text)), default=0) + 1
    insertions = []
    log = []

    tracks = get_track_ranges(xml_text)
    for track_tag in ["AudioTrack", "MidiTrack"]:
        for t_start, t_end, t_content in find_blocks(xml_text, track_tag):
            
            full_name = track_of(t_start, tracks)
            parts     = full_name.split(' ', 1)
            name      = parts[1] if len(parts) == 2 else full_name
            num       = parts[0] + ' ' if len(parts) == 2 else ''

            # Clone with fresh IDs
            id_count = len(re.findall(r'Id="\d+"', t_content))
            counter = iter(range(base, base + id_count))
            cloned = re.sub(r'(?<=Id=")\d+(?=")', lambda _: str(next(counter)), t_content)
            base += id_count + 1

            # Strip clips and automation
            cloned = re.sub(r'<MidiClip\b.*?</MidiClip>', '', cloned, flags=re.DOTALL)
            cloned = re.sub(r'<AudioClip\b.*?</AudioClip>', '', cloned, flags=re.DOTALL)
            cloned = re.sub(r'<AutomationEnvelope\b.*?</AutomationEnvelope>', '', cloned, flags=re.DOTALL)

            # Rename
            cloned = re.sub(r'(<EffectiveName\s+Value=")[^"]*(")', rf'\g<1>{name}{suffix}\g<2>', cloned, count=1)
            cloned = re.sub(r'(<UserName\s+Value=")[^"]*(")', rf'\g<1>{name}{suffix}\g<2>', cloned, count=1)

            insertions.append((t_end, cloned))
            log.append(f"  Duplicated device chain {num}'{name}' → '{name}{suffix}'")

    if not insertions:
        return xml_text, ["No tracks found to duplicate."]

    # Single-pass rebuild
    parts, prev = [], 0
    for pos, cloned in sorted(insertions):
        parts.append(xml_text[prev:pos])
        parts.append("\n" + cloned)
        prev = pos
    parts.append(xml_text[prev:])
    xml_text = "".join(parts)

    # Update NextPointeeId to be above the highest Id we assigned
    xml_text = update_next_pointee_id(xml_text)

    return xml_text, log


def step_quantize_midi_notes(xml_text: str, context: dict) -> tuple:
    """
    Quantize all MIDI note Time positions to 1/16 grid.

    Tracks whose prefix (or parent group prefix) matches exclude_midi_prefixes in config
    are skipped entirely and their MIDI notes are left untouched.
    """
    GRID = 0.25  # 1/16 = 0.25 beats
    count = 0

    def quantize(match):
        nonlocal count
        prefix = match.group(1)   # Everything before Time value
        time   = float(match.group(2))
        quantized = round(round(time / GRID) * GRID, 10)  # Double-round kills float errors
        count += 1
        return f'{prefix}"{quantized}"'

    # Build excluded byte ranges from prefixes specified in config exclude_midi_prefixes
    excluded = get_excluded_track_ranges(xml_text, context, context.get('track_config', {}))

    # Quantize MIDI note Time values, skipping excluded track byte ranges entirely
    new_xml = sub_outside_ranges(
        r'(<MidiNoteEvent\b[^>]*?Time=)"([\d.]+)"',
        quantize,
        xml_text,
        excluded
    )

    if count == 0:
        return xml_text, ["No MIDI notes found"]
    
    return new_xml, [f"Quantized {count} MIDI notes to 1/16"]


def step_set_track_heights(xml_text: str, context: dict) -> tuple:
    """Set all track lane heights to a consistent value (must be multiple of 17, 17–425)."""
    lane_height = context['lane_height']
    valid_heights = [n * 17 for n in range(1, 26)]  # 17 → 425  ← LOCAL ✅
    
    if lane_height not in valid_heights:
        return xml_text, [f"Invalid height {lane_height} — must be multiple of 17 (17–425)"]

    new_xml = re.sub(r'<LaneHeight\s+Value\s*=\s*"\d+"',
                     f'<LaneHeight Value="{lane_height}"', xml_text)
    
    return new_xml, [f"All track heights set to {lane_height}px"]


def step_ungroup_tracks(xml_text: str, context: dict) -> tuple:
    """
    Remove all GroupTrack blocks and restore every child track to ungrouped state.

    Handles both Live 11 ("AudioOut/Master") and Live 12 ("AudioOut/Main")
    by probing existing routing targets before making any changes.

    Steps:
        1. Delete all <GroupTrack> blocks
        2. Reset <TrackGroupId> → -1 on all child tracks
        3. Redirect "AudioOut/GroupTrack" / "MidiOut/GroupTrack" → detected master bus
        4. Fix adjacent <UpperDisplayString> to match — Live 12 crashes if they differ
    """

    group_blocks = find_blocks(xml_text, "GroupTrack")
    if not group_blocks:
        return xml_text, ["No group tracks found."]

    # Warn if find_blocks depth-counting drifted on a large file
    sorted_blocks = sorted(group_blocks, key=lambda x: x[0])
    for i in range(len(sorted_blocks) - 1):
        _, end_a, _ = sorted_blocks[i]
        start_b, _, _ = sorted_blocks[i + 1]
        if end_a > start_b:
            print(f"[WARNING] find_blocks overlap at blocks {i}/{i+1} — splice will be wrong.")

    # Detect Live version: Live 12 uses "AudioOut/Main", Live 11 uses "AudioOut/Master"
    existing = re.findall(r'<Target\s+Value="(AudioOut/[^"]+)"', xml_text)
    bus_label    = "Main" if any("Main" in t for t in existing) else "Master"
    target_audio = f"AudioOut/{bus_label}"
    target_midi  = f"MidiOut/{bus_label}"

    # 1. Collect and splice out all GroupTrack blocks
    tracks = get_track_ranges(xml_text)
    to_remove, log = [], []
    for start, end, content in group_blocks:
        track_name = track_of(start, tracks)
        to_remove.append({"start": start, "end": end})
        log.append(f"  Ungrouped '{track_name}'")

    xml_text = splice_out(xml_text, to_remove)

    # 2. Reset TrackGroupId — Ableton's sentinel for "not in a group" is -1
    xml_text = re.sub(r'<TrackGroupId\s+Value="-?\d+"', '<TrackGroupId Value="-1"', xml_text)

    # 3. Redirect group routing → master bus (generic strings, safe to replace globally)
    xml_text = xml_text.replace('"AudioOut/GroupTrack"', f'"{target_audio}"')
    xml_text = xml_text.replace('"MidiOut/GroupTrack"',  f'"{target_midi}"')

    # 4. Fix UpperDisplayString — must match Target or Live 12 fails routing validation
    #    Only replaces when UpperDisplayString immediately follows a Target we changed.
    #    re.DOTALL needed because newlines sit between the two tags.
    for target in (target_audio, target_midi):
        xml_text = re.sub(
            rf'(<Target\s+Value="{re.escape(target)}"[^>]*/>\s*<UpperDisplayString\s+Value=")Group"',
            rf'\g<1>{bus_label}"',
            xml_text,
            flags=re.DOTALL
        )

    return xml_text, log


def step_transpose_midi_notes(xml_text: str, context: dict) -> tuple:
    """
    Transpose all MIDI notes across the entire project by a fixed number of semitones.
    All tracks move by the exact same amount — global pitch relationships are preserved.
    The shift is capped by the project-wide min/max note so no note ever exceeds 0-127.
    Ableton stores pitch as <MidiKey Value="N" /> — one per KeyTrack (pitch lane).

    Tracks whose prefix (or parent group prefix) matches exclude_midi_prefixes in config
    are skipped entirely — both from the pitch range calculation and the transpose itself.
    """
    semitones = int(context.get("transpose_semitones", 0))
    if semitones == 0:
        return xml_text, ["Transpose = 0 — skipped"]

    # Build excluded byte ranges from prefixes specified in config exclude_midi_prefixes
    excluded = get_excluded_track_ranges(xml_text, context, context.get('track_config', {}))

    # ── PHASE 1: find pitch range across non-excluded tracks only ────────────
    if excluded:
        parts, prev = [], 0
        for start, end in sorted(excluded):
            parts.append(xml_text[prev:start])
            prev = end
        parts.append(xml_text[prev:])
        scan_xml = "".join(parts)
    else:
        scan_xml = xml_text
    all_pitches = [int(p) for p in re.findall(r'<MidiKey Value="(\d+)"', scan_xml)]

    if not all_pitches:
        return xml_text, ["No MIDI notes found"]

    if semitones > 0:
        actual = min(semitones, 127 - max(all_pitches))
    else:
        actual = max(semitones, 0 - min(all_pitches))

    if actual == 0:
        return xml_text, ["Cannot transpose — no headroom in requested direction"]

    # ── PHASE 2: apply uniform shift to every pitch lane in the project ──────
    new_xml = sub_outside_ranges(
        r'(<MidiKey Value=)"(\d+)"',
        lambda m: f'{m.group(1)}"{int(m.group(2)) + actual}"',
        xml_text,
        excluded
    )

    direction = f"+{actual}" if actual > 0 else str(actual)
    capped    = f" (capped from {'+' if semitones > 0 else ''}{semitones})" if actual != semitones else ""

    return new_xml, [f"Transposed {len(all_pitches)} pitch lanes by {direction} semitones{capped}"]


# ═════════════════════════════════════════════════════════════
# RUNNER
# ═════════════════════════════════════════════════════════════

def run_pipeline(als_path, context):
    """Run the full processing pipeline on a single .als file.
    Decompresses the gzip archive, runs all enabled steps in order,
    applies cleanup, validates the result and saves a _processed.als copy.
    """

    print(f"\n{als_path.name}\n" + '-' * 64)

    with gzip.open(als_path, "rb") as f:
        original = f.read().decode("utf-8")

    # ## XML VALIDATION of original file // DEBUG
    # original_errors = validate_xml(original)
    # if original_errors:
    #     print(f"  ⚠️  Original file has issues:")
    #     for e in original_errors:
    #         print(f"       {e}")

    xml = original

    context['als_path'] = als_path # save .als path for use in step functions

    for step_id, step_fn, description in PIPELINE:
        before   = xml
        xml, log = step_fn(xml, context)
        changed  = xml != before or context.pop('_report_written', False)
        print(f"\n  [{'✓' if changed else '—'}] {description}")
        for line in log:
            print(f"         {line.lstrip()}")

    if xml == original:
        print("\n  Already processed — nothing to fix.")
        return
    
    # Cleanup func — fixes safe pre-existing or step-induced issues before validation
    xml = cleanup_project(xml)
    
    ## XML VALIDATION of updated file
    errors = validate_xml(xml, original=original)
    if errors:
        print(f"\n  ⚠️  Validation failed — file NOT saved:")
        for e in errors:
            print(f"       {e}")
        return

    out_path = als_path.parent / f"{als_path.stem}_processed.als"
    with gzip.open(out_path, "wb") as f:
        f.write(xml.encode("utf-8"))
    print()
    print(f"  Saved processed version → {out_path}")  


# ═════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════

def main():
    """Entry point — loads config and settings, builds the pipeline, finds all .als files
    one subfolder deep next to the script, runs the pipeline on each, and compiles
    the global external plugins report if get_project_report is enabled.
    """

    print_main_header()

    try:
        config = load_config()
        settings = load_settings(config)

        context = {
            'dedupe_devices': settings.get('dedupe_devices', []),
            'exclude_conversion_types': settings.get('exclude_conversion_types', []),
            'chain_suffix': settings.get('duplicate_chain_suffix', '').strip("'\""),
            'exclude_midi_prefixes': settings.get('exclude_midi_prefixes', []),
            'transpose_semitones': settings.get('transpose_semitones', 0),
            'lane_height': settings.get('lane_height', 68),
            'track_config': load_track_config(config),
        }

        global PIPELINE # used by print_pipeline_info()
        PIPELINE = load_pipeline(config)

    except (FileNotFoundError, ValueError) as e:
        print(f"Config error: {e}")
        print()
        sys.exit(1)

    if not PIPELINE:
        print("No pipeline steps enabled — update config.ini and re-run.")
        print()
        sys.exit(0)

    root      = Path(__file__).parent
    als_files = find_als_files(root)
    if not als_files:
        print(f"No .als files found in subfolders of: {root}")
        sys.exit(0)

    print_pipeline_header()
    print_pipeline_info(root)
    print(f"Found {len(als_files)} .als file(s) in subfolders of: {root}\n")

    for als_path in als_files:
        run_pipeline(als_path, context)
        print()

    # ── Global external plugins list ──────────────────────────────────────
    plugin_projects = {}
    for als_path in als_files:
        report_path = als_path.parent / f'{als_path.stem}_report.txt'
        if not report_path.exists():
            continue
        text = report_path.read_text(encoding='utf-8')
        in_section = False
        sep_count = 0
        for line in text.splitlines():
            if 'EXTERNAL PLUGINS' in line:
                in_section = True
                continue
            if in_section and line.startswith('═'):
                sep_count += 1
                if sep_count == 2:  # second ═ = end of section
                    break
                continue
            if in_section and line.strip():
                plugin_projects.setdefault(line.strip(), set()).add(als_path.name)

    if plugin_projects:
        # Group plugins by which projects share them
        combo_plugins = {}
        for name, projects in plugin_projects.items():
            key = tuple(sorted(projects))
            combo_plugins.setdefault(key, []).append(name)

        out_lines = []

        # ── FULL LIST ─────────────────────────────────────────────────────
        out_lines += ['═' * 60, '  EXTERNAL PLUGINS — FULL LIST', '═' * 60, '']
        for name in sorted(plugin_projects.keys(), key=lambda x: x.lower()):
            out_lines.append(f'  {name}')
        out_lines.append('')

        # ── PER PROJECT ───────────────────────────────────────────────────
        out_lines += ['═' * 60, '  EXTERNAL PLUGINS — CROSS-PROJECT USAGE', '═' * 60, '']
        for projects, names in sorted(combo_plugins.items(), key=lambda x: -len(x[0])):
            out_lines.append(f'  {", ".join(sorted(names, key=lambda x: x.lower()))}')
            out_lines.append(f'  → {", ".join(projects)}')
            out_lines.append('')

        out_path = als_files[0].parent / '@ External Plugins List.txt'
        out_path.write_text('\n'.join(out_lines), encoding='utf-8')
        print(f'  External plugins list saved → {out_path.name}')


if __name__ == "__main__":

    main()
