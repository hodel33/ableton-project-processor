"""
gui.py

A webview-based GUI for the Ableton Project Processor. Reuses the CLI script's
step functions verbatim — this module is a thin shell that:
  1. Reads/writes config.ini with comment-aware line edits
  2. Scans .als files recursively (skips Backup folders and _processed files)
  3. Runs the pipeline in a background thread with stdout streaming to the UI

The original CLI (`ableton_project_processor.py`) is untouched and fully usable.
"""
import importlib.util
import queue
import re
import sys
import threading
from pathlib import Path

if importlib.util.find_spec("webview") is None:
    import subprocess
    print("First-run setup: installing pywebview (this may take a minute)...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pywebview"])
import webview

import ableton_project_processor as app


ROOT_DIR    = Path(__file__).parent.resolve()
CONFIG_PATH = ROOT_DIR / "config.ini"
GUI_DIR     = ROOT_DIR / "gui"
INDEX_HTML  = GUI_DIR / "index.html"
ICON_PATH   = GUI_DIR / "logo.ico"


# ═════════════════════════════════════════════════════════════
# WINDOW ICON — generated once from the synthwave logo design
# ═════════════════════════════════════════════════════════════

def _ensure_icon() -> Path | None:
    """Render files/logo.ico from the synthwave bar design if missing.
    Returns the path, or None if Pillow is unavailable."""
    if ICON_PATH.exists():
        return ICON_PATH
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    # Source design is 360×165; same rectangles as the GUI SVG logo.
    bars = [
        (0,   0,   30,  165, "#ce3571"),
        (45,  0,   30,  165, "#e55ca2"),
        (90,  0,   30,  165, "#fea741"),
        (135, 0,   30,  165, "#fff054"),
        (195, 0,   165, 30,  "#fff054"),
        (195, 45,  165, 30,  "#fea741"),
        (195, 90,  165, 30,  "#e55ca2"),
        (195, 135, 165, 30,  "#ce3571"),
    ]
    SRC_W, SRC_H = 360, 165
    CANVAS = 256

    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    scale = min(CANVAS / SRC_W, CANVAS / SRC_H)
    off_x = (CANVAS - SRC_W * scale) / 2
    off_y = (CANVAS - SRC_H * scale) / 2
    radius = max(1, int(3 * scale))
    for x, y, w, h, color in bars:
        x0 = off_x + x * scale
        y0 = off_y + y * scale
        draw.rounded_rectangle([x0, y0, x0 + w * scale, y0 + h * scale],
                               radius=radius, fill=color)

    ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
    img.save(ICON_PATH, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ICON_PATH


# ═════════════════════════════════════════════════════════════
# PIPELINE STEP METADATA (canonical order + human labels)
# Mirrors load_pipeline() in ableton_project_processor.py
# ═════════════════════════════════════════════════════════════
PIPELINE_STEPS = [
    ("remove_empty_tracks",                 "Remove empty tracks",                        "Delete Audio/MIDI tracks with no clips"),
    ("remove_muted_tracks",                 "Remove muted tracks",                        "Delete muted/deactivated tracks; cascades to empty groups"),
    ("ungroup_tracks",                      "Ungroup all grouped tracks",                 "Flatten group tracks; reroute children to master bus"),
    ("remove_unused_return_tracks",         "Remove unused return tracks",                "Remove return tracks with no active sends"),
    ("remove_disabled_devices",             "Remove disabled devices",                    "Strip insert devices that are switched off"),
    ("remove_non_automated_devices",        "Remove non-automated insert devices",        "Strip insert devices with zero automation anywhere in the project"),
    ("deduplicate_devices",                 "Deduplicate specific devices per track",     "Keep only the first instance of each named device per track"),
    ("convert_mixer_automation_to_utility", "Convert Mixer Vol/Pan automation → Utility", "Move Mixer Vol/Pan automation onto a cloned Utility device"),
    ("sort_color_tracks",                   "Sort & Recolor tracks / clips",              "Reorder and recolor tracks and clips based on prefixes"),
    ("duplicate_device_chain",              "Duplicate device chains to new tracks",      "Clone each Audio/MIDI track's device chain into a new track below"),
    ("quantize_midi_notes",                 "Quantize all MIDI notes to 1/16",            "Snap all MIDI note timings to 1/16 grid"),
    ("transpose_midi_notes",                "Transpose all MIDI notes",                   "Shift all MIDI pitches by a fixed number of semitones"),
    ("set_track_heights",                   "Set all track heights to a custom size",     "Set every track lane to your preferred height"),
    ("get_project_report",                  "Export full project report to txt",          "Export a detailed per-project report + cross-project plugin list"),
]


# ═════════════════════════════════════════════════════════════
# CONFIG I/O — comment-aware, alignment-preserving line edits
# ═════════════════════════════════════════════════════════════

def _split_line(line: str) -> tuple[str, str]:
    """Split a config line into (body, comment) where comment keeps its '#' and EOL."""
    if '=' not in line:
        return line, ''
    eq = line.index('=')
    hash_idx = line.find('#', eq)
    if hash_idx < 0:
        return line, ''
    return line[:hash_idx], line[hash_idx:]


def _rewrite_section_aligned(text: str, section: str, new_values: dict[str, str]) -> str:
    """Rewrite data lines in [section] with uniform alignment:
      * Key column padded to the longest key's width
      * Single tab between value and inline '#' comment
      * Comment-only lines and non-payload keys preserved verbatim
      * Each key's existing inline comment text travels with its row, unchanged
    """
    lines = text.splitlines(keepends=True)

    start = end = -1
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s == f'[{section}]':
            start = i
        elif start >= 0 and s.startswith('[') and s.endswith(']'):
            end = i
            break
    if start < 0:
        return text
    if end < 0:
        end = len(lines)

    section_lines = lines[start + 1:end]
    eol = '\r\n' if section_lines and section_lines[0].endswith('\r\n') else '\n'

    # Harvest original inline comments for each key (preserved verbatim)
    original_comments: dict[str, str] = {}
    for raw in section_lines:
        line = raw.rstrip('\r\n')
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in line:
            continue
        body, comment = _split_line(line)
        key = body.split('=', 1)[0].strip()
        if comment:
            original_comments[key] = comment.rstrip()

    key_width = max(len(k) for k in new_values) if new_values else 0
    value_width = max(len(str(v)) for v in new_values.values()) if new_values else 0

    out: list[str] = []
    for raw in section_lines:
        line = raw.rstrip('\r\n')
        line_eol = raw[len(line):] or eol
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in line:
            out.append(line + line_eol)
            continue
        body, _ = _split_line(line)
        key = body.split('=', 1)[0].strip()
        if key not in new_values:
            out.append(line + line_eol)
            continue
        value = new_values[key]
        comment = original_comments.get(key, '')
        new_line = f"{key:<{key_width}} = {str(value):<{value_width}}"
        if comment:
            new_line += '\t' + comment
        out.append(new_line + line_eol)

    return ''.join(lines[:start + 1]) + ''.join(out) + ''.join(lines[end:])


def _rewrite_prefixes_section(text: str, prefixes: list[dict]) -> str:
    """Rewrite the data rows of [TRACK_PREFIXES] only.

    Preserves verbatim:
      * All pre-data comment/header lines (between [TRACK_PREFIXES] and first data row)
      * The specials block (from the specials header onward, or first DEF/RTN/MST line)

    Rewrites only the non-special data rows:
      * Sorted ascending by sort number (GUI order)
      * Aligned columns (recomputed each save — never drifts)
      * Blank line before every group so individuals visually cluster below it
      * Each prefix's existing inline comment travels with its row, unchanged
    """
    specials = {'DEF', 'RTN', 'MST'}

    lines = text.splitlines(keepends=True)

    # Locate [TRACK_PREFIXES] section bounds
    start = end = -1
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped == '[TRACK_PREFIXES]':
            start = i
        elif start >= 0 and stripped.startswith('[') and stripped.endswith(']'):
            end = i
            break
    if start < 0:
        return text
    if end < 0:
        end = len(lines)

    section_lines = lines[start + 1:end]
    eol = '\r\n' if section_lines and section_lines[0].endswith('\r\n') else '\n'

    # Find where the specials block starts: specials-header comment OR first DEF/RTN/MST
    special_block_idx = len(section_lines)
    for i, raw in enumerate(section_lines):
        if 'Special' in raw and raw.lstrip().startswith('#'):
            special_block_idx = i
            break
    if special_block_idx == len(section_lines):
        for i, raw in enumerate(section_lines):
            s = raw.strip()
            if '=' in s and not s.startswith('#'):
                key = s.split('=', 1)[0].strip()
                if key in specials:
                    special_block_idx = i
                    break

    main_region = section_lines[:special_block_idx]
    special_region = section_lines[special_block_idx:]

    # Within main_region: find first data line (non-blank, non-comment, has '=')
    first_data_idx = len(main_region)
    for i, raw in enumerate(main_region):
        s = raw.strip()
        if s and not s.startswith('#') and '=' in s:
            first_data_idx = i
            break

    pre_data_header = main_region[:first_data_idx]

    payload_map = {p['prefix']: p for p in prefixes}

    # ── Rebuild non-special data rows ────────────────────────
    non_special = sorted(
        [p for p in prefixes if p['prefix'] not in specials],
        key=lambda p: (int(p.get('sort', 99)), p['prefix']),
    )

    def is_group(name: str) -> bool:
        return not (len(name) == 2 and name.isupper())

    rebuilt_data: list[str] = []
    if non_special:
        prefix_width = max(len(p['prefix']) for p in non_special)
        values = [f"{int(p['sort'])}, {int(p['color'])}" for p in non_special]
        value_width = max(len(v) for v in values)

        for i, p in enumerate(non_special):
            name = p['prefix']
            value = f"{int(p['sort'])}, {int(p['color'])}"
            comment_text = (p.get('comment') or '').strip()
            if i > 0 and is_group(name):
                rebuilt_data.append(eol)
            left = f"{name:<{prefix_width}} = {value:<{value_width}}"
            line = f"{left}    # {comment_text}" if comment_text else left.rstrip()
            rebuilt_data.append(line + eol)

    # ── Preserve special region verbatim, updating only color values ──
    # Ensure exactly one blank line between the data region and the specials block.
    # Strip any leading blanks on the special region; we'll control the separator.
    while special_region and not special_region[0].strip():
        special_region = special_region[1:]

    rebuilt_specials: list[str] = []
    if special_region:
        # One blank line separator (only if the rebuilt data region didn't already end with one)
        if rebuilt_data and rebuilt_data[-1].strip():
            rebuilt_specials.append(eol)
        for raw in special_region:
            line = raw.rstrip('\r\n')
            line_eol = raw[len(line):] or eol
            stripped = line.strip()
            if '=' in line and stripped and not stripped.startswith('#'):
                body, comment = _split_line(line)
                key = body.split('=', 1)[0].strip()
                if key in payload_map:
                    p = payload_map[key]
                    new_value = f"{int(p['sort'])}, {int(p['color'])}"
                    # Preserve key column (everything up to '=' + original leading ws around value).
                    # Always place exactly one tab between value and '#' comment.
                    key_part, _, value_part = body.partition('=')
                    lead_stripped = value_part.lstrip()
                    leading_ws = value_part[:len(value_part) - len(lead_stripped)]
                    new_body = f"{key_part}={leading_ws}{new_value}"
                    if comment:
                        rebuilt_specials.append(new_body + '\t' + comment + line_eol)
                    else:
                        rebuilt_specials.append(new_body + line_eol)
                    continue
            rebuilt_specials.append(line + line_eol)

    rebuilt = ''.join(pre_data_header) + ''.join(rebuilt_data) + ''.join(rebuilt_specials)
    return ''.join(lines[:start + 1]) + rebuilt + ''.join(lines[end:])


def read_config_as_dict() -> dict:
    """Read config.ini and return a structured dict for the frontend."""
    config = app.load_config(str(CONFIG_PATH))

    # Pipeline flags
    pipeline_raw = {k: v.split('#')[0].strip().lower() == 'true'
                    for k, v in config['PIPELINE'].items()} if 'PIPELINE' in config else {}
    pipeline = [
        {"id": sid, "label": label, "description": desc, "enabled": pipeline_raw.get(sid, False)}
        for sid, label, desc in PIPELINE_STEPS
    ]

    # Settings — keep raw strings for editing, the processor re-parses on run
    settings_raw = {}
    if 'SETTINGS' in config:
        for k, v in config['SETTINGS'].items():
            settings_raw[k] = v.split('#')[0].strip()

    # Validate TRACK_PREFIXES via the processor's loader — raises ValueError on
    # duplicate sort orders. The result is discarded; we rebuild our own list
    # below to also capture inline comments (which the processor doesn't need).
    app.load_track_config(config)

    # Track prefixes → list to preserve insertion order
    prefixes = []
    if 'TRACK_PREFIXES' in config:
        for k, v in config['TRACK_PREFIXES'].items():
            parts = v.split('#', 1)
            clean = parts[0].strip()
            comment = parts[1].strip() if len(parts) == 2 else ''
            try:
                sort_order, color_idx = [int(x) for x in clean.split(',')]
            except Exception:
                sort_order, color_idx = 99, 13
            # Categorise: 2-letter uppercase = individual; all-uppercase longer = group;
            # DEF/RTN/MST = special
            key = k.strip()
            if key in ('DEF', 'RTN', 'MST'):
                cat = 'special'
            elif len(key) == 2 and key.isupper():
                cat = 'individual'
            else:
                cat = 'group'
            prefixes.append({
                "prefix": key,
                "sort": sort_order,
                "color": color_idx,
                "category": cat,
                "comment": comment,
            })

    return {"pipeline": pipeline, "settings": settings_raw, "prefixes": prefixes}


def write_config_from_dict(payload: dict) -> None:
    """Persist a payload (pipeline/settings/prefixes) back to config.ini,
    preserving comments and alignment.
    """
    text = CONFIG_PATH.read_text(encoding='utf-8')

    # PIPELINE: bool → 'true'/'false' with aligned columns + tab before comments
    pipeline_map = {item['id']: ('true' if item['enabled'] else 'false')
                    for item in payload.get('pipeline', [])}
    if pipeline_map:
        text = _rewrite_section_aligned(text, 'PIPELINE', pipeline_map)

    # SETTINGS: keep raw strings (user typed them); same alignment treatment
    settings_map = {k: str(v) for k, v in payload.get('settings', {}).items()}
    if settings_map:
        text = _rewrite_section_aligned(text, 'SETTINGS', settings_map)

    # TRACK_PREFIXES: full rewrite (sorted, aligned, grouped) while preserving comments
    prefixes_payload = payload.get('prefixes', [])
    if prefixes_payload:
        text = _rewrite_prefixes_section(text, prefixes_payload)

    CONFIG_PATH.write_text(text, encoding='utf-8')


# ═════════════════════════════════════════════════════════════
# STDOUT → QUEUE REDIRECT
# ═════════════════════════════════════════════════════════════

_ANSI_RE = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')


class QueueStream:
    """Line-buffered stream that pushes each completed line into a queue.
    Strips ANSI colour codes so the HTML log stays clean.
    """
    def __init__(self, q: queue.Queue):
        self.q = q
        self.buf = ''

    def write(self, s: str):
        if not s:
            return
        self.buf += _ANSI_RE.sub('', s)
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            self.q.put(line)

    def flush(self):
        if self.buf:
            self.q.put(self.buf)
            self.buf = ''


# ═════════════════════════════════════════════════════════════
# PIPELINE RUNNER (background thread)
# ═════════════════════════════════════════════════════════════

def _aggregate_plugins(als_files: list[Path], root: Path) -> Path | None:
    """Compile @ External Plugins List.txt — mirrors the tail of main() in the CLI."""
    plugin_projects: dict[str, set[str]] = {}
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
                if sep_count == 2:
                    break
                continue
            if in_section and line.strip():
                plugin_projects.setdefault(line.strip(), set()).add(als_path.name)

    if not plugin_projects:
        return None

    combo_plugins: dict[tuple, list[str]] = {}
    for name, projects in plugin_projects.items():
        key = tuple(sorted(projects))
        combo_plugins.setdefault(key, []).append(name)

    out_lines = ['═' * 60, '  EXTERNAL PLUGINS — FULL LIST', '═' * 60, '']
    for name in sorted(plugin_projects.keys(), key=lambda x: x.lower()):
        out_lines.append(f'  {name}')
    out_lines.append('')
    out_lines += ['═' * 60, '  EXTERNAL PLUGINS — CROSS-PROJECT USAGE', '═' * 60, '']
    for projects, names in sorted(combo_plugins.items(), key=lambda x: -len(x[0])):
        out_lines.append(f'  {", ".join(sorted(names, key=lambda x: x.lower()))}')
        out_lines.append(f'  → {", ".join(projects)}')
        out_lines.append('')

    out_path = root / '@ External Plugins List.txt'
    out_path.write_text('\n'.join(out_lines), encoding='utf-8')
    return out_path


# ═════════════════════════════════════════════════════════════
# API exposed to JavaScript
# ═════════════════════════════════════════════════════════════

class Api:
    def __init__(self):
        self._project_root: Path = ROOT_DIR
        self._log_queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── Meta ─────────────────────────────────────────────────
    def get_initial_state(self) -> dict:
        try:
            cfg = read_config_as_dict()
        except Exception as e:
            return {"ok": False, "error": f"Failed to read config.ini: {e}"}
        return {
            "ok": True,
            "project_root": str(self._project_root),
            "config_path": str(CONFIG_PATH),
            "als_files": self._scan(),
            **cfg,
        }

    # ── File system ──────────────────────────────────────────
    def pick_folder(self) -> dict:
        win = webview.windows[0]
        result = win.create_file_dialog(webview.FileDialog.FOLDER, directory=str(self._project_root))
        if not result:
            return {"ok": False}
        self._project_root = Path(result[0]).resolve()
        return {"ok": True, "project_root": str(self._project_root), "als_files": self._scan()}

    def rescan(self) -> dict:
        return {"ok": True, "als_files": self._scan()}

    def _scan(self) -> list[dict]:
        try:
            files = app.find_als_files(self._project_root)
        except Exception:
            return []
        return [
            {"name": f.name, "folder": f.parent.name, "path": str(f)}
            for f in files
        ]

    # ── Config ───────────────────────────────────────────────
    def save_config(self, payload: dict) -> dict:
        try:
            write_config_from_dict(payload)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reload_config(self) -> dict:
        try:
            return {"ok": True, **read_config_as_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Pipeline ─────────────────────────────────────────────
    def run_pipeline(self) -> dict:
        if self._worker and self._worker.is_alive():
            return {"ok": False, "error": "Pipeline already running"}

        # Drain any leftover queue entries
        while not self._log_queue.empty():
            try:
                self._log_queue.get_nowait()
            except queue.Empty:
                break

        self._stop_event.clear()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()
        return {"ok": True}

    def stop_pipeline(self) -> dict:
        self._stop_event.set()
        return {"ok": True}

    def poll_logs(self) -> dict:
        lines = []
        while not self._log_queue.empty():
            try:
                lines.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        running = bool(self._worker and self._worker.is_alive())
        return {"lines": lines, "running": running}

    # ── Worker ───────────────────────────────────────────────
    def _run_worker(self):
        old_stdout = sys.stdout
        stream = QueueStream(self._log_queue)
        sys.stdout = stream
        try:
            config = app.load_config(str(CONFIG_PATH))
            settings = app.load_settings(config)
            context = app.Context(
                track_config             = app.load_track_config(config),
                dedupe_devices           = settings.get('dedupe_devices', []),
                exclude_conversion_types = settings.get('exclude_conversion_types', []),
                chain_suffix             = str(settings.get('duplicate_chain_suffix', '')).strip("'\""),
                exclude_midi_prefixes    = settings.get('exclude_midi_prefixes', []),
                transpose_semitones      = settings.get('transpose_semitones', 0),
                lane_height              = settings.get('lane_height', 68),
            )
            pipeline = app.load_pipeline(config)

            if not pipeline:
                print("No pipeline steps enabled — toggle at least one on the left.")
                return

            als_files = app.find_als_files(self._project_root)
            if not als_files:
                print(f"No .als files found under:\n  {self._project_root}")
                return

            print(f"Project folder: {self._project_root}")
            print(f"Found {len(als_files)} .als file(s).")
            print(f"Active steps ({len(pipeline)}):")
            for _, _, description in pipeline:
                print(f"  [+] {description}")
            print()

            for als_path in als_files:
                if self._stop_event.is_set():
                    print("\n⏹  Stop requested — aborting remaining files.")
                    break
                app.run_pipeline(als_path, context, pipeline)
                print()

            if not self._stop_event.is_set():
                report_enabled = any(s[0] == 'get_project_report' for s in pipeline)
                if report_enabled and len(als_files) >= 2:
                    out = _aggregate_plugins(als_files, self._project_root)
                    if out:
                        print(f'{"═" * 80}')
                        print('EXTERNAL PLUGINS SUMMARY  (all projects)')
                        print(f'{"═" * 80}')
                        print()
                        print(f'  Saved → {out}')
                print("\n✓ All done.")
        except Exception as e:
            import traceback
            print("\n⚠  Pipeline crashed:")
            for line in traceback.format_exc().splitlines():
                print(f"    {line}")
            print(f"\nError: {e}")
        finally:
            stream.flush()
            sys.stdout = old_stdout


# ═════════════════════════════════════════════════════════════
# ENTRY
# ═════════════════════════════════════════════════════════════

def main():
    if not CONFIG_PATH.exists():
        print(f"config.ini not found at {CONFIG_PATH}")
        sys.exit(1)
    if not INDEX_HTML.exists():
        print(f"GUI assets missing — expected {INDEX_HTML}")
        sys.exit(1)

    api = Api()
    webview.create_window(
        title="Ableton Project Processor",
        url=INDEX_HTML.as_uri(),
        js_api=api,
        width=1320,
        height=880,
        min_size=(1280, 720),
        background_color="#0E0E11",
        text_select=True,
    )
    icon = _ensure_icon()
    webview.start(debug=False, icon=str(icon) if icon else None)


if __name__ == "__main__":
    main()
