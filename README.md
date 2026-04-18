# Ableton Project Processor 🎛️

## 📋 Overview

**Ableton Project Processor** is a modular toolbox for cleaning and transforming Ableton `.als` project files directly at the XML level. It decompresses the `.als` gzip archive, applies a configurable set of processing steps, runs integrity checks and writes a clean `_processed.als` copy alongside the original, all without touching a single knob. Whether you're tidying up a chaotic client project, batch quantizing and transposing all MIDI clips like magic, auto-sorting and recoloring every track in one pass, or just want a detailed report of every external plugin used across your sessions — this has you covered!

The idea behind all of this came funny enough from a friend ranting about receiving messy Ableton projects from clients (who thought ranting could inspire!? hah). I joked that maybe I could help — and voilà, an Ableton project cleaner was born! It became so much more than that though, more of a swiss army knife now really. Proud of how it turned out! I genuinely hope it helps many of you producers out there. Cheers to my friends for contributing ideas along the way: Mattia (Nihil Young), Mateusz (Skytech), Sean Tyas and Jonas Hornblad 💜

> 💡 The original `.als` file is **never overwritten** — a new `_processed.als` file is always created.

<br>

### 🌟 Features

- **Compatible with Ableton Live 12 & 11**: Fully supports project structures from both versions, with processing steps designed to handle version-specific differences safely

- **No Ableton Required**: Operates entirely on the raw decompressed XML inside `.als` files — no Live installation needed, no project loading, no GUI

- **Non-Destructive Processing**: The original `.als` is never touched; every run produces a new `_processed.als` alongside it

- **Batch Processing**: Drop multiple projects in subfolders next to the script and process them all in one run

- **Modular Pipeline**: Toggle each processing step On or Off independently in `config.ini` — run only what you need, in a fixed deterministic order

- **Track Cleaning**: Remove empty tracks, muted tracks and return tracks with no active sends — with automatic cleanup of orphaned send holders and empty groups left behind

- **Device Management**: Remove disabled devices, strip insert devices that have no automation anywhere in the project and deduplicate specific named devices per track — all with guards to protect sound sources and automated on/off toggles

- **Mixer Automation → Utility**: Lift Volume and Pan automation off the Mixer and onto a cloned Utility device inserted directly in the track chain, keeping automation intact and values perfectly mapped

- **Track Organisation**: Sort tracks into a custom prefix-based order and recolor both tracks and their clips in one pass — groups and children handled as atomic units, children without their own prefix inherit the group color

- **MIDI Tools**: Quantize all note timings to a 1/16 grid and/or transpose the entire project by a fixed number of semitones, with automatic pitch clamping and configurable prefix exclusions to protect drum/fx tracks for example

- **Device Chain Duplication**: Clone every Audio and MIDI track's device chain into a new track inserted directly below — clips and automation stripped, custom suffix appended to the name

- **Ungroup Tracks**: Flatten all group tracks in one step — child routing redirected to the master bus

- **Set Track Heights**: Set every track lane to your preferred height in one pass

- **Per-Project Reports**: Export a detailed `_report.txt` for every project covering BPM, time signature, locators, full track and device breakdowns, external plugin list and warning flags for muted/frozen/unnamed/duplicate tracks

- **Global Plugin Aggregation**: If more than one project is found, automatically compile an `@ External Plugins List.txt` across all processed projects — showing every unique external plugin used and a cross-project usage breakdown to spot shared dependencies at a glance

<br>

## ⚙️ Installation

1. 🐍 **Install Python 3.11+**
   - Download and install the latest version from [python.org](https://www.python.org/downloads/)
   - **Windows tip**: During installation, check **"Add Python to PATH"**
   - Verify installation by opening a terminal and running:
     ```bash
     python --version
     ```

2. ⬇️ **Download and place these files** in the same folder:
```
   your_folder/
   ├── ableton_project_processor.py
   ├── config.ini
   ├── run.bat           ← Windows launcher
   └── run.command       ← macOS launcher
```

> 💡 You only need the launcher for your OS (`run.bat` for Windows, `run.command` for macOS)

3. Your `.als` projects should be organised **one subfolder deep** relative to the root folder:
```
   your_folder/
   ├── ableton_project_processor.py
   ├── config.ini
   ├── run.(bat/command)
   └── MySongs/
       ├── MySong_1.als     ← scanned
       └── MySong_2.als     ← scanned
```
   Files directly in the root or already named `_processed.als` are ignored.

<br>

## 🚀 Usage

Configure your steps in `config.ini`, then launch the script. It prints a **processing summary** before starting and prompts for confirmation — press `ENTER` to proceed or `q + ENTER` to exit.

### 🪟 Windows
Double-click `run.bat` — that's it.

### 🍎 macOS
First-time setup required due to macOS security restrictions. Choose one of two options:

**Option A — System Settings (easier):**
1. Double-click `run.command` — a security popup will appear, just close it
2. Go to **System Settings → Privacy & Security**
3. Scroll down and click **"Open Anyway"** next to the `run.command` entry
4. From now on, just double-click `run.command` to launch

**Option B — Terminal (one-time setup):**
1. Right-click the folder → **Services → New Terminal at Folder**
2. Run these two commands:
```bash
   chmod +x run.command
   xattr -d com.apple.quarantine run.command
```
3. Close Terminal — from now on just double-click `run.command` to launch

<br>

## 🔧 Configuration

All behaviour is controlled by `config.ini` in the same directory as the script.

### `[PIPELINE]` — Toggle steps on/off

Set each step to `true` or `false`:

```ini
[PIPELINE]
remove_empty_tracks                  = false  # Remove Audio/MIDI tracks with no clips
remove_muted_tracks                  = false  # Remove muted/deactivated tracks
ungroup_tracks                       = false  # Flatten all group tracks
remove_unused_return_tracks          = false  # Remove return tracks with no active sends
remove_disabled_devices              = false  # Remove insert devices that are turned off
remove_non_automated_devices         = false  # Remove insert devices with no automation
deduplicate_devices                  = false  # Remove duplicate instances of named devices per track (set in SETTINGS)
convert_mixer_automation_to_utility  = false  # Move Mixer Vol/Pan automation onto a cloned Utility device
sort_color_tracks                    = false  # Reorder and recolor tracks based on prefixes (set in TRACK_PREFIXES)
duplicate_device_chain               = false  # Clone each track's device chain into a new track below
quantize_midi_notes                  = false  # Snap all MIDI note timings to 1/16 grid (set exclusions in SETTINGS)
transpose_midi_notes                 = false  # Shift all MIDI pitches by a fixed number of semitones (set in SETTINGS)
set_track_heights                    = false  # Set all track lane heights to a custom size (set in SETTINGS)
get_project_report                   = false  # Export a full project report to txt
```

### `[SETTINGS]` — Step-specific parameters

```ini
[SETTINGS]

dedupe_devices           = ott, saus    # Device names to deduplicate (comma-separated) — case-insensitive, partial names work (e.g. 'saus' matches 'Sausage Fattener')
exclude_conversion_types = RTN, MST		# Exclude Return (RTN) and/or Master (MST) from conversion
duplicate_chain_suffix   = ' [chain]'   # Suffix appended to duplicated track names
exclude_midi_prefixes    = DRUMS,DR,FX  # Track prefixes to skip during quantize & transpose
transpose_semitones      = -12          # Semitone shift for MIDI notes (e.g. +2, -3, -12)
lane_height              = 68           # Track height — must be a multiple of 17 (range: 17–425)
```

### `[TRACK_PREFIXES]` — Sort order & colors (used by `sort_color_tracks`)

> 💡 The prefix list below is just a starting point of one workflow — feel free to completely make it your own. Change sort orders, swap colors, add new prefixes or remove ones you don't need. It's fully yours to customize.
>
> A color palette reference is included at the bottom of the section — but to find the exact color index for a specific color, just check it directly in Ableton and note the corresponding number.

Each prefix maps to a sort position and an Ableton color index (0–69). Two types of prefixes are supported — 2-letter prefixes for individual tracks and full uppercase words for group tracks:

```ini
[TRACK_PREFIXES]
# Prefix = Sort Order, Ableton Color Index (0-69)
# ─── Individual track prefixes (2-letter) ────────────────────────
BD  = 2, 14     # Kick                       — Red (light)
DR  = 4, 56     # Drums                      — Red (dark)
SB  = 6, 15     # Sub Bass                   — Brown
MB  = 7, 15     # Mid Bass                   — Brown
TB  = 8, 15     # Top Bass                   — Brown
LD  = 10, 19    # Leads                      — Green
PL  = 11, 19    # Plucks                     — Green
AR  = 12, 19    # Arps                       — Green
PD  = 13, 22    # Pads                       — Blue
KY  = 16, 21    # Keys                       — Cyan
OR  = 17, 39    # Orchestral                 — Purple
FX  = 20, 0     # FX                         — Pink
RS  = 21, 0     # Risers                     — Pink
AT  = 22, 54    # Atmos                      — Pink
VX  = 25, 3     # Vocals                     — Yellow

# ─── Wide group prefixes (full uppercase word) ───────────────────
KICK        = 1, 14     # Kick group         — Red (light)
DRUMS       = 3, 56     # Drums group        — Red (dark)
BASS        = 5, 15     # Bass group         — Brown
SYNTHS      = 9, 19     # Synths group       — Green
INSTRUMENTS = 15, 21    # Instruments group  — Cyan
EFFECTS     = 19, 0     # FX group           — Pink
VOCALS      = 24, 3     # Vocals group       — Yellow

# ─── Special track types (by type, not prefix) ───────────────────
DEF = 99, 13    # Default fallback           — White
RTN = 99, 41    # Return tracks              — Grey
MST = 99, 69    # Master                     — Black

# ─── Ableton color palette reference (5 x 14) ────────────────────
# Col 1      (0, 14, 28, 42, 56)                   →  Red / Pink
# Col 2-3    (1-2, 15-16, 29-30, 43-44, 57-58)     →  Orange / Brown
# Col 4-6    (3-5, 17-19, 31-33, 45-47, 59-61)     →  Green / Yellow
# Col 7-10   (6-9, 20-23, 34-37, 48-51, 62-65)     →  Blue / Cyan
# Col 11-13  (10-12, 24-26, 38-40, 52-54, 66-68)   →  Purple / Pink
# Col 14     (13, 27, 41, 55, 69)                  →  White / Black
```

> Any track whose prefix doesn't match an entry in the list falls back to `DEF`.

<br>

## 🎚️ Pipeline Steps Explained

| Step | What it does |
|---|---|
| `remove_empty_tracks` | Deletes Audio/MIDI tracks with no clips |
| `remove_muted_tracks` | Deletes muted/deactivated tracks; cascades to groups that become empty as a result |
| `ungroup_tracks` | Flattens all group tracks; redirects routing to master bus |
| `remove_unused_return_tracks` | Removes return tracks with no active sends; re-indexes remaining send holders |
| `remove_disabled_devices` | Removes insert devices that are turned off; devices with an automated on/off are never removed; first device in chain always kept to protect track volume/pan/send automation |
| `remove_non_automated_devices` | Removes insert devices with no automation anywhere in the project; first device in chain always kept to protect track volume/pan/send automation |
| `deduplicate_devices` | Keeps only the first instance of each named device per track; target names set via `dedupe_devices` — case-insensitive, partial names work |
| `convert_mixer_automation_to_utility` | Moves Mixer Vol/Pan automation onto a cloned Utility device appended at the end of each affected track's chain; Volume → Gain, Pan → Balance (1:1 value mapping); requires at least one existing Utility device anywhere in the project |
| `sort_color_tracks` | Reorders and recolors tracks and their clips based on prefixes in `TRACK_PREFIXES`; groups and children sorted as atomic units; children sorted by their own prefix within each group; children without a matching prefix inherit their parent group's color |
| `duplicate_device_chain` | Clones each Audio/MIDI track's device chain into a new track directly below; clips and automation stripped; `duplicate_chain_suffix` appended to the name |
| `quantize_midi_notes` | Snaps all MIDI note timings to 1/16 grid; set `exclude_midi_prefixes` to skip specific track prefixes (e.g. DRUMS, EFFECTS) |
| `transpose_midi_notes` | Shifts all MIDI pitches by `transpose_semitones`; shift is capped by project-wide min/max note to stay within 0–127; set `exclude_midi_prefixes` to skip specific track prefixes (e.g. DRUMS, EFFECTS) |
| `set_track_heights` | Sets every track lane to your preferred height — set via `lane_height` in SETTINGS |
| `get_project_report` | Exports a full read-only report to `ProjectName_report.txt`; enable this step alone to report on the original unmodified file; if more than one project is found, a shared `@ External Plugins List.txt` is also generated (see [📊 Project Reports](#-project-reports)) |

> **Note:** `convert_mixer_automation_to_utility` requires at least one Utility (StereoGain) device anywhere in the project to use as a clone template.

<br>

## 📊 Project Reports

### Per-project report — `ProjectName_report.txt`

Generated next to the source `.als` file when `get_project_report = true`. The file contains three sections:

- **PROJECT SUMMARY** — Creator, BPM, time signature, locators (with names), track counts by type, return track names, clip counts (MIDI/Audio), total automation envelopes, and any warning flags: frozen tracks, muted tracks, unnamed tracks, duplicate track names, disabled devices.
- **EXTERNAL PLUGINS** — Alphabetical list of all external (VST2/VST3/AU) plugins used in the project.
- **FULL DEVICE LIST** — Nested device tree per track. Each device shows its name, `[Off]` if disabled, and `[Auto:N]` if it has N automated parameters.

> 💡 To report on the original unmodified project, enable only `get_project_report` and disable all other steps — the report always reflects the state of the project after any enabled steps have run.

A compact summary is also printed to the terminal during processing.

### Global report — `@ External Plugins List.txt`

Automatically written to the same folder as your projects after all files have been processed, if more than one `.als` file was found. It aggregates the external plugin data from every `_report.txt` and produces two sections:

- **FULL LIST** — Every external plugin found across all projects, sorted alphabetically.
- **CROSS-PROJECT USAGE** — Plugins grouped by which combination of projects they appear in; useful for spotting shared dependencies or missing installs.

<br>

## 🛡️ Safety & Integrity

Before validation, the script runs a silent auto-cleanup pass on the processed XML:

- **Dead automation envelopes** orphaned by removed tracks or devices are automatically removed
- **NextPointeeId** is automatically corrected if it has fallen behind the highest ID in the project

After cleanup, the script runs integrity checks before saving:

- **TrackSendHolder count** matches the remaining return track count
- **No duplicate track IDs** (prevents Ableton's "non-unique list ids" error)
- **No new dangling PointeeIds** introduced by the script — only newly introduced ones block saving
- **No truncated output** — verifies `</LiveSet>` is present at end of file

If any check fails, the issue is printed clearly in the terminal output and the file is **not saved**.

<br>

## 📜 License

This project is licensed under a Custom License — see the [LICENSE](LICENSE) file for details.

### Summary:

1. **Personal Use Only**: The software may be used and modified for personal, non-commercial purposes only.
2. **No Commercial Use**: The software may not be used for any commercial purposes.
3. **No Distribution**: The software may not be distributed or included in any larger software distributions.
4. **No Sale**: The software may not be sold.

For the full license, please refer to the [LICENSE](LICENSE) file in the repository.

<br>

## 💬 Feedback & Contact

I'd love to network, discuss tech or swap music recommendations. Feel free to connect with me on:

🌐 **LinkedIn**: [Björn Hödel](https://www.linkedin.com/in/bjornhodel)<br>
📧 **Email**: [hodel33@gmail.com](mailto:hodel33@gmail.com)<br>
📸 **Instagram**: [@hodel33](https://www.instagram.com/hodel33)

If you run into any bugs, have feature suggestions or just want to share how you're using the tool — I'd love to hear from you! 💜
