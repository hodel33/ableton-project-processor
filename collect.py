"""
collect.py

Gather an Ableton project into a self-contained bundle — every sample, .asd analysis
sibling and Max-for-Live device copied in and repointed, so the set opens on another
machine with nothing offline. Non-destructive: the original .als is never touched.

Triggered by [COLLECT] enabled in config.ini; run_collect() is the entry point. Missing
samples are auto-located across a tiered on-disk search first, and only genuinely-lost
files are reported. Optionally writes "@ Required Plugins & Packs.txt" listing the
3rd-party plugins, M4L devices and Ableton Packs the set depends on.

Sits beside pipeline.py — both build on als_core, neither imports the other.
"""

import re
import os
import gzip
import time
import shutil
from pathlib import Path
from datetime import datetime
from xml.sax.saxutils import escape, unescape

from als_core import (find_blocks, extract_device_name, SKIP_DIRS,
                      read_als_header, detect_live_version, MIN_SUPPORTED_LIVE)


# ── .als reference (decoder key, not used directly) ───────────────────────────
# Kept as a comment so the raw values the code compares against stay readable. A <FileRef> is
# the little block inside a SampleRef (a sample) or MxPatchRef (an M4L device) that says WHERE
# that file lives on disk — an absolute <Path>, a <RelativePath>, and RelativePathType: a flag
# telling Ableton what that relative path is anchored to (the project, a Pack, the User Library…),
# i.e. how to relocate the file. The code just compares the raw string (e.g. ptype == '2') and
# writes '3'. Names below are the reverse-engineered enum (posted on the Cycling '74 forum;
# never officially published) — 0,1,3,5,6,7 additionally verified against real .als files here:
#      0 = Unknown                 (Missing — no relative anchor, resolved by the absolute Path;
#                                   also where a lost/offline sample ends up)
#      1 = RelativeToDocument      (External — relative to the .als itself)
#      2 = RelativeToOldLibrary    (legacy library location)
#      3 = RelativeToProject       (inside the project folder — what Collect rewrites to)
#      5 = RelativeToFactoryPack   (Core Library / add-on Pack content)
#      6 = RelativeToUserLibrary   (your Ableton User Library)
#      7 = RelativeToBuiltinContent (Live's built-in devices/content)
# Max for Live device container tags (Collect locates the .amxd via the inner
# <MxPatchRef>, NOT these): MxDeviceInstrument, MxDeviceAudioEffect, MxDeviceMidiEffect


# 3rd-party plugin hosts. Native Live devices have their own tags and are ignored —
# they ship with every install, so they can never be "missing" on a new machine.
PLUGIN_TAGS = ('PluginDevice', 'AuPluginDevice')

# Ableton's own Pack (LivePackId www.ableton.com/0). Ships with every Live install, so
# it is never worth naming as a dependency — unlike any other Pack.
CORE_LIBRARY = 'Core Library'

# ── Layout widths ─────────────────────────────────────────────────────────────
# Every bar/divider width the terminal output and the report draw lives here, so it
# can be re-tuned from one place. Bars are structural rules of a fixed width; they do
# NOT track the text inside them. (Same numbers as the processor's own layout block.)
SECTION_BAR_W  = 60   # ═ bar under each section header in the plugin report (REPORT_BAR)
DIVIDER_W      = 80   # ─ rule bracketing the still-missing-files prompt
PROJECT_RULE_W = 64   # - underline under each per-project title (matches the processing run)

# ── Terminal layout ───────────────────────────────────────────────────────────
PROJECT_RULE = '-' * PROJECT_RULE_W
TERM_W       = 12   # label pad; widest label is 'M4L devices' (11)

# ── Missing-sample search bounds ──────────────────────────────────────────────
# Dev-tweakable file caps for locate_missing's disk scan — deliberately NOT surfaced in
# config.ini: these are internal safety limits, not producer-facing options. The automatic
# tiers scan folders the user never chose, so they share one bounded budget; the user's
# EXPLICIT backup location (Tier 5) gets its own, much larger one so a deliberate library
# sweep is never starved by the guesses. Both scans are Stop-interruptible regardless.
AUTO_TIER_BUDGET    = 200_000     # shared file cap across the automatic tiers (1–4)
BACKUP_SEARCH_LIMIT = 1_000_000   # file cap for EACH backup_search_location (Tier 5); 0 = unlimited

# ═════════════════════════════════════════════════════════════
# LOADING
# ═════════════════════════════════════════════════════════════

def load_als_xml(als_path: Path) -> str:
    """Decompress an .als gzip archive into raw XML text (same approach as the processor)."""
    with gzip.open(als_path, "rb") as f:
        return f.read().decode("utf-8")


# ═════════════════════════════════════════════════════════════
# COLLECT  (opt-in via [COLLECT] enabled in config.ini)
# ═════════════════════════════════════════════════════════════
#
# Non-destructive. Produces a self-contained bundle folder next to the source:
#     <stem>_collected/
#         <stem>.als            ← rewritten project (original never touched)
#         Samples/...           ← every used sample, gathered locally
#
# Per-sample decision (location-based, not type-based — the type flag can lie):
#   1. Pack content (non-empty LivePackName — Core Library and add-on Packs alike) →
#      left behind by default (the target is assumed to already have the Packs); set
#      collect_ableton_packs = true to copy it in for a fully standalone bundle.
#   2. Found INSIDE the project folder (matched by filename + OriginalFileSize) →
#      copied into the bundle preserving its subpath.
#   3. Found OUTSIDE the project folder → copied into Samples/Collected/.
#   4. Found nowhere → reported MISSING, reference left as-is (no copy).
#
# Every collected/inside reference is repointed: RelativePathType → 3, and
# RelativePath + Path rewritten relative to / under the bundle. Files are
# deduplicated (one physical copy per unique source), name collisions in
# Samples/Collected/ are disambiguated (_1, _2, ...).

EXCLUDE_DIRS = ('Backup', 'Ableton Project Info')  # never index backups / the project marker

PROJECT_MARKER = 'Ableton Project Info'  # its presence next to an .als = a real Ableton Project

def _field(block: str, tag: str):
    """Read the Value attribute of a <tag Value="..."> inside a block (XML-escaped, raw)."""
    m = re.search(r'<' + tag + r'\s+Value="([^"]*)"', block)
    return m.group(1) if m else None

def _set_field(block: str, tag: str, value: str) -> str:
    """Replace the Value of the first <tag Value="..."> in block (value must be XML-escaped)."""
    return re.sub(r'(<' + tag + r'\s+Value=")[^"]*(")',
                  lambda m: m.group(1) + value + m.group(2), block, count=1)

def _xml(s: str) -> str:
    """Escape a filesystem string for use in an XML attribute value."""
    return escape(s, {'"': '&quot;'})

def human_bytes(n: float) -> str:
    """Format a byte count as a human-readable string."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024

def build_project_index(project_root: Path):
    """
    Index every file physically under the project folder (by name+size and by name).
    Returns (by_name_size, by_name) for identity matching.
    Skips Backup/ and the Ableton Project Info marker (see EXCLUDE_DIRS).
    """
    by_name_size, by_name = {}, {}
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            by_name_size.setdefault((fn.lower(), size), fp)
            by_name.setdefault(fn.lower(), []).append(fp)
    return by_name_size, by_name


def _within(path: Path, root: Path) -> bool:
    """True when `path` sits inside `root` (root already resolved). The workspace home-zone
    test: a sample resolving under root is left where it is instead of collected."""
    try:
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def workspace_root(project_root: Path, levels: int) -> Path | None:
    """The 'home zone' root — `levels` folders above the project folder. Samples that resolve
    anywhere under it are kept in place (not copied). Returns None when the feature is off
    (levels <= 0) or the walk would land on a drive root / the home folder: there, treating
    everything below as 'nearby' would wrongly keep unrelated projects' samples out of the
    bundle and collect almost nothing — so an over-large `levels` is guarded to a no-op."""
    if levels <= 0:
        return None
    root = project_root
    for _ in range(levels):
        root = root.parent
    try:
        root = root.resolve()
    except OSError:
        return None
    if root == Path(root.anchor) or root == Path.home():
        return None
    return root


def classify_ref(block: str, als_dir: Path, idx_ns: dict, idx_n: dict,
                 skip_packs: bool, skip_m4l: bool = False, is_m4l: bool = False,
                 keep_root: Path | None = None):
    """Decide what to do with one SampleRef / MxPatchRef. Returns (category, source_path_or_None, filename)."""
    ptype    = _field(block, 'RelativePathType')
    path_raw = _field(block, 'Path')
    rel_raw  = _field(block, 'RelativePath')
    size     = _field(block, 'OriginalFileSize')

    abs_path = unescape(path_raw) if path_raw else ''
    rel_path = unescape(rel_raw) if rel_raw else ''
    name     = Path((abs_path or rel_path).replace('\\', '/')).name if (abs_path or rel_path) else ''
    pack     = unescape(_field(block, 'LivePackName') or '')

    # 1. PACK CONTENT (samples only) — left out by default (skip_packs True), collected
    #    only when collect_ableton_packs = true.
    #
    #    Follows Ableton's own Collect All and Save, which offers "Files from Factory Packs"
    #    as an INCLUDE checkbox left unticked by default: whoever you hand the set to most
    #    likely has the same Packs installed already, so copying them in would just bloat the
    #    bundle. The requirements report instead names which Packs the target needs. Switch it
    #    ON (collect_ableton_packs = true) to make the bundle fully standalone even on a
    #    machine without those Packs — cost measured on a real project: ~1.2% of bundle size.
    #    When ON it's all-or-nothing: the .als gives us NO way to tell a Pack bundled with a
    #    Live edition from one bought separately — both are just a LivePackName + an
    #    ableton.com/<id> installing to the same folder — so "collect only the risky ones" is
    #    unbuildable. Either every Pack sample is collected, or none is.
    #
    #    The user-facing switch is phrased POSITIVELY — "Include Ableton Pack content" in the
    #    GUI, collect_ableton_packs in [COLLECT], shipped OFF (false). Internally it becomes
    #    skip_packs (its inverse), so on the default OFF skip_packs is True and pack content
    #    is returned as ('pack', ...) below and never copied.
    #
    #    DETECTION: a NON-EMPTY <LivePackName>, not RelativePathType — Ableton writes
    #    type 5 (not 2) for Pack samples, so a type-only check missed them entirely.
    #    The tag is always present on a SampleRef and empty for everything else, which
    #    makes it an exact yes/no signal. Core Library is just Ableton's own Pack
    #    (LivePackId www.ableton.com/0) — same mechanism as any other Pack.
    #    ptype 2 is kept as a belt-and-braces fallback (never yet observed in the wild).
    #    M4L devices are gated separately (collect_m4l_devices, just below); User Library
    #    content (type 6) is never gated.
    if not is_m4l and skip_packs and (pack or ptype == '2'):
        return ('pack', None, name)

    # M4L DEVICES — collected by default; collect_m4l_devices = false skips copying the .amxd
    # and leaves the reference as-is (the requirements report lists it as a dependency). It's
    # all-or-nothing, for producers whose target machine already has their M4L devices.
    if is_m4l and skip_m4l:
        return ('m4l_skip', None, name)

    # 2. Inside the project? Identity = filename + OriginalFileSize (size 0 → name only)
    src = None
    if size and size != '0':
        src = idx_ns.get((name.lower(), int(size)))
    if src is None:
        hits = idx_n.get(name.lower())
        if hits:
            src = hits[0]
    if src is not None:
        return ('inside', src, name)

    # 3. Outside but resolvable at its recorded location? A sample that resolves inside the
    #    workspace "home zone" (keep_root — folders above the project) is left where it is:
    #    returned 'keep', not copied, its ref untouched. Samples only — M4L is always bundled.
    def verdict(p: Path) -> tuple:
        if keep_root is not None and not is_m4l and _within(p, keep_root):
            return ('keep', p, name)
        return ('outside', p, name)

    if abs_path and Path(abs_path).is_file():
        return verdict(Path(abs_path))
    if rel_path:
        p = (als_dir / rel_path).resolve()
        if p.is_file():
            return verdict(p)

    # 4. Nowhere to be found
    return ('missing', None, name)


def _tail_overlap(a_parts: list, b_parts: list) -> int:
    """How many trailing path components a_parts and b_parts share (case-insensitive already)."""
    n = 0
    for a, b in zip(reversed(a_parts), reversed(b_parts)):
        if a != b:
            break
        n += 1
    return n


def locate_missing(missing: list, project_root: Path, resolved_dirs: set,
                   backup_roots: list | None = None, budget_files: int = AUTO_TIER_BUDGET,
                   backup_budget: int = BACKUP_SEARCH_LIMIT, should_stop=None,
                   cli: bool = True) -> dict:
    """
    Try to find still-missing files on disk by filename + exact OriginalFileSize.

    missing       : list of (key, name, size, origin) — 'key' is opaque (the caller maps
                    it back); 'origin' is the file's recorded folder (Tier-3 seed) or None.
    resolved_dirs : folders where the project's already-resolved samples live (Tier 2).
    Returns ({key: found_path} for the ones located, drew_status_line) — the second flag
    lets the caller reuse the erased status line for its heading instead of adding a blank.

    One shared index + 'visited' set span every tier. Identity is (name, exact size), so a
    same-name / different-size file is ignored — not a match, not flagged. EXCEPTION: files
    with no stored size (OriginalFileSize 0 — Ableton freeze / record / consolidate output, and
    anything Ableton has already flagged Missing, which zeroes size+CRC) can't be size-matched.
    They fall back to filename, DISAMBIGUATED by the recorded relative <Path> stub
    (e.g. .../Samples/Processed/Freeze/<name>): among same-name candidates the one whose location
    best matches that subpath wins, so a repeated name resolves to the right file and an unrelated
    same-name file isn't grabbed. A genuine tie (no distinguishing subpath) stays offline — never
    a silent wrong relink. os.scandir yields the size for free on Windows. The automatic tiers
    (1–4, folders the user never chose) share one `budget_files` cap; the user's EXPLICIT backup
    locations (Tier 5, up to a few master-library / external-drive folders) EACH get their own,
    much larger `backup_budget` so a deliberate library sweep is never starved by the guesses
    (0 = unlimited). A drive-root/home block bounds every scan, and `should_stop` makes the whole
    search interruptible (the console/GUI Stop button).
    """
    want = {(n.lower(), int(s)): origin for _k, n, s, origin in missing if s and s != '0'}
    # Size-0 refs → matched by name alone (see docstring). want_n0 is constant; because a name
    # match can only be CONFIRMED unique after a full pass, its presence keeps scans from
    # early-exiting when the sized files are all found.
    want_n0 = {n.lower() for _k, n, s, _o in missing if not (s and s != '0')}
    if not want and not want_n0:
        return {}, False
    found, visited = {}, set()          # found: (name, size) -> path        (exact matches)
    cand_n0 = {}                         # name -> {candidate paths}          (name-only matches)
    left, budget = [len(want)], [budget_files]
    unsized = bool(want_n0)

    # Progress — a big backup scan can run for seconds with nothing on screen and look frozen.
    # ONE status line, updated in place (not stacked log lines): a spinner, the found/total
    # tally, and the folder being scanned. CLI redraws it with '\r' and erases it at the end;
    # the GUI can't render '\r', so it gets '§SEARCH§ found total folder' markers that app.js
    # turns into a single status element (cleared on 'done'). Gated by `shown`/seeded to 'now',
    # so a fast search that finishes within the interval prints nothing at all.
    SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    total = len(want) + len(want_n0)
    tick, spin, shown = [time.monotonic()], [0], [False]

    def term_w():
        # Fit the actual terminal so a long path never wraps or gets hard-cut; the '- 1' keeps
        # an exactly-full line from nudging the cursor onto the next row. Falls back to 96
        # (the header's width) when there's no real terminal.
        return shutil.get_terminal_size((96, 24)).columns - 1

    def n_found():
        # confirmed sized matches + size-0 names that now have at least one candidate on disk
        return len(found) + sum(1 for nm in want_n0 if nm in cand_n0)

    def heartbeat(d):
        now = time.monotonic()
        if now - tick[0] < 0.15:          # ~7 redraws/sec — enough to animate the spinner
            return
        tick[0] = now
        if cli:
            if not shown[0]:              # first frame: a blank line above so the status line
                print()                   # isn't glued to the box printed just before it
            frame = SPINNER[spin[0] % len(SPINNER)]
            spin[0] += 1
            width  = term_w()
            prefix = f"{frame}  Searching for missing samples  ({n_found()}/{total})  "
            avail  = max(10, width - len(prefix))     # room left for the path
            path   = str(d)
            if len(path) > avail:                     # keep the tail — it's the most specific part
                path = '…' + path[-(avail - 1):]
            print('\r' + (prefix + path).ljust(width)[:width], end='', flush=True)
        else:
            print(f"§SEARCH§\t{n_found()}\t{total}\t{d}")
        shown[0] = True

    def heartbeat_close():
        if not shown[0]:                  # nothing was ever drawn (instant search) — stay silent
            return
        print('\r' + ' ' * term_w() + '\r', end='', flush=True) if cli else print("§SEARCH§\tdone")

    def scan(root):
        """Recursively scandir `root`; dedups via `visited`, spends the shared budget, skips
        Backup / drive-roots / home. Early-exits once every SIZED file is found — unless size-0
        files are pending, which need a full pass to prove each name is unique."""
        if (left[0] == 0 and not unsized) or budget[0] <= 0 or root is None:
            return
        if should_stop is not None and should_stop():   # Stop pressed — bail out of every tier
            return
        try:
            root = root.resolve()
        except OSError:
            return
        if not root.is_dir() or root == Path(root.anchor) or root == Path.home():
            return
        stack = [root]
        while stack and budget[0] > 0 and (left[0] or unsized):
            if should_stop is not None and should_stop():
                return
            d = stack.pop()
            if d in visited:
                continue
            visited.add(d)
            heartbeat(d)
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name not in EXCLUDE_DIRS:
                            stack.append(Path(e.path))
                    else:
                        budget[0] -= 1
                        nm = e.name.lower()
                        k  = (nm, e.stat().st_size)
                        if k in want and k not in found:
                            found[k] = Path(e.path)
                            left[0] -= 1
                            if left[0] == 0 and not unsized:
                                return
                        if nm in want_n0:
                            cand_n0.setdefault(nm, set()).add(Path(e.path))
                        if budget[0] <= 0:
                            return
                except OSError:
                    continue

    # Tier 1 — the project subtree.
    scan(project_root)
    # Tier 2 — where resolved samples live, plus up to 2 levels above each (the pack /
    #          library container — reaches sibling categories & packs), deduped. This
    #          holistic pass places the bulk in the common "library moved" case; the
    #          shared budget keeps it from running away into a giant library.
    for d in resolved_dirs:
        for r in (d, d.parent, d.parent.parent):
            scan(r)
    # Tier 3 — the project's parent folders, up to 2 levels up (samples kept next to, or a
    #          level above, the project — a very common layout). A single BOUNDED region,
    #          so it goes before the far-flung per-orphan hunt below. (A grandparent that's
    #          a drive-root is skipped by scan()'s own guard.)
    for r in (project_root.parent, project_root.parent.parent):
        scan(r)
    # Tier 4 — LAST RESORT: per orphan, bottom-up rings from its own recorded origin (seed
    #          the nearest existing ancestor, then widen up to 3 levels; 'visited' makes each
    #          ring hit only the new siblings). This is the potentially-massive step — it can
    #          expand into an orphan's original pack/library, and runs once per orphan — and
    #          its "still in its library" case is largely already covered by Tier 2, so it
    #          runs only after every cheaper, bounded tier has missed.
    for (n, s), origin in want.items():
        if (n, s) in found or origin is None:
            continue
        seed = origin
        while seed is not None and not seed.exists():
            seed = seed.parent
        node, up = seed, 0
        while node is not None and up <= 3 and (n, s) not in found:
            scan(node)
            node, up = node.parent, up + 1
    # ── Tier 5 — the user's backup search folders from config.ini ([COLLECT]
    #    backup_search_location), up to a few folders they point at their master sample library,
    #    a projects root or an external drive. Runs last because it is the broadest region — but
    #    because it is the user's DELIBERATE choice (not a guess), EACH gets its OWN, much larger
    #    budget instead of the scraps the automatic tiers leave behind: refill the counter before
    #    each so neither Tiers 1–4 nor an earlier backup folder can starve a later one.
    #    0 = unlimited (bounded then only by Stop and the drive-root guard).
    for backup_root in (backup_roots or []):
        if backup_root is not None and backup_root.is_dir():
            budget[0] = backup_budget if backup_budget > 0 else float('inf')
            scan(backup_root)
    heartbeat_close()   # erase the status line before the caller prints the outcome

    result = {k: found[(n.lower(), int(s))]
              for k, n, s, _o in missing
              if s and s != '0' and (n.lower(), int(s)) in found}
    # Size-0 files: resolve by name, but let the recorded relative <Path> pick the right one.
    for k, n, s, origin in missing:
        if s and s != '0':
            continue
        paths = cand_n0.get(n.lower())
        if not paths:
            continue
        # Score each same-name candidate by how much of the recorded relative path it matches
        # from the end (filename always counts as 1). rel = origin folder + filename.
        rel_parts = ((origin / n) if origin else Path(n)).as_posix().lower().split('/')
        scored  = sorted(((_tail_overlap(p.as_posix().lower().split('/'), rel_parts), p)
                          for p in paths), key=lambda t: t[0], reverse=True)
        best    = scored[0][0]
        winners = [p for m, p in scored if m == best]
        # Accept only a single clear winner: the subpath pins it (best ≥ 2 = filename + at least
        # one parent folder), or there's just one same-name candidate anywhere. A tie is skipped.
        if len(winners) == 1 and (best >= 2 or len(paths) == 1):
            result[k] = winners[0]
    return result, shown[0]


def prompt_missing_decision(missing: list, decide=None, cli: bool = True) -> bool:
    """
    Pre-flight decision for samples / M4L devices that are STILL missing after the
    tiered auto-find. Lists each (name, size, last-seen path), then asks ONE global,
    all-or-nothing Y/n question — Enter or 'y' collects everything else and leaves the
    missing offline; 'n' cancels (nothing written). Returns True to proceed, False to
    abort. Returns True immediately when nothing is missing.

    The list is always PRINTED (so it streams to whatever is reading stdout — terminal
    or the GUI console). Only the yes/no gate is pluggable: `decide` is called with the
    deduped [(name, size, origin), ...] and returns True to proceed. When None we fall
    back to the terminal input() prompt — so the CLI is unchanged and the GUI can hand
    in a dialog instead of blocking on a keyboard that isn't there.
    """
    if not missing:
        return True

    # Dedup: a file referenced by many clips should appear once.
    uniq = {}
    for name, size, origin in missing:
        uniq.setdefault((name.lower(), size), (name, size, origin))
    listed = sorted(uniq.values(), key=lambda t: t[0].lower())

    # Name and location on separate lines — a full sample path overflows 80 chars.
    print(f"\n  {'─' * DIVIDER_W}")
    print(f"  ⚠  {len(uniq)} file(s) could not be found and will stay OFFLINE:\n")
    for i, (name, size, origin) in enumerate(listed):
        if i:
            print()   # blank line between entries so each file stands apart
        human = human_bytes(int(size)) if size and size != '0' else ''
        loc   = origin or '(no location recorded)'
        if cli:
            print(f"      {name}{f'    {human}' if human else ''}")   # size only when known
            print(f"        expected at: {loc}")
        else:
            # GUI: shallower indent (the console is narrower, so deep indent makes the path
            # wrap sooner) and size inline in parens only when known — a lone trailing '—'
            # for freeze/record files with no stored size reads as a stray dash.
            print(f"  {name}{f'  ({human})' if human else ''}")
            print(f"    expected at: {loc}")
    # CLI: manual two-line wrap keeps the tip tidy at ~80 cols. GUI: one flowing line, so the
    # console wraps it itself (a hard break + continuation indent looks broken there) — and it
    # points at the Collect panel's field, not config.ini, since that's where the GUI sets it.
    if cli:
        print(f"\n  Tip: if a file should exist, set the backup search location in config.ini at your")
        print(f"       master sample folder or external hard drive, then re-run to search there too.")
    else:
        print(f"\n  Tip: if a file should exist, set the Backup search location in the Collect panel to "
              f"your master sample folder or external drive, then re-run to search there too.")
    print(f"  {'─' * DIVIDER_W}")

    if decide is not None:
        return decide(listed)

    print()
    print(f"  Collect everything else and leave {'it' if len(uniq) == 1 else 'them'} offline?  [Y/n]")
    return input("  > ").strip().lower() not in ("n", "no")


def extract_plugins(xml: str) -> list:
    """
    Find every 3rd-party plugin instance in the project — VST2/VST3 (PluginDevice)
    and Audio Units (AuPluginDevice). Native Live devices are deliberately excluded.
    Reuses the processor's extract_device_name(), which already knows where each
    plugin format hides its name (VST2 PlugName vs VST3/AU BrowserContentPath).
    Returns [(name, format), ...] sorted by name — what you need to reinstall it, and
    nothing more. How many instances a set uses is irrelevant to that question.
    """
    plugins = set()
    for tag in PLUGIN_TAGS:
        for _s, _e, block in find_blocks(xml, tag):
            name = extract_device_name(block, tag)
            if not name:
                continue
            # Format matters when re-installing on a new machine — VST2 ≠ VST3 ≠ AU
            fmt = 'AU' if tag == 'AuPluginDevice' else ('VST3' if 'Vst3PluginInfo' in block else 'VST2')
            plugins.add((name.replace('[ext] ', ''), fmt))

    return sorted(plugins, key=lambda t: t[0].lower())


def extract_packs(xml: str) -> list:
    """
    Distinct Ableton Packs a set draws on → [(pack_name, pack_id), ...] sorted by name.

    Pack membership is marked by a non-empty <LivePackName> on the SampleRef's FileRef.
    That tag is always present and is empty for every non-Pack sample, so it is an exact
    yes/no signal — unlike RelativePathType, which reports Pack samples as type 5.
    Core Library is simply Ableton's own Pack (www.ableton.com/0); add-on Packs carry
    their real name and a unique id, e.g. "Trap Drums by Sound Oracle" / .../313.

    This lists which Packs a set draws on regardless of whether their samples get collected
    — provenance only, kept so a report can name them without re-deriving it.
    """
    packs = set()
    for _s, _e, block in find_blocks(xml, 'SampleRef'):
        name = unescape(_field(block, 'LivePackName') or '')
        if name:
            packs.add((name, unescape(_field(block, 'LivePackId') or '')))

    return sorted(packs, key=lambda t: t[0].lower())


def uncollected_packs(analysis: dict) -> list:
    """
    Packs whose samples were left OUT of the bundle (collect_ableton_packs = false) → [(name, id), ...].

    These are a real dependency: without them installed, those samples are offline on the
    target machine. Core Library is deliberately excluded — it ships with every Live
    install, so naming it would be noise rather than something to act on.
    Returns [] only when Pack content was included (collect_ableton_packs = true); on a
    default collect it names the skipped Packs, which then are a real dependency.
    """
    packs = {}
    for i, (_s, _e, _kind, cat, _src, _name) in enumerate(analysis['infos']):
        if cat != 'pack':
            continue
        blk  = analysis['tagged'][i][3]
        name = unescape(_field(blk, 'LivePackName') or '')
        if name and name != CORE_LIBRARY:
            packs[name] = unescape(_field(blk, 'LivePackId') or '')

    return sorted(packs.items(), key=lambda t: t[0].lower())


REPORT_BAR = '═' * SECTION_BAR_W

# Named for what a producer actually needs to DO with it: the '@ ' prefix sorts it to the
# top of the bundle folder, and the name says these must be present before the set opens.
# Deliberately NOT the processor's '@ External Plugins List.txt' — this one also covers
# Max for Live devices and Packs, and the two files serve different purposes.
REPORT_NAME = '@ Required Plugins & Packs.txt'


def _split_report_entries(text: str) -> list:
    """
    Split an existing report back into [(als_name, block_text), ...].

    An entry begins at a boxed header whose title is a .als filename. The section
    headers inside an entry (EXTERNAL PLUGINS / MAX FOR LIVE DEVICES) never end in
    '.als', so they can never be mistaken for an entry boundary.
    """
    lines  = text.splitlines()
    starts = [i for i in range(len(lines) - 2)
              if lines[i] == REPORT_BAR and lines[i + 2] == REPORT_BAR
              and lines[i + 1].strip().lower().endswith('.als')]

    return [(lines[i + 1].strip(),
             '\n'.join(lines[i:starts[n + 1] if n + 1 < len(starts) else len(lines)]).rstrip('\n'))
            for n, i in enumerate(starts)]


def build_plugin_block(title: str, plugins: list, m4l: list, packs: list, creator: str) -> list:
    """
    Build the report block for one .als, as a list of lines.

    This report is always an ADD-ON to an actual collect — it is never produced on its
    own — so every status here can speak in terms of what the collect did.

    Sections, each rendered only when it has something actionable to say:
      EXTERNAL PLUGINS      3rd-party VST2/VST3/AU that MUST already be installed on
                            the target machine — the real portability risk.
      MAX FOR LIVE DEVICES  ALWAYS listed when present, even though the .amxd's are
                            collected into the bundle. Shipping the file is not enough:
                            loading it needs Max for Live, which comes with Live Suite
                            but is a paid add-on on Standard — so this is as real a
                            requirement as any plugin. Any that failed to copy are
                            flagged too, so they don't fail silently later.
      REQUIRED PACKS        Shown when Pack content was left OUT (collect_ableton_packs =
                            false — the default): those Packs are then a real dependency on
                            the target machine. When it IS collected instead, nothing is
                            required, so the section is omitted. Core Library is never
                            listed: it ships with Live.
    """
    # Section header, same shape as the processor's reports: bars with no blank line
    # before the content that follows.
    def header(t: str) -> list:
        return [REPORT_BAR, f'  {t}', REPORT_BAR]

    # Label column = longest label + 1, so every colon lines up with one space before
    # it — the same rule the processor's PROJECT SUMMARY uses.
    labels = ('Collected', 'Creator')
    W      = max(len(l) for l in labels) + 1
    stamp  = datetime.now().strftime('%Y-%m-%d %H:%M')

    lines  = header(title)
    lines += [f'  {"Collected":<{W}}: {stamp}',
              f'  {"Creator":<{W}}: {creator}', '']

    # One name width shared by the two-column sections, so their right-hand column lines
    # up across the report instead of jumping. (Packs are a bare list — no second column —
    # so their names are excluded here, or a long Pack name would pad everything else.)
    width = max([len(n) for n, _f in plugins] + [len(n) for n, _st in m4l] + [0])

    if plugins:
        lines += header('EXTERNAL PLUGINS')
        for name, fmt in plugins:
            lines.append(f"  {name:<{width}}   {fmt}")
        lines.append('')

    if m4l:
        lines += header('MAX FOR LIVE DEVICES')
        for name, status in m4l:
            # Always show the status — 'included' / 'skipped' / 'not found' — so it's clear at a
            # glance what happened to each device (the mirror of the samples' collected/missing).
            lines.append(f"  {name:<{width}}   {status}")
        lines.append('')
        if (failed := sum(1 for _n, st in m4l if st == 'not found')):
            lines.append(f"  WARNING: {failed} M4L device(s) could not be collected — install them manually.")
        if any(st == 'skipped' for _n, st in m4l):
            # Intentionally left out (collect_m4l_devices off) — a real dependency on the target.
            lines += ['  Note: these were left out — the target machine must already have',
                      '  them (User Library or the same path), and needs Max for Live to',
                      '  load them (incl with Suite, paid add-on for Standard).', '']
        else:
            lines += ['  Note: the .amxd files are included in the bundle, but Max for Live must be',
                      '  installed to load them (incl with Suite, paid add-on for Standard)', '']

    if packs:
        lines += header('REQUIRED PACKS')
        for name, _pack_id in packs:      # name only — the LivePackId is internal noise
            lines.append(f"  {name}")
        lines += ['',
                  '  Note: these samples were not included. Install these Packs on the',
                  '  target machine or they will be offline.', '']

    return lines


def write_plugin_report(out_als: Path, plugins: list, m4l: list, packs: list,
                        creator: str) -> Path | None:
    """
    Write the requirements manifest as REPORT_NAME, alongside the collected .als.
    Deliberately NOT named after the .als: a project can end up with several collected
    .als files, and its plugin dependencies belong to the project as a whole.

    Returns the report path — or None when the project has no 3rd-party dependencies
    at all, since an empty manifest is pure noise and is simply never created.
    """
    if not plugins and not m4l and not packs:
        return None

    lines  = build_plugin_block(out_als.name, plugins, m4l, packs, creator)
    report = out_als.with_name(REPORT_NAME)

    # Multi-.als aware: one project can be collected several times (different sets, all
    # landing in the same folder in in-place mode). Each .als contributes its own full
    # block; the newest collect goes to the top, and re-collecting the same .als replaces
    # its previous block instead of duplicating it.
    block = '\n'.join(lines).rstrip('\n')
    older = ([b for n, b in _split_report_entries(report.read_text(encoding='utf-8'))
              if n.lower() != out_als.name.lower()] if report.is_file() else [])

    report.write_text('\n\n\n'.join([block] + older) + '\n', encoding='utf-8')
    return report


# ── Discovery / grouping for COLLECT ─────────────────────────────────────────
# SKIP_DIRS (the Ableton-owned folders) is shared with the processor — same rule,
# one definition. Collect adds one extra rule of its own: skip *_collected.als.
VERSION_CHOICES = ('1', '3', '5', 'all')

# workspace_levels_up ceiling — matches the GUI's Off/1/2/3 control (COLLECT_WORKSPACE_LEVELS
# in app.js). A hand-edited config value above this is clamped, so 3 is a true maximum
# everywhere; deeper than that risks a home zone that swallows unrelated projects.
MAX_WORKSPACE_LEVELS = 3


def find_collect_als(target: Path) -> list:
    """
    Find the .als files to COLLECT. Same idea as the processor's find_als_files(), plus
    two collect-specific rules:
      • skip Ableton-owned folders (Backup/, Ableton Project Info/, Samples/, Presets/)
      • skip our own *_collected.als output, so a second run can't collect a collect
        (in-place mode writes <stem>_collected.als straight into the project folder)
    Normal processing deliberately still SEES _collected files — the user may well want
    to process those too. Only collect excludes them.
    """
    if target.is_file():
        return [target]

    return sorted(f for f in target.rglob('*.als')
                  if not any(p in SKIP_DIRS for p in f.parts)
                  and not f.stem.endswith(('_collected', '_processed')))


def _fmt_versions(skipped: list) -> str:
    """The distinct Live versions actually present in a skipped list, for the notice —
    '9' / '9 & 10' / '8, 9 & 10'. Only versions really found are named (never a guessed range)."""
    vs = sorted({v for _f, v in skipped})
    return str(vs[0]) if len(vs) == 1 else ', '.join(map(str, vs[:-1])) + f' & {vs[-1]}'


def split_supported_units(units: list) -> tuple[list, list]:
    """Partition collect units by Live version. Live 9/10 sets store sample references in a
    different <FileRef> schema this tool can't read (see als_core.MIN_SUPPORTED_LIVE), so
    they're pulled out here to be REFUSED with a clear message rather than silently misread
    as "everything is missing". Reads only each .als header, not the whole (often huge) set.

    Returns (supported_units, skipped) where skipped is [(als_path, live_version), ...].
    A unit that loses only some of its versions keeps the rest; one that loses all of them
    drops out entirely. A version we can't read off the header is left in (proceeds).
    """
    supported, skipped = [], []
    for proot, is_real, files in units:
        keep = []
        for f in files:
            v = detect_live_version(read_als_header(f))
            if v is not None and v < MIN_SUPPORTED_LIVE:
                skipped.append((f, v))
            else:
                keep.append(f)
        if keep:
            supported.append((proot, is_real, keep))
    return supported, skipped


def group_collect_targets(als_files: list, versions: str) -> list:
    """
    Group discovered .als files into collect units → [(project_root, is_real, [als...])].

    REAL project (an 'Ableton Project Info' folder sits beside the .als): every .als in
    that folder is treated as a version iteration of the same song — newest first, then
    trimmed to `versions`. They collect TOGETHER into the project folder, sharing one
    Samples/Collected, one Presets/Collected and one plugin report.

    LOOSE .als: each is its own unit and is never batched. Nothing on disk tells us which
    loose files belong to the same project, and guessing wrong would merge unrelated
    songs into one bundle — so `versions` deliberately does not apply to them.

    "Most recent" is decided by mtime, not filename: real-world set naming is far too
    messy to parse (song.als / song_v2.als / song final.als / song FINAL2.als).
    """
    real, loose = {}, []
    for f in als_files:
        (real.setdefault(f.parent, []) if (f.parent / PROJECT_MARKER).is_dir()
         else loose).append(f)

    units = []
    for root, files in real.items():
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        units.append((root, True, files if versions == 'all' else files[:int(versions)]))
    units += [(f.parent, False, [f]) for f in loose]

    return units


def analyze_als(als_path: Path, project_root: Path, idx_ns: dict, idx_n: dict,
                skip_packs: bool, skip_m4l: bool, keep_root: Path | None = None) -> dict:
    """
    Read one .als and classify every sample / M4L reference. Pure analysis — no disk
    writes, no locating yet — so a batch can pool every version's results before it
    commits to anything.

    Samples (audio + video) live in <SampleRef>; Max-for-Live devices in <MxPatchRef>.
    Both wrap an identical inner <FileRef>, so one classifier handles both. Scoping to
    MxPatchRef (not the whole device) targets only the primary .amxd ref, skipping the
    provenance FileRefs in SourceContext — exactly as SampleRef isolates sample refs.
    """
    xml    = load_als_xml(als_path)
    tagged = ([('sample', s, e, c) for (s, e, c) in find_blocks(xml, 'SampleRef')]
              + [('m4l', s, e, c) for (s, e, c) in find_blocks(xml, 'MxPatchRef')])
    infos  = [(s, e, kind, *classify_ref(c, project_root, idx_ns, idx_n, skip_packs, skip_m4l,
                                         is_m4l=(kind == 'm4l'), keep_root=keep_root))
              for (kind, s, e, c) in tagged]     # → (start, end, kind, category, src, name)

    return {'als': als_path, 'xml': xml, 'tagged': tagged, 'infos': infos}


def show_path(p: Path, root: Path) -> str:
    """Path as displayed in the terminal: relative to the run's source folder when it sits
    under it, absolute otherwise. The settings block prints that folder once, in full."""
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(p.resolve())


def claim_dest(out_dir: Path, folder: str, src: Path, used: dict) -> str:
    """
    Pick the destination filename for one collected file inside `folder`, disambiguating
    against BOTH the names this batch has already claimed (`used[folder]`) AND files already
    sitting in that folder ON DISK from an earlier in-place collect.

    The on-disk check is SIZE-AWARE, and that is the whole point:
      • same name + same byte size  → the SAME file (the identity the tool uses everywhere —
        the resume-skip, the project index). The name is reused, so a genuine re-collect
        stays idempotent: the copy phase sees a matching file already there and skips it.
      • same name + different size   → a DIFFERENT file. The name is treated as taken and the
        newcomer is renamed (stem_1, stem_2, …) rather than overwriting it — which would
        corrupt the older reference still pointing at that path.
    A name-only seed would have broken resume (the same external file, still external in the
    untouched original .als, would be renamed on every re-run and duplicated endlessly);
    matching on size is what keeps resume idempotent while still never clobbering a different
    file. On a first collect the folder doesn't exist yet, so this reduces to the in-batch
    check alone. Returns the bundle-relative "folder/filename" and records it in `used`.
    """
    names    = used.setdefault(folder, set())
    dest_dir = out_dir / folder
    try:
        src_size = src.stat().st_size
    except OSError:
        src_size = -1

    def taken(fn: str) -> bool:
        if fn.lower() in names:               # already claimed by this batch
            return True
        p = dest_dir / fn
        try:                                  # on disk from a prior run?
            return p.is_file() and p.stat().st_size != src_size   # different file → keep clear
        except OSError:
            return True                       # unreadable → play safe, don't claim it

    fn = src.name
    if taken(fn):
        n = 1
        while taken(f"{src.stem}_{n}{src.suffix}"):
            n += 1
        fn = f"{src.stem}_{n}{src.suffix}"
    names.add(fn.lower())
    return f"{folder}/{fn}"


def plan_collect(als_list: list, project_root: Path, is_real_project: bool, out_dir: Path,
                 skip_packs: bool, skip_m4l: bool, display_root: Path,
                 backup_roots: list | None = None, should_stop=None, cli: bool = True,
                 workspace_levels: int = 0) -> dict:
    """
    ANALYSE one or more .als files destined for ONE output folder, and return a plan.
    Touches nothing on disk — so every unit in a run can be planned first, the user
    asked ONCE about everything that is missing, and only then does anything execute.

    REAL project → als_list holds the chosen version iterations and out_dir is the
                   project folder itself (in-place), so every version shares one
                   Samples/Collected, one Presets/Collected and one plugin report.
    LOOSE .als   → als_list is always a single file, and out_dir is its own bundle.

    Batching is about CORRECTNESS, not just speed. One shared assignment map means a file
    used by several versions is copied once — and, more importantly, two DIFFERENT files
    that happen to share a filename can never overwrite each other in the shared
    Collected folder, which is exactly what independent per-version runs would do.
    """
    # Real project → index the folder so already-internal samples are kept in place.
    # Loose → empty index, so nothing counts as 'inside' and every sample is collected.
    idx_ns, idx_n = build_project_index(project_root) if is_real_project else ({}, {})

    # The workspace "home zone" — samples living in the folders AROUND the project are kept in
    # place, never copied. Real projects only: a loose .als has no surrounding project to anchor
    # to, and it collects everything into its own bundle by design.
    keep_root = workspace_root(project_root, workspace_levels) if is_real_project else None

    # ── Phase 1: analyse every version up front ──────────────────────────────────
    analyses = [analyze_als(a, project_root, idx_ns, idx_n, skip_packs, skip_m4l, keep_root)
                for a in als_list]

    # ── Phase 2: ONE auto-find pass (Tiers 1–4) across all versions ──────────────
    # Pooling the orphans means the potentially-large disk scan runs once for the whole
    # batch on a single shared budget, instead of repeating per version. Each hit is
    # reclassified 'outside' (its found path becomes the collect source) so it flows
    # through the rest like any normal file. Genuinely-missing ones stay 'missing'.
    resolved_dirs, to_find = set(), []
    for fi, an in enumerate(analyses):
        for i, (_s, _e, _k, cat, src, name) in enumerate(an['infos']):
            if cat in ('inside', 'outside', 'keep') and src is not None:
                resolved_dirs.add(src.parent)
            elif cat == 'missing':
                blk    = an['tagged'][i][3]
                origin = unescape(_field(blk, 'Path') or '')
                to_find.append(((fi, i), name, _field(blk, 'OriginalFileSize'),
                                Path(origin).parent if origin else None))
    # (No phase label here — the search is usually instant. If it runs long on a big backup,
    #  locate_missing shows its own in-place status line while walking; the outcome is reported
    #  as the 'Recovered' box row below and, for anything still lost, the missing-file prompt.)
    located, search_drew = (locate_missing(to_find, project_root, resolved_dirs, backup_roots,
                                            should_stop=should_stop, cli=cli)
                            if to_find else ({}, False))
    n_recovered = len(set(located.values()))   # unique offline files the search rescued
    for (fi, i), path in located.items():
        s, e, kind, _cat, _src, name = analyses[fi]['infos'][i]
        # A recovered sample sitting in the home zone is RELINKED in place (its ref repointed
        # to where it was found, no copy); anything else is collected into the bundle as usual.
        new_cat = ('relink' if (keep_root is not None and kind != 'm4l'
                                and _within(path, keep_root)) else 'outside')
        analyses[fi]['infos'][i] = (s, e, kind, new_cat, path, name)

    # ── Phase 3: ONE shared assignment map for the whole batch ───────────────────
    # Keyed by resolved source path, so the same physical file always maps to the same
    # destination (dedup across versions), and the per-folder collision namespace is shared,
    # so a name clash between two different files is resolved once, globally. Disambiguation
    # (claim_dest) also guards against files already sitting in the destination Collected/
    # folders from an earlier in-place collect — see claim_dest for the size-aware reasoning.
    # Samples → Samples/Collected/ , M4L devices → Presets/Collected/ (flat, mirrors samples).
    assignments, used, kind_of = {}, {}, {}
    for an in analyses:
        for _s, _e, kind, cat, src, _name in an['infos']:
            if cat not in ('inside', 'outside') or src in assignments:
                continue
            kind_of[src] = kind   # so the terminal tallies can split samples from M4L
            if cat == 'inside':
                assignments[src] = src.relative_to(project_root).as_posix()
                continue
            folder = 'Samples/Collected' if kind == 'sample' else 'Presets/Collected'
            assignments[src] = claim_dest(out_dir, folder, src, used)

    # ── Phase 4: pooled tallies + one deduped missing list ───────────────────────
    # Everything below counts unique FILES, never references — one sample used by 30
    # clips is one file.
    missing = {}    # keyed, so a file missing in several versions is reported once
    gone    = {'sample': set(), 'm4l': set()}
    for an in analyses:
        for i, (_s, _e, kind, cat, _src, name) in enumerate(an['infos']):
            if cat != 'missing':
                continue
            blk = an['tagged'][i][3]
            # Once Live flags a file Missing (RelativePathType 0) it blanks RelativePath
            # and leaves only a PROJECT-RELATIVE stub in Path — so anchor any relative
            # value to the project root to report the full location it's expected at.
            raw  = unescape(_field(blk, 'Path') or '')
            loc  = Path(raw) if raw else None
            if loc is not None and not loc.is_absolute():
                loc = project_root / loc
            size = _field(blk, 'OriginalFileSize')
            missing.setdefault((name.lower(), size),
                               (name, size, show_path(loc, display_root) if loc else ''))
            gone[kind].add((name.lower(), size))
    missing = list(missing.values())

    # Home-zone samples left in place (workspace_levels_up): 'keep' (already linked) and
    # 'relink' (was Missing, reconnected to where it sits). Neither is copied. Counted by
    # unique source, so a file used by many clips/versions counts once. M4L never lands here.
    kept_src = {'sample': set(), 'm4l': set()}
    for an in analyses:
        for _s, _e, kind, cat, src, _name in an['infos']:
            if cat in ('keep', 'relink') and src is not None:
                kept_src[kind].add(src)

    # Which files will actually be copied, and how many bytes total?
    #   • src == dest (a real project's internal samples)          → nothing to do
    #   • dest already exists with matching size                   → a prior run
    #     already copied it (RESUME-SAFE after a crash) → skip
    #   • otherwise (absent, or partial/size-mismatched from crash) → copy
    to_copy, to_copy_bytes = [], 0
    uptodate = {'sample': 0, 'm4l': 0}   # already in the destination from an earlier run
    inproj   = {'sample': 0, 'm4l': 0}   # real project's own files — stay exactly where they are
    asd      = 0                         # .asd analysis siblings riding along
    for src, rel in assignments.items():
        k = kind_of[src]
        dest = out_dir / rel
        try:
            if src.resolve() == dest.resolve():
                inproj[k] += 1
                continue
            if dest.is_file() and dest.stat().st_size == src.stat().st_size:
                uptodate[k] += 1
            else:
                to_copy.append((src, dest))
                to_copy_bytes += src.stat().st_size

            # ── .asd analysis sibling ────────────────────────────────────────
            # Holds the sample's warp markers; without it Live re-analyses on load and
            # the clip plays at the wrong timing. Never referenced in the XML — Live
            # finds it by name — so it only has to land next to its sample.
            #
            # The suffix is APPENDED to the whole filename: 'kick.wav' → 'kick.wav.asd'.
            # Named from `dest`, not `src`, so a collision-renamed sample keeps its pair:
            # 'kick_1.wav' → 'kick_1.wav.asd'.
            if k == 'sample' and (asd_src := src.with_name(src.name + '.asd')).is_file():
                asd += 1
                asd_dest = dest.with_name(dest.name + '.asd')
                if not (asd_dest.is_file()
                        and asd_dest.stat().st_size == asd_src.stat().st_size):
                    to_copy.append((asd_src, asd_dest))
                    to_copy_bytes += asd_src.stat().st_size
        except OSError:
            pass
    # .get() — .asd entries are in to_copy but not in kind_of, so they are skipped here.
    copied = {'sample': 0, 'm4l': 0}
    for src, _dest in to_copy:
        if k := kind_of.get(src):
            copied[k] += 1

    # Free space on the destination drive (project_root always exists; out_dir may not yet)
    free = shutil.disk_usage(project_root).free

    # Which Ableton Packs this set draws on — shown as a count in the box, never as names.
    # By default their samples are left out (assumed already installed on the target), so the
    # count is just a heads-up of what won't be bundled unless collect_ableton_packs = true.
    packs = sorted({p for an in analyses for p in extract_packs(an['xml'])},
                   key=lambda t: t[0].lower())

    # ── The box: same shape as the processor's per-project summary ────────────────
    def tally(kind: str, noun: str = '') -> tuple[int, str]:
        """(total, '12 files  (9 collected | 2 kept | 1 missing)').

        'collected' folds together copied, already-in-destination and already-in-project
        — the byte cost of each is the Disk row's job. 'kept' is the home-zone files left in
        place. The breakdown is shown only when something isn't a plain collect (kept or missing).
        """
        ok    = copied[kind] + uptodate[kind] + inproj[kind]
        kept  = len(kept_src[kind])
        lost  = len(gone[kind])
        total = ok + kept + lost
        head  = f"{total} {noun}".rstrip()
        if kept or lost:
            seg = [f"{ok} collected"] + ([f"{kept} kept"] if kept else []) \
                                      + ([f"{lost} missing"] if lost else [])
            head += "  (" + " | ".join(seg) + ")"
        return total, head

    rows = []
    if is_real_project:   # which version(s) got picked — the whole point of the versions setting
        rows.append(('Versions', ', '.join(a.name for a in als_list)))
    rows.append(('Samples', tally('sample', 'file(s)')[1]))
    if n_recovered:   # samples Live had flagged offline that the tier/backup search found on disk
        rows.append(('Recovered', f"{n_recovered} offline sample{'s' if n_recovered != 1 else ''} "
                                  f"found on disk"))
    if asd:   # warp markers — worth stating, since losing them changes how a loop plays
        rows.append(('Analysis', f"{asd} .asd file(s)"))
    if (n_m4l := tally('m4l'))[0]:   # M4L devices collected (or missing) this run
        rows.append(('M4L devices', n_m4l[1]))
    elif (m4l_skipped := len({name.lower() for an in analyses
                              for (_s, _e, _k, cat, _src, name) in an['infos']
                              if cat == 'm4l_skip'})):   # collect_m4l_devices off, but the set uses some
        rows.append(('M4L devices', f"{m4l_skipped} (skipped — listed in report)"))
    if packs:
        rows.append(('Packs', str(len(packs))))
    extra = f", {ud} file(s) already there" if (ud := sum(uptodate.values())) else ''
    rows.append(('Disk', f"{human_bytes(to_copy_bytes)} to copy{extra}"))
    rows.append(('Added to' if is_real_project else 'New bundle', show_path(out_dir, display_root)))

    # Title on its own line, then the rows indented under it — the same shape the processing
    # run uses per file (title + '-' underline, no box). The GUI leans on its console CSS to
    # style the title, so it skips the underline there.
    # Title = the project's folder (a real project may hold several collected versions, so it
    # must NOT be titled by one version's filename) / the file's name (loose).
    name = project_root.name if is_real_project else als_list[0].name
    if cli:
        # When the search drew its status line, it already left a blank line above and an erased
        # line right here — put the heading on that erased line (no extra '\n') so a searched and
        # an instant project get the SAME single blank line above the heading.
        print(f"{'' if search_drew else chr(10)}{name}\n{PROJECT_RULE}\n")
    else:
        print(f"\n{name}")
    for label, value in rows:
        print(f"  {label:<{TERM_W}}: {value}")
    if missing:
        print(f"\n  ⚠  {len(missing)} file(s) could not be found — full list below, after all projects")

    # Planning stops here — nothing has touched the disk yet. The caller pools every
    # unit's `missing` into ONE decision before any unit is allowed to execute.
    return {'project_root': project_root, 'out_dir': out_dir,
            'is_real_project': is_real_project, 'analyses': analyses,
            'assignments': assignments, 'to_copy': to_copy, 'to_copy_bytes': to_copy_bytes,
            'free': free, 'missing': missing}


def _fmt_duration(seconds: float) -> str:
    """m:ss, or h:mm:ss once past an hour — for the copy bar's ETA."""
    s = int(seconds + 0.5)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class CopyProgress:
    """ONE progress bar for the whole copy phase, shared across every project. The label
    flicks through to whichever project is copying right now; speed and ETA are measured
    over the whole run, so they settle instead of resetting per file.

    CLI redraws a '\\r' bar in place (erased at the end, like tqdm leave=False). The GUI
    can't show a carriage-return bar in its line-buffered console, so it emits tab-tagged
    '§COPY§' lines that app.js turns into one animated bar element."""
    BAR_W  = 24
    LINE_W = 92   # pad/erase width; stays inside the header's 96-col layout

    def __init__(self, total_bytes: int, cli: bool = True):
        self.total     = total_bytes
        self.cli       = cli
        self.done      = 0
        self.start     = time.monotonic()
        self.last_tick = 0.0
        self.shown     = False   # has the CLI bar been drawn? (gates the blank line above it)

    def drop(self, n_bytes: int):
        """A project was skipped before copying (e.g. the disk gate) — drop its bytes from
        the total so the bar can still reach 100%."""
        self.total = max(0, self.total - n_bytes)

    def advance(self, n_bytes: int, label: str):
        self.done += n_bytes
        now = time.monotonic()
        if now - self.last_tick < 0.1:   # throttle redraws to ~10/s
            return
        self.last_tick = now
        frac    = (self.done / self.total) if self.total else 1.0
        elapsed = max(now - self.start, 1e-6)
        speed   = self.done / elapsed                    # bytes/sec, averaged over the run
        eta     = (self.total - self.done) / speed if speed > 0 else 0.0
        if self.cli:
            if not self.shown:            # first frame: a blank line above so the bar isn't glued
                print()                   # to the last project box printed just before it
            fill = round(self.BAR_W * frac)
            bar  = '=' * fill + ' ' * (self.BAR_W - fill)
            desc = f"Copying {label}"[:28]
            body = (f"{desc:<28}  [{bar}] {frac * 100:3.0f}%  "
                    f"{human_bytes(speed)}/s  ETA {_fmt_duration(eta)}")
            print('\r' + body.ljust(self.LINE_W)[:self.LINE_W], end='', flush=True)
        else:
            print(f"§COPY§\t{self.done}\t{self.total}\t{speed:.0f}\t{eta:.0f}\t{label}")
        self.shown = True

    def close(self):
        if self.cli:
            print('\r' + ' ' * self.LINE_W + '\r', end='', flush=True)
        else:
            print("§COPY§\tdone")


def execute_collect(plan: dict, plugin_report: bool = False, cli: bool = True,
                    progress: 'CopyProgress' = None):
    """
    Carry out one planned unit: disk gate → copy → rewrite → write .als → report.
    Runs only after the caller's single pooled missing-file decision said go.
    `progress` is the run-wide CopyProgress bar; this unit just advances it.
    """
    project_root = plan['project_root']
    out_dir, is_real_project = plan['out_dir'], plan['is_real_project']
    analyses, assignments = plan['analyses'], plan['assignments']
    to_copy, to_copy_bytes, free = plan['to_copy'], plan['to_copy_bytes'], plan['free']

    # Prints nothing on success — plan_collect's box already showed the numbers, and the
    # caller prints one confirmation line per unit. Only failures are reported here.

    # Gate: never start a copy we can't finish. (Small 1% headroom so we don't fill the drive.)
    if to_copy_bytes + to_copy_bytes // 100 > free:
        if progress:
            progress.drop(to_copy_bytes)   # not copying this one — keep the bar honest
        print(f"\n  ⚠  {out_dir.name}: NOT ENOUGH DISK SPACE — need {human_bytes(to_copy_bytes)}, "
              f"only {human_bytes(free)} free. Skipped (nothing copied, no .als written).")
        return None

    # Copy the files ONCE for the whole batch, in 1 MB chunks so the shared progress bar
    # advances even through a single large file (a 100 MB wav copied in one call would freeze
    # it), and so speed/ETA stay live. The bar spans the whole run — this unit only advances it,
    # setting the label to its own bundle so the text flicks through the projects as they copy.
    #   • Atomic: copy to a temp name in the destination, then os.replace() into place, so
    #     a crash never leaves a half-written file at the real path. A re-run re-copies any
    #     file whose size doesn't match and skips the finished rest (resume-safe).
    #   • TOCTOU: a source that vanished between analysis and now is reported, not fatal —
    #     the collect finishes everything it still can, and the file is flagged as skipped.
    skipped, copied_files, copied_bytes = [], 0, 0
    for src, dest in to_copy:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + '.part')
        try:
            if progress:
                progress.advance(0, out_dir.name)   # flick the label to this project
            with open(src, 'rb') as fsrc, open(tmp, 'wb') as fdst:
                while (buf := fsrc.read(1 << 20)):
                    fdst.write(buf)
                    if progress:
                        progress.advance(len(buf), out_dir.name)
            shutil.copystat(src, tmp)          # match shutil.copy2 (bytes + metadata)
            os.replace(tmp, dest)
            copied_files += 1
            copied_bytes += dest.stat().st_size
        except OSError as e:
            tmp.unlink(missing_ok=True)
            skipped.append((src.name, e))
    if skipped:   # leading '\n' steps off the shared bar's line before printing
        print(f"\n  ⚠  {out_dir.name}: {len(skipped)} file(s) could not be copied "
              f"(vanished or unreadable) — left offline:")
        for name, _e in skipped:
            print(f"        {name}")

    out_dir.mkdir(parents=True, exist_ok=True)
    # Ensure the Ableton Project Info marker exists — required for RelativePathType=3
    # paths to resolve (else samples go offline). No-op if already a real project.
    (out_dir / PROJECT_MARKER).mkdir(exist_ok=True)

    # Rewrite each version's refs against the shared map, and write its own .als
    for an in analyses:
        xml = an['xml']
        # reverse order so earlier offsets stay valid while splicing
        for start, end, _kind, cat, src, _name in sorted(an['infos'], key=lambda t: -t[0]):
            # 'keep' = a home-zone sample already linked correctly → leave its ref BYTE-UNTOUCHED
            # (the collected .als is written in place, so its existing relative path still resolves).
            if cat in ('missing', 'pack', 'm4l_skip', 'keep'):
                continue
            block = xml[start:end]
            if cat == 'relink':
                # Home-zone sample Ableton had flagged Missing: reconnect it to where it actually
                # sits — no copy. Document-relative (type 1), the same form as any external sample.
                # Resolve BOTH sides before relpath: the located src is already resolved, so an
                # unresolved out_dir (e.g. an 8.3 short path) would otherwise share no common
                # prefix and yield a giant ../../.. chain instead of a clean relative path.
                new_abs = src.resolve().as_posix()
                rel     = Path(os.path.relpath(new_abs, out_dir.resolve())).as_posix()
                block   = _set_field(block, 'RelativePathType', '1')
            else:   # 'inside' / 'outside' — collected into the bundle, repointed project-relative
                rel     = assignments[src]
                new_abs = (out_dir / rel).resolve().as_posix()
                block   = _set_field(block, 'RelativePathType', '3')
            block = _set_field(block, 'RelativePath', _xml(rel))
            block = _set_field(block, 'Path', _xml(new_abs))
            xml = xml[:start] + block + xml[end:]
        an['xml']     = xml
        an['out_als'] = out_dir / f"{an['als'].stem}_collected.als"
        with gzip.open(an['out_als'], 'wb') as f:
            f.write(xml.encode('utf-8'))

    # Optional 3rd-party plugin manifest — ONE shared file beside the collected .als files.
    # Written OLDEST → NEWEST because each write prepends its own block, so the most recent
    # version ends up on top. (Path rewriting never touches PluginDevice blocks, so scanning
    # the rewritten xml here gives the same result as scanning the original.)
    wrote_report = False
    if plugin_report:
        for an in reversed(analyses):
            plugins = extract_plugins(an['xml'])
            # M4L status comes straight from the classification: anything that resolved was
            # copied into the bundle, anything still 'missing' never made it.
            # Path().stem drops the .amxd — every device here is one, so the extension
            # carries no information and just clutters the list (plugins show no extension either).
            m4l_dev = sorted({(Path(name).stem,
                               'included'     if cat in ('inside', 'outside')
                               else 'skipped' if cat == 'm4l_skip'          # collect_m4l_devices off
                               else 'not found')                            # 'missing' — couldn't locate the .amxd
                              for _s, _e, kind, cat, _src, name in an['infos'] if kind == 'm4l'},
                             key=lambda t: t[0].lower())
            # By default (packs left out) this lists the required Packs; it's empty only when
            # collect_ableton_packs = true copied them in, so nothing is required.
            packs   = uncollected_packs(an)
            creator = (m.group(1) if (m := re.search(r'<Ableton\b[^>]*\bCreator="([^"]+)"',
                                                     an['xml'][:500])) else '?')
            report  = write_plugin_report(an['out_als'], plugins, m4l_dev, packs, creator)
            wrote_report = wrote_report or report is not None

    # Returned rather than printed: the caller pads all units together so the columns align.
    # Counts reflect what was actually copied, so a TOCTOU skip above lowers them honestly.
    return {'out_dir': out_dir.resolve(), 'files': copied_files,
            'bytes': copied_bytes, 'report': wrote_report}


def load_collect_settings(config) -> dict:
    """Read the [COLLECT] section into a plain dict, applying safe defaults for any absent
    key: Packs left out, M4L devices collected, report on, no backup location, newest
    version only. Inline '# comments' are stripped the same way the processor's loaders do."""
    sec = config['COLLECT'] if 'COLLECT' in config else {}

    def raw(key: str) -> str:
        return sec.get(key, '').split('#')[0].strip()

    def flag(key: str, default: bool) -> bool:
        v = raw(key).lower()
        return default if v == '' else v == 'true'

    def intval(key: str, default: int) -> int:
        try:
            return max(0, int(raw(key) or default))
        except ValueError:
            return default

    versions = raw('versions') or '1'
    if versions not in VERSION_CHOICES:
        versions = '1'
    # Any number of folders, stored pipe-delimited in the one key (a lone path with no '|' still
    # parses as a single location, so old configs keep working). '|' is illegal in Windows paths
    # and vanishingly rare on macOS, so it's a safe separator.
    backup_roots = [Path(p) for p in
                    (s.strip() for s in raw('backup_search_location').split('|')) if p]

    return {
        'versions':      versions,
        'workspace_levels': min(MAX_WORKSPACE_LEVELS, intval('workspace_levels_up', 0)),  # 0 = off; capped at 3 (GUI parity)
        'skip_packs':    not flag('collect_ableton_packs', False),   # default: leave Packs out
        'skip_m4l':      not flag('collect_m4l_devices', True),      # default: collect M4L devices
        'plugin_report': flag('write_report', True),
        'backup_roots':  backup_roots,
    }


def run_collect(root: Path, config, *, on_confirm_start=None, on_confirm_missing=None,
                should_stop=None, cli=True) -> None:
    """Entry point for a collect run. Discovers every project under `root`, plans each unit
    with nothing written yet, asks ONCE about anything still missing, then executes.
    Config-driven via [COLLECT]; the GUI calls this same function.

    Two prompts are pluggable so the same code serves the terminal and the GUI (which has
    no stdin — an input() there would hang the worker thread forever):
      on_confirm_start()          → return True to proceed. None = the terminal [Y/n]
                                    prompt. The GUI passes lambda: True (the Run button
                                    already IS the confirmation).
      on_confirm_missing(listed)  → return True to collect anyway. None = the terminal
                                    prompt. The GUI passes a native confirm dialog. The
                                    missing list is printed either way (see
                                    prompt_missing_decision).
      should_stop()               → checked BETWEEN project units; return True to halt.
                                    A unit is atomic (all copies + .als + report or
                                    nothing), so stopping here never leaves a half-written
                                    bundle. None = never stops (the CLI, which has no Stop
                                    button). Mirrors the pipeline's 'halt after current
                                    file' — collect halts after the current project.
      cli                         → True = full terminal presentation: the COLLECT SETTINGS
                                    block + ═-boxed per-project summaries + a boxed Done line.
                                    False = GUI presentation: no settings block (they live in
                                    the left panel) and light ─ dividers instead of heavy
                                    boxes, matching the processing run's console look."""
    cfg   = load_collect_settings(config)
    units = group_collect_targets(find_collect_als(root), cfg['versions'])

    # Version guard — Live 9/10 sets use a different <FileRef> schema this tool can't read.
    # Refuse them up front (printed with the Found line below) instead of misreading every
    # sample as "missing". The skipped list is surfaced later so it sits with the run summary.
    units, skipped_old = split_supported_units(units)

    if not units:
        if skipped_old:
            n = len(skipped_old)
            print(f"\n  ⚠  All {n} .als file(s) here were saved by Ableton Live {_fmt_versions(skipped_old)} "
                  f"— this tool supports Live {MIN_SUPPORTED_LIVE} & 12 only. Nothing to collect:")
            for f, v in skipped_old:
                print(f"        {show_path(f, root)}  (Live {v})")
            print(f"      Open & re-save {'them' if n != 1 else 'it'} in Live {MIN_SUPPORTED_LIVE}+ "
                  f"first, then re-run.")
            print(f"      Note: a whole backlog? Batch it — see the README (\"Older projects\").\n")
        else:
            print(f"\nNo .als files to collect under: {root}\n")
        return

    n_loose = sum(1 for _r, real, _f in units if not real)
    n_real  = len(units) - n_loose

    if cli:
        # ── CLI: full settings block, in the processor's print_pipeline_info shape ──────
        SW = 15
        print("\n************      COLLECT SETTINGS      ************\n")
        print(f"  {'Project folder':<{SW}}: {root.resolve()}")
        if n_real:   # meaningless when there is nothing but loose .als files
            print(f"  {'Versions':<{SW}}: "
                  f"{'all' if cfg['versions'] == 'all' else cfg['versions'] + ' most recent'} per project folder")
            wl = cfg['workspace_levels']
            print(f"  {'Workspace':<{SW}}: "
                  + ('Off — all external samples collected' if wl == 0
                     else f"{wl} folder level(s) up left in place, not collected"))
        print(f"  {'Ableton Packs':<{SW}}: {'NOT Included' if cfg['skip_packs'] else 'Included'}")
        print(f"  {'M4L devices':<{SW}}: {'NOT Included' if cfg['skip_m4l'] else 'Included'}")
        print(f"  {'Report':<{SW}}: {'Enabled' if cfg['plugin_report'] else 'Disabled'}")
        for i, br in enumerate(cfg['backup_roots']):
            print(f"  {('Backup search' if i == 0 else ''):<{SW}}: {br}")
        print()
    else:
        # ── GUI: the settings are already visible in the left panel, so skip the block and
        # just print the bare folder line. No leading newline — it must sit flush at the top
        # like the processing run's first line (a '\n' here renders an empty row above it).
        # The console styles 'Project folder:' as a heading with its own underline (as it does
        # in the processing run), so NO explicit divider is added — one would double up. ──────
        print(f"Project folder: {root.resolve()}")

    # A target folder can hold loose .als and project folders at once, so the run has no
    # single mode — report a count of each kind present.
    mix = [f"{n_loose} loose .als"] if n_loose else []
    if n_real:
        mix.append(f"{n_real} project folder{'s' if n_real != 1 else ''}")
    print(f"Found {len(units)} project(s) to collect — {', '.join(mix)}")

    # Some sets were saved by Live 9/10 and pulled out above — name them so the count
    # isn't silently short (and it's clear WHY they aren't in the run).
    if skipped_old:
        n = len(skipped_old)
        print(f"\n  ⚠  Skipped {n} .als file(s) saved by Ableton Live {_fmt_versions(skipped_old)} "
              f"— this tool supports Live {MIN_SUPPORTED_LIVE} & 12 only:")
        for f, v in skipped_old:
            print(f"        {show_path(f, root)}  (Live {v})")
        print(f"      Open & re-save {'them' if n != 1 else 'it'} in Live {MIN_SUPPORTED_LIVE}+ "
              f"first to include {'them' if n != 1 else 'it'}.")
        print(f"      Note: a whole backlog? Batch it — see the README (\"Older projects\").")

    # A configured backup folder that doesn't exist (a typo, or an external drive that's
    # unplugged) is silently skipped by the search — flag each one, or the missing-files prompt
    # at the end reads as if no backup was ever set, when really it just couldn't be reached.
    for br in cfg['backup_roots']:
        if not br.is_dir():
            print(f"\n  ⚠  Backup search location not found — skipping it: {br}")
            print(f"      Check the path is correct and any external drive is connected, then re-run.")

    if on_confirm_start is not None:
        if not on_confirm_start():
            return
    else:
        print()
        print(f"  Collect all {len(units)} project(s)?  [Y/n]")
        if input("  > ").strip().lower() in ('n', 'no'):
            print("\n  Cancelled — nothing collected.\n")
            return

    # PLAN every unit first — nothing touches the disk yet, so the whole run can be pooled
    # into ONE missing-file decision before anything is committed.
    plans = []
    for proot, is_real, files in units:
        if should_stop is not None and should_stop():
            print("\n⏹  Stop requested — halting before anything was written.")
            return
        out_dir = proot if is_real else proot / f"{files[0].stem}_collected"
        plans.append(plan_collect(files, proot, is_real, out_dir,
                                   cfg['skip_packs'], cfg['skip_m4l'], root,
                                   cfg['backup_roots'], should_stop=should_stop, cli=cli,
                                   workspace_levels=cfg['workspace_levels']))
    # A Stop pressed mid-scan of the LAST unit's backup search won't hit the loop-top check above,
    # so catch it here too — before the missing-files prompt and any copying.
    if should_stop is not None and should_stop():
        print("\n⏹  Stop requested — halting before anything was written.")
        return

    # ONE missing-file decision for the entire run. A sample missing from five sets is one
    # problem, so it is listed once and asked about once — never once per unit.
    pooled = {}
    for p in plans:
        for name, size, loc in p['missing']:
            pooled.setdefault((name.lower(), size), (name, size, loc))
    if not prompt_missing_decision(list(pooled.values()), on_confirm_missing, cli=cli):
        print(f"\n  Cancelled — nothing copied, no .als written.\n")
        return

    # Execute unit by unit, checking for a stop request between each (a unit is atomic,
    # so a mid-run stop can't leave a bundle half-written). Already-copied files are
    # resume-safe, so a stopped-then-re-run collect just picks up where it left off.
    done, stopped = [], False
    grand_bytes = sum(p['to_copy_bytes'] for p in plans)
    progress = CopyProgress(grand_bytes, cli=cli) if grand_bytes else None
    for plan in plans:
        if should_stop is not None and should_stop():
            print("\n⏹  Stop requested — halting before the next project "
                  "(already-collected projects are complete).")
            stopped = True
            break
        if (d := execute_collect(plan, cfg['plugin_report'], cli=cli, progress=progress)):
            done.append(d)
    if progress:
        progress.close()
    if not done:
        print()
        return

    # Path and its stats on separate lines: a full bundle path + the "(n file(s), size)"
    # tail overflows the (narrower) GUI console and wraps mid-row, so the stats hang on
    # their own indented line under the path instead of trailing it.
    # When the CLI bar drew, it already left a blank line above and an erased line right here —
    # let 'Saved' reuse that erased line (skip this blank) so the spacing is one line either way.
    if not (cli and progress and progress.shown):
        print()
    for d in done:
        print(f"  Saved → {show_path(d['out_dir'], root)}")
        print(f"          ({d['files']} file(s), {human_bytes(d['bytes'])}"
              f"{', + report' if d['report'] else ''})")

    # Both close with a single thin rule above the summary line — no heavy box. The CLI uses
    # the same '-' rule as each per-project title; the GUI uses its own '─' width.
    done_line = (f"{'Stopped' if stopped else 'Done'} — {len(done)} project(s), "
                 f"{sum(d['files'] for d in done)} file(s) copied "
                 f"({human_bytes(sum(d['bytes'] for d in done))})")
    if cli:
        print(f"\n{PROJECT_RULE}")
        print(done_line)
        print()
    else:
        print(f"\n{'─' * DIVIDER_W}")
        print(done_line)
        print()
