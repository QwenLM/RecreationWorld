"""Verbatim-scrape detection gate for RecreationBench Web.

The benchmark task is to *rebuild* a website with React. A common, benchmark-ruling
violation is to instead **scrape the original site verbatim** — fetch the rendered
HTML/CSS and re-emit it (e.g. via ``dangerouslySetInnerHTML``) inside an empty React
shell. The visual/structural metrics cannot tell "rebuild" from "scrape" (a verbatim
copy maximises pixel/DOM similarity by construction), so a dedicated gate is needed.

Design — three signals, computed only from the agent's *raw bytes* (the built
``output/index.html`` plus the workspace ``src/**`` source files). We never read the
normalised ``agent_dom`` snapshot, because the structural scorer strips exactly the
build fingerprints this gate relies on (``structural_score.py`` ``normalize_dom``).

  A  size / provenance : a single huge ``output/index.html`` (>2 MB), or copied bytes
                         dwarfing authored bytes (ratio >= 3, or >= 300 KB copied).
  B  build fingerprints : auto-generated markers that a from-scratch author *cannot*
                         reproduce by hand (Vue ``data-v-<hash>``, Emotion ``css-<hash>``,
                         styled ``sc-<MixedHash>``, Webflow ``data-w-id``, Nuxt/Gatsby/
                         Next/Astro/Svelte/Angular build attrs). Total occurrences >= 20.
  C  opaque injection  : ``dangerouslySetInnerHTML`` / ``innerHTML =`` / raw-HTML import
                         in ``src`` — the mechanism for re-emitting fetched markup.
  D  serialized DOM    : the target's rendered DOM serialised into a JSON/TS node tree
                         (``{tag, cls, kids/children}``) and re-emitted through a generic
                         ``createElement``/``switch(node.tag)`` renderer — a laundered scrape
                         that leaves no fingerprints (B), no literal ``<div`` (A), and no
                         ``innerHTML`` (C). Guarded by a provenance signal (``#text`` node
                         markers OR preserved original BEM/CMS class names) so it stays
                         zero-false-positive against honest data-driven rebuilds.
  E  raw-HTML injection: the atom.io evasion — the target's rendered HTML stored VERBATIM as
                         raw strings (in ``src/data/*.json``/``.ts`` or ``.html`` files) and
                         re-emitted through ``dangerouslySetInnerHTML``. On a fingerprint-less
                         STATIC target this leaves NO build hashes (B misses), and because the
                         copy is raw HTML *strings* (not a ``{tag,cls,kids}`` node tree) signal
                         D misses it too. The provenance tell is the HTML ``class="`` attribute
                         (raw or JSON-escaped ``class=\\"``): authored React JSX uses
                         ``className=``, so a high count of literal ``class="`` proves the
                         injected content is copied markup, not hand-authored components. E fires
                         only on the conjunction injection (C) + many literal ``class="`` +
                         real copied volume — zero-false-positive against honest injection
                         (inline SVG / small rich-text snippets carry ~0 ``class="``).

Verdict = SCRAPE  iff  (B and (A or C))  OR  D  OR  E  OR  F.

**F (source-level class-name provenance) is the load-bearing, mechanism-agnostic signal.**
Signals A-E each detect a specific laundering *mechanism* and are individually evadable (new
mechanisms: abbreviated node-tree keys, DOMParser vs innerHTML, class-stripping at render). F
instead asks whether the agent's OWN SOURCE reproduces the REFERENCE site's class names (bespoke
or compiler-hashed) — which an honest rebuild never does (it authors its own), regardless of how
the copy is laundered downstream. See the F-block below for details / calibration.

**B is a necessary condition** for the A/C injection path and is its false-positive guard: only and is the false-positive guard: only build fingerprints
distinguish "scraped" from "honestly rebuilt very faithfully". Reused images/fonts and
high visual/DOM similarity are *allowed* and deliberately are NOT signals here. Validated
on 101 honest opus rebuilds: max fingerprint count was 2 (threshold is 20) → 0 false
positives. ``dangerouslySetInnerHTML`` alone appears in ~32% of honest builds (legit:
inline SVG, rich text), which is exactly why C alone never triggers.

Asset URLs that merely *contain* a framework path (e.g. a reused ``/_next/static/...``
image) and CDN hosts are intentionally excluded from the fingerprint set — reusing
binary assets is permitted, so those would be false positives.
"""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Cap applied to a confirmed-scrape submission (shared with the other integrity gates).
SCRAPE_CAP = 0.10

# Thresholds (B is the load-bearing, zero-false-positive guard).
FINGERPRINT_MIN = 20          # total fingerprint occurrences for signal B
OUTPUT_HTML_BIG_BYTES = 2_000_000
COPIED_RATIO_MIN = 3.0
COPIED_ABS_MIN = 300_000
COPIED_RATIO_FLOOR_BYTES = 50_000   # don't trip ratio on trivially small copied chunks

_SRC_EXTS = {".ts", ".tsx", ".js", ".jsx", ".html", ".json", ".css", ".vue", ".svelte"}
_MAX_FILE_BYTES = 24_000_000        # cap per-file read (huge scrapes can be 30 MB+)

# --- build fingerprints: auto-generated markers honest hand-authoring can't reproduce.
# Emotion/styled tightened to require a hash-like suffix (digit / mixed-case) so honest
# kebab class names like ``css-grid`` / ``sc-header`` do NOT match.
_FINGERPRINT_PATTERNS = [
    r'data-v-[0-9a-f]{6,}',                       # Vue scoped-style attribute
    r'data-reactroot', r'data-react-helmet', r'data-react-checksum',
    r'data-w-id', r'data-wf-page', r'data-wf-site',   # Webflow build attrs
    r'nuxt-link', r'\b__nuxt\b', r'data-n-head', r'data-nuxt-',
    r'\b__gatsby\b', r'gatsby-focus-wrapper', r'gatsby-image-wrapper',
    r'id=["\']__next["\']', r'data-nextjs-',          # Next.js (not /_next/static — asset paths)
    r'data-astro-', r'astro-island',
    r'css-(?=[a-z0-9]*[0-9])[a-z0-9]{5,}',            # Emotion (hash contains a digit)
    r'\bsc-(?=[A-Za-z]*[a-z])(?=[A-Za-z]*[A-Z])[A-Za-z]{5,}',  # styled-components (mixed-case hash)
    r'data-styled\b', r'data-emotion\b',
    r'svelte-(?=[a-z0-9]*[0-9])[a-z0-9]{6,}', r'data-svelte-h',   # Svelte scoped
    r'_ngcontent-', r'_nghost-', r'ng-version',       # Angular
]
FINGERPRINT_RE = re.compile("|".join(_FINGERPRINT_PATTERNS))

# Markers that a .ts/.tsx file is dominated by injected/copied raw markup rather than
# authored React. Precise to avoid false triggers: `.innerHTML\s*=(?!=)` excludes the
# `==`/`===` comparisons; the raw-markup import requires an actual markup file path with a
# `?raw` suffix (Vite raw import) rather than a bare `? raw` ternary.
_INJECTION_RE = re.compile(
    r'dangerouslySetInnerHTML'
    r'|\.innerHTML\s*=(?!=)'
    r'|insertAdjacentHTML'
    r'|\.(?:html|htm|svg|xml)\?raw\b'
    r'|from\s+["\'][^"\']+\.html["\']'
)
_HTMLSTR_RE = re.compile(r'html:\s*[`"\']')

# --- Signal D: laundered scrape via a serialized-DOM node tree ---------------------------
# The basecamp/carbonbrief evasion: instead of injecting raw HTML (caught by C) or leaving
# build fingerprints (caught by B), the agent serialises the target's *rendered DOM* into a
# JSON/TS node tree (``{tag, cls, kids/children, ...}``) and re-emits it through a generic
# recursive ``React.createElement``/``switch(node.tag)`` renderer. This produces NO literal
# ``<div`` (copied_bytes stays ~0), NO ``dangerouslySetInnerHTML`` (uses_injection=false),
# and NO build hashes (fingerprints < 20) — so signals A/B/C all miss it. Functionally it is
# still a verbatim DOM copy.
#
# D fires only on the conjunction of: a large serialised node tree (many HTML-tag-valued
# ``"tag"`` fields + child-array nesting) + a generic tag-dispatching renderer + real volume
# + a PROVENANCE guard that an honest rebuild cannot satisfy: either ``#text`` node markers
# (browsers emit these; nobody hand-authors them) OR a high density of the ORIGINAL site's
# BEM/CMS class names preserved verbatim (an honest rebuild uses its own class names / Tailwind,
# so it produces ~0 of these). The provenance guard is what makes D zero-false-positive: a
# data-driven rebuild that authors its own Tailwind classes (e.g. a section/block model) has
# no ``#text`` and no original BEM/CMS classes, so D does NOT fire on it.
SERIALIZED_TAG_NODES_MIN = 300    # HTML-tag-valued "tag" fields in data files
SERIALIZED_NEST_MIN = 20          # child-array nesting keys (kids/children/...)
SERIALIZED_DOM_BYTES_MIN = 100_000
HASHTEXT_MIN = 15                 # "#text" node markers (strong DOM-serialisation tell)
ORIG_CLASS_MIN = 30               # preserved original BEM/CMS class occurrences

# a "tag" field whose value is an HTML element name or the DOM text-node marker "#text"
_DOM_TAG_NODE_RE = re.compile(r'"tag"\s*:\s*"(#text|[a-z][a-z0-9]{0,11})"')
_HASHTEXT_RE = re.compile(r'"tag"\s*:\s*"#text"')
# child-array nesting keys used to serialise a DOM tree (kept precise to avoid matching
# generic content arrays like "items"/"content" that legit content-models use)
_DOM_NEST_RE = re.compile(r'"(?:kids|children|childNodes|nodes)"\s*:\s*\[')
# a generic renderer that DISPATCHES on a data node's tag/type field (honest component code
# writes JSX directly; it does not switch on a ``.tag`` string field pulled from data).
# NB: the quantifiers before ``.tag`` are BOUNDED ({0,40}) and the ``node.tag === "..."`` form
# is anchored on the ``.tag`` property (no unbounded ``[\w.]+`` prefix) — an unbounded greedy
# prefix caused catastrophic O(n^2) backtracking on large all-word-char blobs (e.g. a 19 MB
# base64 assets.ts). A cheap substring pre-check at the call site skips files with no anchor.
_RENDER_BY_TAG_RE = re.compile(
    r'createElement\(\s*[\w.]{0,40}\.?(?:tag|type)\b'     # createElement(node.tag, ...) / createElement(tag, ...)
    r'|switch\s*\(\s*[\w.]{1,40}\.(?:tag|type|nodeName)\s*\)'  # switch (node.tag) { ... }
    r'|\.tag\s*===\s*["\']'                              # node.tag === "..."
)
# a class value that is an ORIGINAL-site class an honest rebuild would not reproduce:
# real BEM (word chars on BOTH sides of ``__``/``--`` — excludes Tailwind arbitrary
# values like ``w-[--var]``) or a known CMS/grid token.
_ORIG_CLASS_VAL_RE = re.compile(
    r'[A-Za-z0-9]__[A-Za-z0-9]'                       # BEM element:  block__el
    r'|[A-Za-z0-9]--[A-Za-z0-9]'                      # BEM modifier: block--mod
    r'|\bwp-(?:image|block|caption)\b|\belementor\b|\bet_pb_|\bnode--|\bmenu-item\b'  # CMS
    r'|\bcol-(?:xl|lg|md|sm)-\d'                      # Bootstrap grid
)
# extract class/cls/className string values so we only test D-provenance on class attributes
_CLASS_VAL_RE = re.compile(r'"(?:class|cls|className)"\s*:\s*"([^"]*)"')

# --- Signal E: raw-HTML injection scrape (the atom.io evasion) ---------------------------
# The target's rendered HTML stored VERBATIM as raw strings (in data JSON/TS or .html) and
# re-emitted via ``dangerouslySetInnerHTML``. On a fingerprint-less static target this leaves
# no build hashes (B misses) and, being raw HTML *strings* rather than a ``{tag,cls,kids}``
# node tree, signal D misses it too. Provenance tell: the HTML ``class="`` attribute (raw or
# JSON-escaped ``class=\"``). Authored React uses ``className=``, so literal ``class="`` in the
# source can only be copied markup. Measured across all scanned source files; E is gated on
# ``uses_injection`` (C) so honest static class strings that are never injected cannot trip it.
RAW_MARKUP_INJECT_MIN = 50    # literal HTML class="/class=\" attributes (with injection)
# HTML class attribute, raw (`class="`) or JSON-escaped (`class=\"`). The optional backslash
# matches the escaped form inside .json/.ts string literals. Word-boundary + the ``=`` right
# after ``class`` means JSX ``className=`` never matches (there ``class`` is followed by ``Name``).
_RAW_HTML_CLASS_RE = re.compile(r'\bclass=\\?"')

# --- Signal F: source-level class-name PROVENANCE (mechanism-agnostic backstop) -----------
# The load-bearing anti-scrape signal. Signals A-E each target a specific *mechanism* (raw-HTML
# injection, {tag,cls,kids} node trees, build fingerprints); agents keep finding new laundering
# mechanisms that evade all of them: abbreviated node-tree keys ({t,c} instead of {tag,children}),
# DOMParser instead of dangerouslySetInnerHTML, STRIPPING the copied classes at render time so the
# rendered-DOM originality cap sees only the agent's own Tailwind (containment_classed ~= 0), and
# reproducing bespoke / react-jss classes that are not in the fingerprint list. F sidesteps the
# mechanism entirely and asks a PROVENANCE question about the agent's OWN SOURCE: does src/**
# (including harvested src/data/*.json blobs) contain the REFERENCE site's class names?
#
# The task legitimately requires reproducing the reference's TEXT CONTENT (100% is the goal), but
# the agent must AUTHOR ITS OWN class names. An honest rebuild's classes are its own (Tailwind /
# semantic), so it shares ~0 of the original's DISTINCTIVE (bespoke / build-hash) classes even when
# it converges on the same content and layout. A scrape carries the original's classes verbatim
# into its source (even if it strips or remaps them at render time), so a high overlap with the GT
# class vocabulary is a mechanism-independent proof of copying. Calibrated on 100 honest opus
# rebuilds (0 false positives; worst honest overlap 89 distinctive, 0 opaque) vs known glm scrapes
# (100-500 distinctive, or verbatim compiler hashes).
#
#   F1  opaque-fingerprint match : >= F_OPAQUE_MIN of GT's *opaque* classes (compiler hashes:
#         react-jss ``Foo__Kah4H``, emotion ``css-1ab2``, styled ``sc-Xy``, svelte scoped, CSS-module
#         ``_name_hash_1``, MS-Word-Online ``SCXW1234``/``BCX0``) appear verbatim in agent src. These
#         are RANDOM / session-specific and cannot be independently reinvented -> near-zero FP.
#   F2  bulk distinctive match  : >= F_DIST_COUNT_MIN of GT's distinctive (non-utility) classes
#         appear verbatim in agent src. Honest semantic convergence tops out well below this
#         (measured opus max 89); a scrape reproduces hundreds.
#   F3  high-fraction copy      : the agent reproduces >= F_FRAC_MIN of GT's distinctive vocab (with
#         >= F_FRAC_COUNT_MIN absolute) -> catches small-vocab sites copied near-whole (rust-lang
#         0.98, umich 0.95, diez 0.90) where the absolute count is below F2.
#
# F fires iff F1 or F2 or F3. Utility / framework classes (Tailwind, Bootstrap grid, ubiquitous
# semantic words) are EXCLUDED from the distinctive set so an honest Tailwind rebuild of a Tailwind
# original cannot false-positive. F is gt_dom-driven; if GT DOM is unavailable it is simply inert.
F_OPAQUE_MIN = 3
F_DIST_COUNT_MIN = 100          # > the worst HONEST distinctive overlap measured (opus dolly.com 89)
F_FRAC_MIN = 0.50              # binding F3 guard: honest max fraction measured was 0.38 (0.12 margin)
F_FRAC_COUNT_MIN = 50          # small-vocab near-whole copy floor (raised 40->50 for extra margin)
F_MIN_GT_DISTINCT = 30          # too-small GT distinctive vocab -> F2/F3 inactive (coincidence-prone)

# opaque = compiler / library-generated class; never a product of honest semantic naming.
# Split into (a) unambiguous library-specific shapes and (b) react-jss / CSS-module hashes whose
# 5-char tail must pass an ENTROPY check so deterministic numeric BEM (``header__logo2``,
# ``main_panel_2``, ``card__row12``) is NOT misread as a random hash.
_OPAQUE_SPECIFIC_RES = [
    re.compile(r'^css-[a-z0-9]*[0-9][a-z0-9]*$'),             # emotion css-1ab2c3
    re.compile(r'^sc-[A-Za-z]*[a-z][A-Za-z]*[A-Z][A-Za-z]{2,}$'),  # styled-components mixed-case
    re.compile(r'^svelte-[a-z0-9]*[0-9][a-z0-9]{4,}$'),       # svelte scoped
    re.compile(r'^(?:SCXW\d{4,}|BCX\d+)$'),                   # MS Word Online canvas ids
]
_REACTJSS_TAIL_RE = re.compile(r'__([A-Za-z0-9]{5})$')       # react-jss / CSS-module Foo__Kah4H
_VITE_MOD_TAIL_RE = re.compile(r'_([a-z0-9]{5})_\d+$')       # vite CSS-module _stroke-type_k4w8l_1


def _is_hashy(tail: str) -> bool:
    """True if a 5-char class-name tail looks like a RANDOM build hash rather than human-authored
    BEM/numeric. Excludes ``logo2``/``item1``/``row12``/``panel`` (a lowercase word +/- <=2 trailing
    digits); accepts ``Kah4H``/``JGaoV``/``k4w8l`` (mixed case, or >=2 digits)."""
    if re.fullmatch(r'[a-z]+[0-9]{0,2}', tail):     # lowercase word (+ up to 2 trailing digits) -> BEM
        return False
    has_up = any(c.isupper() for c in tail)
    has_lo = any(c.islower() for c in tail)
    n_dig = sum(c.isdigit() for c in tail)
    return (has_up and has_lo) or n_dig >= 2


def _is_opaque_class(c: str) -> bool:
    if any(r.search(c) for r in _OPAQUE_SPECIFIC_RES):
        return True
    m = _REACTJSS_TAIL_RE.search(c)
    if m and _is_hashy(m.group(1)):
        return True
    m = _VITE_MOD_TAIL_RE.search(c)
    if m and _is_hashy(m.group(1)):
        return True
    return False


# utility / framework class shapes an honest rebuild may legitimately share with the original.
# NOTE (known bounded limitation): the ambiguous English-word prefixes (text-/content-/list-/
# grid-/border-/font-/space-/inset-/…) match on prefix+`-` without a Tailwind-value check, so a
# bespoke class that happens to start with one (e.g. `text-hero`, `content-main`) is also excluded
# from the distinctive set. This is deliberately left broad to preserve the calibrated 0-FP on
# Tailwind rebuilds (a value-grammar check risks re-introducing false positives). Residual FN
# vector: a site whose bespoke scheme is DOMINATED by these prefixes AND has no opaque classes;
# signal F1 (opaque) and the other integrity gates provide independent backup there.
_UTILITY_CLASS_RE = re.compile(
    r'^-?(?:sm|md|lg|xl|2xl|hover|focus|active|group|peer|dark|first|last|odd|even|motion|print):'
    r'|^-?(?:bg|text|border|ring|divide|from|via|to|fill|stroke|shadow|opacity|rounded|'
    r'p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|ml|mr|w|h|min|max|size|basis|'
    r'flex|grid|gap|space|justify|items|content|self|place|order|col|row|'
    r'top|bottom|left|right|inset|z|float|clear|object|overflow|overscroll|'
    r'font|leading|tracking|indent|align|whitespace|break|list|'
    r'block|inline|table|hidden|flow|contents|absolute|relative|fixed|static|sticky|'
    r'transition|duration|delay|ease|animate|transform|scale|rotate|translate|skew|origin|'
    r'cursor|select|resize|appearance|outline|container|columns|aspect|'
    r'uppercase|lowercase|capitalize|truncate|antialiased|italic|underline|'
    r'sr|not-sr|pointer-events|will-change|snap|touch|'
    r'backdrop|blur|brightness|contrast|grayscale|invert|saturate|sepia)'
    r'(?:-|$|\[)'
)
_BOOTSTRAP_GRID_RE = re.compile(r'^col-(?:xs|sm|md|lg|xl)-\d+$')
_COMMON_CLASSES = frozenset("""
header footer nav navbar main aside section article container wrapper content inner outer
row col column grid flex card item items list link links button btn icon logo image img title
subtitle heading label badge tag chip pill menu submenu dropdown modal dialog popup tooltip
active current open closed show hide hidden visible disabled selected checked expanded collapsed
primary secondary tertiary success warning danger error info light dark muted default small
medium large huge tiny mini big left right center top bottom middle start end justified page
body site brand hero banner slider carousel gallery box panel block group form input field
textarea select option checkbox radio search submit reset table thead tbody tfoot cell head
foot overlay backdrop mask spinner loader loading skeleton placeholder text value name meta
info detail description summary caption note tip sidebar widget module first last odd even
clearfix clear code line prose close message null screenshot arrow base
""".split())
# class-name token shape (also used to pull class-like tokens out of agent source)
_CLASS_TOKEN_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_-]{3,}')


def _is_distinctive_class(c: str) -> bool:
    """A class an honest rebuild would NOT independently reinvent: not a utility / grid class,
    not an ultra-common semantic word, and long enough to be non-coincidental."""
    if len(c) < 4 or c in _COMMON_CLASSES:
        return False
    if _UTILITY_CLASS_RE.match(c) or _BOOTSTRAP_GRID_RE.match(c):
        return False
    if re.fullmatch(r'[a-z]{1,3}', c) or re.fullmatch(r'\d[\w-]*', c):
        return False
    return True


def _load_gt_class_vocab(gt_dom_dir, target_pages):
    """Collect the reference site's class-name vocabulary from GT DOM snapshots
    (``gt_dom/<page>/dom_snapshot.json``). Returns (distinctive_set, opaque_set, total)."""
    import json as _json
    gt_dom_dir = Path(gt_dom_dir) if gt_dom_dir else None
    if not gt_dom_dir or not gt_dom_dir.is_dir():
        return set(), set(), 0
    vocab = set()
    pages = list(target_pages) if target_pages else [
        p.name for p in gt_dom_dir.iterdir() if p.is_dir()]
    for pid in pages:
        if isinstance(pid, dict):          # tolerate page-info dicts as well as bare ids
            pid = pid.get("id")
        if not isinstance(pid, str):
            continue
        snap = gt_dom_dir / pid / "dom_snapshot.json"
        if not snap.is_file():
            continue
        try:
            body = _json.loads(snap.read_text(encoding="utf-8", errors="ignore")).get("body")
        except Exception:
            continue
        stack = [body]
        while stack:
            n = stack.pop()
            if not isinstance(n, dict):
                continue
            for c in (n.get("classes") or []):
                if isinstance(c, str):
                    vocab.add(c)
            ch = n.get("children")
            if isinstance(ch, list):
                stack.extend(ch)
    distinctive = {c for c in vocab if _is_distinctive_class(c)}
    opaque = {c for c in vocab if _is_opaque_class(c)}
    return distinctive, opaque, len(vocab)


def _read(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(_MAX_FILE_BYTES)
    except Exception:
        return ""


def _byte_size(path: Path, text: str) -> int:
    """True on-disk byte size (size thresholds are byte-denominated). Falls back to the
    decoded-text length only if stat() fails."""
    try:
        return path.stat().st_size
    except OSError:
        return len(text)


def _iter_src_files(src_dir: Path):
    if not src_dir or not src_dir.is_dir():
        return
    for root, dirs, files in os.walk(src_dir):
        # never descend into dependency / build dirs
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "dist")]
        for name in files:
            if os.path.splitext(name)[1].lower() in _SRC_EXTS:
                yield Path(root) / name


def compute_scrape_gate(agent_html_path, workspace_src_dir=None, output_dir=None,
                        gt_dom_dir=None, target_pages=None) -> dict:
    """Detect verbatim-scrape submissions from raw agent bytes.

    Args:
        agent_html_path: the served built HTML (``output/index.html``).
        workspace_src_dir: the agent's ``src/`` directory (authored source). Optional —
            when absent (e.g. re-evaluating an output-only export) the gate falls back to
            the built HTML alone.
        output_dir: the served output directory (scanned for any additional ``*.html``).
        gt_dom_dir: the reference site's GT DOM snapshots dir (``.../evaluation/gt_dom``),
            used by signal F (source-level class-name provenance). Optional — F is inert
            without it.
        target_pages: list of page ids to source GT classes from (defaults to all pages
            present under ``gt_dom_dir``).

    Returns a dict with the signals, the verdict (``SCRAPE``/``CLEAN``), and the raw
    measurements, for auditing.
    """
    agent_html_path = Path(agent_html_path) if agent_html_path else None
    output_dir = Path(output_dir) if output_dir else (
        agent_html_path.parent if agent_html_path else None)
    workspace_src_dir = Path(workspace_src_dir) if workspace_src_dir else None

    fp_counts = {}          # distinct fingerprint string -> occurrences
    fp_total = 0
    output_html_bytes = 0
    largest_other_html = 0
    copied_bytes = 0
    authored_bytes = 0
    uses_injection = False
    raw_html_markup = 0     # literal HTML class="/class=\" attributes (signal E provenance)
    # Signal D (serialized-DOM node tree) accumulators
    dom_tag_nodes = 0
    dom_hashtext = 0
    dom_nest = 0
    dom_orig_class = 0
    dom_bytes = 0
    uses_generic_tag_renderer = False
    agent_class_tokens = set()   # signal F: class-like src tokens that match the GT class vocab
    # Load the reference class vocabulary up front so signal-F token harvesting can intersect on
    # the fly (finditer + membership) instead of materialising every token of a 19 MB src/data
    # blob — same cheap-scan discipline as signals D/E on this loop.
    gt_distinctive, gt_opaque, gt_class_total = _load_gt_class_vocab(gt_dom_dir, target_pages)
    _gt_class_all = gt_distinctive | gt_opaque

    def _tally_fingerprints(text):
        nonlocal fp_total
        for m in FINGERPRINT_RE.findall(text):
            fp_total += 1
            fp_counts[m] = fp_counts.get(m, 0) + 1

    # --- built HTML (always available): feeds fingerprint counting (B) and the >2 MB
    # size signal (A). NOT added to copied_bytes: the bundle is mostly the agent's own
    # compiled+minified React, so counting it as "copied" would inflate every honest
    # build (a normal bundle is ~300 KB). Inlined scraped markup instead shows up via the
    # huge total size (bill 16 MB / clay 34 MB) and via the src files below.
    html_files = []
    if agent_html_path and agent_html_path.is_file():
        html_files.append(agent_html_path)
    if output_dir and output_dir.is_dir():
        for f in output_dir.glob("*.html"):
            if f not in html_files:
                html_files.append(f)
    for f in html_files:
        t = _read(f)
        _tally_fingerprints(t)
        sz = _byte_size(f, t)
        if f.name == "index.html":
            output_html_bytes = max(output_html_bytes, sz)
        elif output_html_bytes == 0:
            # no file named index.html — track the largest html as a fallback
            largest_other_html = max(largest_other_html, sz)
    if output_html_bytes == 0:
        output_html_bytes = largest_other_html

    # --- workspace source: fingerprints + authored-vs-copied split + injection.
    for f in _iter_src_files(workspace_src_dir):
        ext = f.suffix.lower()
        t = _read(f)
        if not t:
            continue
        _tally_fingerprints(t)
        # Signal F: harvest class-like tokens that MATCH the reference class vocabulary (incl.
        # tokens laundered into src/data/*.json or stripped at render). finditer + membership keeps
        # the working set bounded by |GT vocab| and avoids the full-list spike on base64 blobs.
        if _gt_class_all:
            for m in _CLASS_TOKEN_RE.finditer(t):
                tok = m.group(0)
                if tok in _gt_class_all:
                    agent_class_tokens.add(tok)
        sz = _byte_size(f, t)
        # Signal E provenance: literal HTML class attributes (raw or JSON-escaped). Authored
        # React JSX uses className=, so any class="/class=\" here is copied raw markup. The
        # cheap ``in`` pre-check skips the regex walk on files that can't match (e.g. base64).
        if "class=" in t:
            raw_html_markup += len(_RAW_HTML_CLASS_RE.findall(t))

        # --- Signal D: serialized-DOM node tree + generic renderer + provenance ------
        if ext in (".json", ".ts", ".tsx", ".js", ".jsx"):
            # Cheap substring pre-checks gate every regex walk below, so a large non-matching
            # blob (e.g. a 19 MB base64 asset bundle) is skipped in O(n) `in` time rather than
            # scanned by each regex. Behaviour is identical: a regex over a string with no
            # possible match returns nothing.
            n_tag = len(_DOM_TAG_NODE_RE.findall(t)) if '"tag"' in t else 0
            n_nest = len(_DOM_NEST_RE.findall(t)) if ('"kids"' in t or '"children"' in t
                                                      or '"childNodes"' in t or '"nodes"' in t) else 0
            dom_tag_nodes += n_tag
            dom_nest += n_nest
            if '"tag"' in t:
                dom_hashtext += len(_HASHTEXT_RE.findall(t))
            if '"class"' in t or '"cls"' in t or '"className"' in t:
                for cv in _CLASS_VAL_RE.findall(t):
                    dom_orig_class += len(_ORIG_CLASS_VAL_RE.findall(cv))
            # count this file's bytes toward the serialized-DOM volume if it is
            # itself node-tree-shaped (many tag nodes + real nesting)
            if n_tag >= 50 and n_nest >= 5:
                dom_bytes += sz
            if (ext in (".ts", ".tsx", ".js", ".jsx")
                    and ("createElement" in t or "switch" in t or ".tag" in t)
                    and _RENDER_BY_TAG_RE.search(t)):
                uses_generic_tag_renderer = True

        if ext in (".html", ".vue", ".svelte"):
            copied_bytes += sz
            if _INJECTION_RE.search(t):
                uses_injection = True
            continue
        if ext == ".json":
            if "<div" in t or "<section" in t or _INJECTION_RE.search(t) or FINGERPRINT_RE.search(t):
                copied_bytes += sz
            continue
        if ext == ".css":
            continue  # CSS bytes are neither authored-React nor an injection signal
        # .ts/.tsx/.js/.jsx — decide authored vs copied (appendix-C heuristic)
        injected = bool(_INJECTION_RE.search(t))
        if injected:
            uses_injection = True
        ffp = len(FINGERPRINT_RE.findall(t))
        if (injected or _HTMLSTR_RE.search(t) or ffp >= 30
                or (len(t) > 80_000 and ("<div" in t or "<section" in t))):
            copied_bytes += sz
        else:
            authored_bytes += sz

    ratio = (copied_bytes / authored_bytes) if authored_bytes else (
        float("inf") if copied_bytes else 0.0)

    signal_A = bool(
        output_html_bytes > OUTPUT_HTML_BIG_BYTES
        or copied_bytes >= COPIED_ABS_MIN
        or (copied_bytes >= COPIED_RATIO_FLOOR_BYTES and ratio >= COPIED_RATIO_MIN)
    )
    signal_B = fp_total >= FINGERPRINT_MIN
    signal_C = uses_injection

    # Signal D — laundered serialized-DOM scrape. Independent of B (a laundered scrape has
    # NO build fingerprints), so it is its own sufficient trigger. The provenance guard
    # (``#text`` markers OR preserved original BEM/CMS classes) keeps it zero-false-positive.
    dom_provenance = (dom_hashtext >= HASHTEXT_MIN) or (dom_orig_class >= ORIG_CLASS_MIN)
    signal_D = bool(
        dom_tag_nodes >= SERIALIZED_TAG_NODES_MIN
        and dom_nest >= SERIALIZED_NEST_MIN
        and uses_generic_tag_renderer
        and dom_provenance
        and dom_bytes >= SERIALIZED_DOM_BYTES_MIN
    )

    # Signal E — raw-HTML injection scrape (the atom.io evasion). Verbatim HTML strings
    # re-emitted via dangerouslySetInnerHTML on a fingerprint-less static target: B misses
    # (no build hashes), D misses (raw strings, not a node tree). The literal ``class="``
    # count is the provenance guard (authored JSX uses ``className=``), and it is gated on
    # injection (C) + real copied volume, so honest injection (inline SVG / small rich text,
    # which carry ~0 ``class="``) cannot trip it.
    signal_E = bool(
        uses_injection
        and raw_html_markup >= RAW_MARKUP_INJECT_MIN
        and copied_bytes >= COPIED_RATIO_FLOOR_BYTES
    )

    # Signal F — source-level class-name PROVENANCE (mechanism-agnostic). Compares the agent's
    # authored class-token vocabulary (from src/**, incl. harvested src/data/*.json) against the
    # reference site's class vocabulary (GT DOM). Honest rebuilds author their own class names ->
    # ~0 overlap with the original's DISTINCTIVE / OPAQUE classes; a scrape carries them verbatim
    # (even if stripped/remapped at render time), so a high overlap proves copying regardless of
    # the laundering mechanism. F1 (opaque compiler-hash match) is near-zero-FP on its own; F2/F3
    # are gated on a non-trivial GT distinctive vocab to avoid coincidence on tiny sites.
    # (GT vocab was loaded before the src loop; agent_class_tokens already = src-tokens ∩ GT vocab.)
    matched_distinctive = gt_distinctive & agent_class_tokens
    matched_opaque = gt_opaque & agent_class_tokens
    frac_distinctive = (len(matched_distinctive) / len(gt_distinctive)) if gt_distinctive else 0.0
    f_active = len(gt_distinctive) >= F_MIN_GT_DISTINCT
    signal_F1 = len(matched_opaque) >= F_OPAQUE_MIN
    signal_F2 = f_active and len(matched_distinctive) >= F_DIST_COUNT_MIN
    signal_F3 = (f_active and frac_distinctive >= F_FRAC_MIN
                 and len(matched_distinctive) >= F_FRAC_COUNT_MIN)
    signal_F = bool(signal_F1 or signal_F2 or signal_F3)

    verdict = "SCRAPE" if (
        (signal_B and (signal_A or signal_C)) or signal_D or signal_E or signal_F
    ) else "CLEAN"

    return {
        "verdict": verdict,
        "signal_A_size": signal_A,
        "signal_B_fingerprints": signal_B,
        "signal_C_injection": signal_C,
        "signal_D_serialized_dom": signal_D,
        "signal_E_injection_markup": signal_E,
        "signal_F_class_provenance": signal_F,
        "fingerprints_total": fp_total,
        "fingerprints_distinct": len(fp_counts),
        "fingerprints_top": dict(sorted(fp_counts.items(), key=lambda kv: -kv[1])[:8]),
        "output_html_bytes": output_html_bytes,
        "copied_bytes": copied_bytes,
        "authored_bytes": authored_bytes,
        "copied_to_authored_ratio": (round(ratio, 2) if ratio != float("inf") else None),
        "uses_injection": uses_injection,
        "raw_html_markup": raw_html_markup,   # literal HTML class= count (signal E provenance)
        # serialized-DOM (signal D) measurements, for auditing
        "serialized_dom": {
            "tag_nodes": dom_tag_nodes,
            "nest_arrays": dom_nest,
            "hashtext_nodes": dom_hashtext,
            "orig_class_hits": dom_orig_class,
            "dom_bytes": dom_bytes,
            "generic_tag_renderer": uses_generic_tag_renderer,
            "provenance": dom_provenance,
        },
        # signal F (source-level class-name provenance) measurements, for auditing
        "class_provenance": {
            "gt_classes_total": gt_class_total,
            "gt_distinctive": len(gt_distinctive),
            "gt_opaque": len(gt_opaque),
            "matched_distinctive": len(matched_distinctive),
            "matched_opaque": len(matched_opaque),
            "frac_distinctive": round(frac_distinctive, 4),
            "F1_opaque": signal_F1,
            "F2_bulk": signal_F2,
            "F3_fraction": signal_F3,
            "matched_opaque_sample": sorted(matched_opaque)[:8],
            "matched_distinctive_sample": sorted(matched_distinctive, key=lambda c: -len(c))[:8],
        },
        "cap": SCRAPE_CAP,
    }
