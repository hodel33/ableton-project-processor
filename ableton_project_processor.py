"""
ableton_project_processor.py

A swiss army knife for Ableton projects — batch-Collect and batch-process whole folders
of sets straight at the XML level, without opening Live once.

Collect : a batch "Collect All and Save" — bundle every project with its samples, .asd
          analysis files and Max for Live devices copied in and relinked, tracking down
          missing samples on disk first.
Process : clean tracks, ungroup, strip unused devices, sort & recolor, quantize/transpose
          MIDI, convert mixer automation to utility, and detailed per-project reports with
          external plugin aggregation.

Which one runs is set by [COLLECT] enabled in config.ini; when on, Collect replaces the
processing pipeline for that run (see collect.py). Original files are never overwritten —
a new _processed.als, or a fresh _collected bundle, is always created.

Usage:
    python ableton_project_processor.py

© 2026 Hodel33
"""
import sys
import os
import configparser
from pathlib import Path

from als_core import Context, find_als_files
from pipeline import load_pipeline, run_pipeline, aggregate_external_plugins
from collect import run_collect

# Windows consoles default to cp1252 and choke on the box-drawing / ✓ characters the
# reports print — force UTF-8 so output is safe when run or piped anywhere.
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass


CONFIG_LOCATION = "config.ini"


# ── Report & terminal layout widths ──────────────────────────────────────────
# The fixed bar/rule widths this entry point draws on screen, kept here so they can be
# re-tuned from one place. Structural — they do NOT track the text inside them.
# (The .txt-report widths live with the report builders in pipeline.py.)
SUMMARY_BAR_W = 80   # ═ bar around the on-screen summary boxes
HEADER_RULE_W = 96   # ‾ rule under the ASCII-art app header


# ═════════════════════════════════════════════════════════════
# CONFIG & STARTUP
# ═════════════════════════════════════════════════════════════


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
    print("‾" * HEADER_RULE_W)
    print("\033[0;0m")


def print_pipeline_header():
    """Print the processing settings section header."""
    print("************      PROCESSING SETTINGS      ************")
    print()


def print_pipeline_info(root: Path, pipeline: list):
    """Print the project folder and the enabled pipeline steps."""
    print(f"  Project folder : {root}")
    print(f"  Active steps   :")
    print()
    for step_id, step_fn, description in pipeline:
        print(f"    [+] {description}")

    print()

    if any(step_id == "convert_mixer_automation_to_utility" for step_id, _, _ in pipeline):
        print("  Note: project must have at least one Utility device anywhere to use as clone template.")
        print()


def confirm_start(n_files: int) -> bool:
    """Y/n start confirmation — Enter or 'y' proceeds, 'n' quits. Matches the app's other
    terminal prompts. Returns False if they chose not to start."""
    print()
    print(f"  Process all {n_files} .als file(s)?  [Y/n]")
    return input("  > ").strip().lower() not in ("n", "no")


def load_config(config_file=CONFIG_LOCATION):
    """Load and return the config.ini file as a ConfigParser object."""

    config = configparser.ConfigParser()
    config.optionxform = str
    try:
        if not config.read(config_file, encoding='utf-8'):
            raise FileNotFoundError(f"{config_file} not found")
    except configparser.DuplicateOptionError as e:
        raise ValueError(f"[{e.section}] duplicate prefix '{e.option}' — prefix names must be unique")

    return config


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

    # Reject duplicate sort orders — specials (DEF/RTN/MST) all share 99 by design, so skip them.
    specials = {'DEF', 'RTN', 'MST'}
    seen: dict[int, list[str]] = {}
    for prefix, cfg in track_config.items():
        if prefix in specials:
            continue
        seen.setdefault(cfg['sort'], []).append(prefix)
    dupes = {s: names for s, names in sorted(seen.items()) if len(names) > 1}
    if dupes:
        details = '; '.join(f"sort {s} used by {', '.join(names)}" for s, names in dupes.items())
        raise ValueError(f"[TRACK_PREFIXES] duplicate sort order(s) — {details}")

    return track_config


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
    except (FileNotFoundError, ValueError) as e:
        print(f"Config error: {e}")
        print()
        sys.exit(1)

    root = Path(__file__).parent

    # Collect mode fully replaces processing: when [COLLECT] enabled is on, this run
    # gathers project bundles and the pipeline is skipped entirely.
    if config.get('COLLECT', 'enabled', fallback='false').split('#')[0].strip().lower() == 'true':
        run_collect(root, config)
        return

    try:
        settings = load_settings(config)

        context = Context(
            track_config             = load_track_config(config),
            dedupe_devices           = settings.get('dedupe_devices', []),
            exclude_conversion_types = settings.get('exclude_conversion_types', []),
            chain_suffix             = settings.get('duplicate_chain_suffix', '').strip("'\""),
            exclude_midi_prefixes    = settings.get('exclude_midi_prefixes', []),
            transpose_semitones      = settings.get('transpose_semitones', 0),
            lane_height              = settings.get('lane_height', 68),
        )

        pipeline = load_pipeline(config)

    except (FileNotFoundError, ValueError) as e:
        print(f"Config error: {e}")
        print()
        sys.exit(1)

    if not pipeline:
        print("No pipeline steps enabled — update config.ini and re-run.")
        print()
        sys.exit(0)

    als_files = find_als_files(root)
    if not als_files:
        print(f"No .als files found in subfolders of: {root}\n")
        sys.exit(0)

    print_pipeline_header()
    print_pipeline_info(root, pipeline)
    print(f"Found {len(als_files)} .als file(s) in subfolders of: {root}")
    if not confirm_start(len(als_files)):
        print("\n  Cancelled — nothing processed.\n")
        sys.exit(0)

    for als_path in als_files:
        run_pipeline(als_path, context, pipeline)
        print()

    # ── Global external plugins list ──────────────────────────────────────
    report_enabled = any(s[0] == 'get_project_report' for s in pipeline)
    out_path = (aggregate_external_plugins(als_files, root)
                if report_enabled and len(als_files) >= 2 else None)
    if out_path:
        print(f'{"═" * SUMMARY_BAR_W}')
        print('EXTERNAL PLUGINS SUMMARY  (all projects)')
        print(f'{"═" * SUMMARY_BAR_W}')
        print(f'  Saved → {out_path}')
        print()

    print()


if __name__ == "__main__":

    main()
