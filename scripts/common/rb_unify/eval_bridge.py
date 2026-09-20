"""Bridge a unified release instance into a platform eval, and normalize the result.

The unified 4-component instance (``instance.json`` + ``reference/`` + ``tests/`` +
``vlm_assertions.json``) standardizes the eval INPUTS; the runtime (launch app,
attach the a11y backend, capture screenshots) stays platform-specific. This module
is the thin, platform-agnostic seam between the two:

  * :func:`manifest_from_instance` remaps ``vlm_assertions.json`` (schema v2) into the
    ``test_manifest.json`` shape the desktop runners already consume
    (``{"vlm_assertions": [{"name", "description"}, ...]}``) — see
    ``atspi_test_runner.select_vlm_assertions``.
  * :func:`finalize_scores` folds a platform's native programmatic + VLM results into
    the ONE settled per-app metric surface ``{program_score, vlm_score, prog_vlm_avg}``.
    It does not recompute pass rates — each platform scores its own way — it only
    extracts them and maps *infra-invalid* runs to ``None`` (dropped from any field
    average) while leaving *model-failure* zeros (no binary / no launch) as a real 0.0.

That None-vs-0.0 line is the eval-zero taxonomy: a crashed judge or a dead VM is not
evidence the recreation scored zero, but a recreation that never built is.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.artifact_store import ArtifactStore

# VLM dimensions carry no score when the judge itself could not run — distinct from a
# recreation that launched and simply failed the assertions.
_INFRA_ERRORS = {
    "harness_crash",
    "eval_error",
    "vlm_judge_failed",
    "vlm_judge_errors",
    "vlm_api_exception",
    "invalid_vlm_response",
    "missing_vlm_key",
}
# A programmatic dimension with one of these "errors" is a genuine 0: the model's
# recreation could not be launched at all, so every behavioural check fails.
_MODEL_FAILURE_ERRORS = {
    "no_binary",
    "no_launch_script",
    "no_display",
    "candidate_build_failed",
    "app_launch_failed",
}


def manifest_from_instance(instance_dir: str | Path) -> dict:
    """Build the desktop ``test_manifest.json`` payload from ``vlm_assertions.json``.

    The runner keys VLM work by screenshot filename (``name``) and grades against the
    frozen ``description``; the unified schema stores those as ``screenshot`` (falling
    back to ``id``) and ``assertion``. Assertions with neither a screenshot nor an id
    are dropped — they cannot be matched to a candidate frame.
    """
    inst = Path(instance_dir)
    raw = json.loads((inst / "vlm_assertions.json").read_text())
    out: list[dict] = []
    for a in raw.get("assertions", []):
        name = a.get("screenshot") or a.get("id")
        if not name:
            continue
        out.append({"name": name, "description": a.get("assertion", "")})
    return {"instance_id": raw.get("instance_id", inst.name), "vlm_assertions": out}


def canonicalize_android_manifest(
    vlm_path: str | Path, manifest_path: str | Path
) -> dict:
    """Canonicalize Android's runnable frozen VLM subset by release-level ID.

    Android's scorer keeps programmatic and VLM cases in one ``tests`` list.  Old
    exports can therefore carry a stale VLM subset even when the sibling schema-v2
    file has already been corrected.  The manifest is also the executable inventory:
    a canonical assertion without a manifest entry has no testcase capable of taking
    its screenshot.  Preserve every non-VLM testcase, intersect the runnable VLM IDs
    with the canonical IDs, refresh their metadata, and recompute the fixed
    denominator/hash.  Canonicalization may remove a stale assertion but must never
    manufacture a new executable testcase.
    """
    import hashlib

    vlm_path = Path(vlm_path)
    manifest_path = Path(manifest_path)
    raw = json.loads(vlm_path.read_text())
    manifest = json.loads(manifest_path.read_text())

    old_vlm = manifest.get("vlm_assertions") or []
    old_by_id = {
        str(item.get("name") or "").split("::")[-1]: item
        for item in old_vlm
        if isinstance(item, dict) and item.get("name")
    }
    old_names = {
        str(item.get("name") or "")
        for item in old_vlm
        if isinstance(item, dict) and item.get("name")
    }

    canonical = []
    for item in raw.get("assertions") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        assertion_id = str(item["id"])
        previous = old_by_id.get(assertion_id)
        if previous is None:
            continue
        module = str(previous.get("module") or "")
        if not module:
            linked = str(item.get("links_to") or assertion_id)
            module = linked.split("_", 1)[0]
        name = str(previous.get("name") or f"{module}::{assertion_id}")
        canonical.append(
            {
                "name": name,
                "module": module,
                "tag": str(previous.get("tag") or item.get("aspect") or ""),
                "description": str(item.get("assertion") or ""),
                "screenshot": str(item.get("screenshot") or ""),
            }
        )

    non_vlm_tests = [
        item
        for item in manifest.get("tests") or []
        if isinstance(item, dict)
        and str(item.get("name") or "") not in old_names
        and not str(item.get("name") or "").split("::")[-1].endswith("_vlm")
    ]
    canonical_tests = [
        {
            "name": item["name"],
            "module": item["module"],
            "baseline_status": "passed",
        }
        for item in canonical
    ]
    tests = sorted(non_vlm_tests + canonical_tests, key=lambda item: item["name"])
    canonical_ids = {item["name"].split("::")[-1] for item in canonical}
    manifest["ignored"] = [
        item
        for item in manifest.get("ignored") or []
        if str(item.get("name") or "").split("::")[-1] not in canonical_ids
    ]
    manifest["tests"] = tests
    manifest["total"] = len(tests)
    manifest["vlm_assertions"] = canonical
    manifest["vlm_total"] = len(canonical)
    payload = "\n".join(item["name"] for item in tests)
    manifest["manifest_sha"] = hashlib.sha256(payload.encode()).hexdigest()[:12]
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def _dim_score(dim: dict | None, *, is_vlm: bool) -> float | None:
    """Extract one dimension's pass rate, or ``None`` when the run was invalid.

    Invalid (``None``) means the eval could not produce a verdict: no result file, an
    infra/judge error, a skipped VLM lane, or zero tests actually run. A model-failure
    error (no binary / no launch) is NOT invalid — it is a real 0.0.
    """
    if not dim:
        return None
    err = str(dim.get("error") or "").strip()
    if err:
        if err in _MODEL_FAILURE_ERRORS:
            return 0.0
        return None
    if is_vlm and str(dim.get("skipped") or "").strip():
        return None
    total = dim.get("total")
    if not total:  # 0 or missing: nothing scored -> not a defensible 0
        return None
    rate = dim.get("pass_rate")
    if rate is None:
        passed = dim.get("passed")
        rate = (passed / total) if (passed is not None) else None
    return None if rate is None else round(float(rate), 4)


def finalize_scores(
    programmatic: dict | None,
    vlm: dict | None,
    *,
    instance_id: str = "",
    platform: str = "",
) -> dict:
    """Fold native programmatic + VLM results into the settled per-app metric surface.

    Returns ``{instance_id, platform, program_score, vlm_score, prog_vlm_avg}`` where
    each score is a 0..1 float or ``None``. ``prog_vlm_avg`` is the mean of the
    dimensions that are present; it is ``None`` only when both dimensions are invalid.
    """
    prog = _dim_score(programmatic, is_vlm=False)
    v = _dim_score(vlm, is_vlm=True)
    present = [x for x in (prog, v) if x is not None]
    avg = round(sum(present) / len(present), 4) if present else None
    return {
        "instance_id": instance_id,
        "platform": platform,
        "program_score": prog,
        "vlm_score": v,
        "prog_vlm_avg": avg,
    }


def finalize_from_combined(
    combined: dict, *, instance_id: str = "", platform: str = ""
) -> dict:
    """Normalize the desktop ``atspi_eval.json`` ``{programmatic, vlm}`` shape."""
    return finalize_scores(
        combined.get("programmatic"),
        combined.get("vlm"),
        instance_id=instance_id or combined.get("instance_id", ""),
        platform=platform or combined.get("platform", ""),
    )


def unified_app_candidates(task_id: str, override: str = "") -> list[str]:
    """Plausible unified app keys for a run's task id, most specific first.

    Unified instances are keyed by the bare app name while some older run IDs include
    the ``bench50-`` compatibility prefix and a trailing variant. A consumer that passes
    such an ID through unchanged may miss the unified input and silently fall back to
    legacy per-stage bundles. Try the supported derivations rather than assuming one.

    ``override`` (RB_UNIFIED_APP) wins outright, for naming no derivation would catch.
    NOTE: a variant containing a hyphen (``unieval-instance``) defeats the
    trailing-segment strip; keep variants hyphenless (``unievalinst``).
    """
    override = (override or "").strip()
    if override:
        return [override]
    out: list[str] = []
    seen: set[str] = set()
    bare = task_id.removeprefix("bench50-")
    for c in (bare, task_id):
        if not c:
            continue
        for v in (c, c.rsplit("-", 1)[0] if "-" in c else ""):
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return out


def unified_prefixes(
    task_id: str, unified_prefix: str, platform: str, override: str = ""
) -> list[str]:
    """Artifact-store prefixes to try for a unified instance, in candidate order."""
    base = unified_prefix.rstrip("/")
    return [
        f"{base}/{platform}/{c}/" for c in unified_app_candidates(task_id, override)
    ]


def copy_unified_from_local(
    root: str | Path,
    platform: str,
    task_id: str,
    dest: str | Path,
    components: tuple[str, ...] = ("tests",),
    *,
    override: str = "",
    log=print,
) -> dict:
    """Copy one released input from a caller-owned local directory.

    Accepted roots are the dataset root, a platform directory, or the task
    directory itself. The return shape matches :func:`download_unified` so
    platform runners can select storage at their outer boundary without
    changing materialization or scoring.
    """

    root = Path(root).expanduser().resolve()
    dest = Path(dest)
    names = [
        name
        for name in unified_app_candidates(task_id, override)
        if Path(name).name == name and name not in {".", ".."}
    ]
    candidates: list[Path] = []
    if (root / "instance.json").is_file():
        candidates.append(root)
    for name in names:
        candidates.extend((root / platform / name, root / name))

    rep: dict = {"prefix": "", "tried": [str(path) for path in candidates]}
    source = next(
        (path for path in candidates if (path / "instance.json").is_file()), None
    )
    if source is None:
        log(f"  no local unified instance for {task_id} (tried {rep['tried']})")
        return rep

    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "instance.json", dest / "instance.json")
    optional_manifest = source / "vlm_assertions.json"
    if optional_manifest.is_file():
        shutil.copy2(optional_manifest, dest / optional_manifest.name)
    for component in components:
        target = dest / component
        shutil.rmtree(target, ignore_errors=True)
        source_component = source / component
        if source_component.is_dir():
            shutil.copytree(source_component, target)
            rep[component] = sum(1 for path in target.rglob("*") if path.is_file())
        else:
            rep[component] = 0
    rep["prefix"] = str(source)
    log(
        f"  local unified {source}: "
        + " ".join(f"{c}={rep.get(c, 0)}" for c in components)
    )
    return rep


def download_unified(
    prefix: str,
    platform: str,
    task_id: str,
    dest: str | Path,
    components: tuple[str, ...] = ("tests",),
    *,
    override: str = "",
    log=print,
    store: ArtifactStore | None = None,
) -> dict:
    """Fetch components of a unified instance into ``dest`` (pod-side).

    Each platform previously hand-rolled this: linux inlines it into a VM script, windows
    has it in its orchestrator, macOS drives its VM over ssh. This is the shared pod-side
    version — flattening each component into ``dest/<component>`` — so a track that can
    import rb_unify does not need a fourth copy.

    Returns ``{"prefix": <resolved>, <component>: <file count>, "tried": [...]}``. An empty
    ``prefix`` in the result means nothing matched: the caller must fall back and SAY so
    rather than proceed as if the unified task had been used.
    """
    rep: dict = {"prefix": "", "tried": []}
    dest = Path(dest)
    if store is None:
        from infrastructure.artifacts import artifact_store_from_environment

        store = artifact_store_from_environment(log=lambda message: log(f"  {message}"))
    chosen = ""
    chosen_keys: list[str] = []
    for pfx in unified_prefixes(task_id, prefix, platform, override):
        rep["tried"].append(pfx)
        keys = list(store.iter_keys(pfx))
        if keys:
            chosen = pfx
            chosen_keys = keys
            break
    if not chosen:
        log(f"  no unified instance for {task_id} (tried {rep['tried']})")
        return rep
    rep["prefix"] = chosen
    for comp in components:
        rep[comp] = 0
    for key in chosen_keys:
        if key.endswith("/"):
            continue
        rel = key[len(chosen) :]
        for comp in components:
            head = f"{comp}/"
            if rel.startswith(head):
                out = dest / comp / rel[len(head) :]
                out.parent.mkdir(parents=True, exist_ok=True)
                store.get_file(key, out)
                # Object stores do not preserve POSIX mode bits.  These two files are
                # executable interfaces in the frozen reference contract, so restore
                # that part of the contract at the shared download boundary.
                if comp == "reference" and out.name in {"build.sh", "launch.sh"}:
                    out.chmod(out.stat().st_mode | 0o111)
                rep[comp] += 1
                break
        else:
            if rel in ("instance.json", "vlm_assertions.json"):
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                store.get_file(key, out)
    log(f"  unified {chosen}: " + " ".join(f"{c}={rep.get(c, 0)}" for c in components))
    return rep


def materialize_web_dataset(
    instance_dir: str | Path, dataset_root: str | Path, domain: str = ""
) -> dict:
    """Turn a unified web instance back into the dataset dir the web runner consumes.

    web is the one track whose eval takes a DATASET DIRECTORY rather than restoring stage
    bundles (``run_agent.py --dataset``), and whose scorer reads fixed paths under it::

        <dataset>/<domain>/task.json                      the task spec (REQUIRED)
        <dataset>/<domain>/site_meta.json                  page list / nav graph
        <dataset>/<domain>/site/                          the served reference
        <dataset>/<domain>/evaluation/eval_config.json     dimension weights + vlm_judge
        <dataset>/<domain>/evaluation/tests/{scripted,agent_gen}/*.spec.ts
        <dataset>/<domain>/evaluation/gt_dom|gt_screenshots|gt_layout|gt_regions/

    So "use the unified task" for web means materialising that shape. Two things are worth
    knowing about the result:

    * The release re-files web's specs by what they actually assert
      (``tests/static``, ``tests/interactive``); this inverts that renaming back to the
      generator names + authorship dirs the scorer groups by, so the per-source weighting
      (scripted 0.6 / agent_generated 0.4) still applies.
    * ``task.json`` and ``site_meta.json`` ship under ``reference/`` and are placed back
      at the domain root. ``task.json`` is not optional: the runner and the scorer both
      abort without it, and it carries the prompt / target pages / time limit. Missing
      it is reported as ``task_json: False`` so the caller can fail Phase 1 loudly
      instead of surfacing an opaque "task.json not found" from the runner.
    * ``gt_*`` (screenshots / DOM / layout / regions) ships inside the release under
      ``tests/gt/`` and is placed back under ``evaluation/`` here, because regenerating it
      would have to reproduce the original capture pipeline exactly — any Chrome, font or
      timing difference moves the visual baseline silently. ``needs_gt`` is False once it
      is in place, and True for an older instance that predates shipping it.
    """
    # Frozen-name -> original runner location. This is a read-side contract and belongs beside
    # materialization; the deleted package_release module was an authoring-only producer.
    web_lane_map = {
        ("functional_interaction.spec.ts", "agent_gen"): (
            "interactive",
            "nav_transition.spec.ts",
        ),
        ("functional_interaction.spec.ts", "root"): (
            "interactive",
            "widget_reveal.spec.ts",
        ),
        ("secondary.spec.ts", "root"): ("interactive", "restored_widget.spec.ts"),
        ("functional.spec.ts", "scripted"): ("static", "content.scripted.spec.ts"),
        ("functional.spec.ts", "agent_gen"): ("static", "content.agent_gen.spec.ts"),
        ("structural.spec.ts", "scripted"): ("static", "structure.scripted.spec.ts"),
        ("structural.spec.ts", "agent_gen"): ("static", "structure.agent_gen.spec.ts"),
        ("quality.spec.ts", "scripted"): ("static", "hygiene.scripted.spec.ts"),
        ("quality.spec.ts", "agent_gen"): ("static", "hygiene.agent_gen.spec.ts"),
        ("visual.spec.ts", "agent_gen"): ("static", "layout.agent_gen.spec.ts"),
    }

    inst = Path(instance_dir)
    domain = domain or inst.name
    task_dir = Path(dataset_root) / domain
    rep: dict = {
        "domain": domain,
        "task_json": False,
        "site_meta": False,
        "site": 0,
        "specs": 0,
        "eval_config": False,
        "needs_gt": True,
        "notes": [],
    }

    # released name -> (authorship, generator basename)
    inverse = {
        released: (authorship, source)
        for (source, authorship), (part, released) in web_lane_map.items()
        if part and released
    }

    arch = inst / "reference" / "reference.tar.gz"
    if arch.is_file():
        import tarfile

        with tarfile.open(arch) as tf:
            tf.extractall(task_dir)  # noqa: S202 - our own release artifact
        site = task_dir / "site"
        rep["site"] = sum(1 for _ in site.rglob("*")) if site.is_dir() else 0
        if not rep["site"]:
            rep["notes"].append("reference.tar.gz held no site/")
    else:
        rep["notes"].append("reference.tar.gz missing (no reference to serve)")

    # the task spec sits beside the site in the release; the runner reads it at the
    # domain root. task.json absent -> the runner exits with "task.json not found"
    # before the agent starts, so say so here where the cause is still visible.
    task_dir.mkdir(parents=True, exist_ok=True)
    for fn, key in (("task.json", "task_json"), ("site_meta.json", "site_meta")):
        srcf = inst / "reference" / fn
        if srcf.is_file():
            shutil.copy2(srcf, task_dir / fn)
            rep[key] = True
        else:
            rep["notes"].append(f"{fn} missing from reference/")

    ev = task_dir / "evaluation"
    (ev / "tests").mkdir(parents=True, exist_ok=True)
    kit_dirs: set = set()
    for spec in sorted((inst / "tests").rglob("*.spec.ts")):
        authorship, source = inverse.get(spec.name, ("", ""))
        if not source:
            rep["notes"].append(f"unmapped spec {spec.name} -> tests/ root")
            authorship, source = "root", spec.name
        out_dir = ev / "tests" if authorship == "root" else ev / "tests" / authorship
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(spec, out_dir / source)
        rep["specs"] += 1
        if "from './rb_web_kit'" in spec.read_text(errors="replace"):
            kit_dirs.add(out_dir)

    # rb_web_kit.ts is imported as a SIBLING ("./rb_web_kit"), and re-filing the specs by
    # what they assert scatters them across authorship dirs — so the kit has to follow each
    # one. Restoring only *.spec.ts leaves every behavioural spec unable to resolve its
    # import, which fails the whole interactive lane at compile time. Earlier releases
    # did not include an interactive lane, so they did not exercise this path.
    kit = inst / "tests" / "interactive" / "rb_web_kit.ts"
    if not kit.is_file():
        found = sorted((inst / "tests").rglob("rb_web_kit.ts"))
        kit = found[0] if found else kit
    if kit_dirs:
        if kit.is_file():
            for d in sorted(kit_dirs):
                shutil.copy2(kit, d / "rb_web_kit.ts")
            rep["kit_placed"] = len(kit_dirs)
        else:
            rep["notes"].append(
                f"rb_web_kit.ts missing but {len(kit_dirs)} spec(s) import it "
                "— the interactive lane cannot compile"
            )

    # ground truth ships with the release (see package_release: regenerating it risks
    # silently moving the visual/structural baseline), so place it where the scorer looks
    gt_root = inst / "tests" / "gt"
    if gt_root.is_dir():
        for gt in sorted(p2.name for p2 in gt_root.iterdir() if p2.is_dir()):
            shutil.copytree(gt_root / gt, ev / gt, dirs_exist_ok=True)
            rep[gt] = sum(1 for _ in (ev / gt).rglob("*"))
        rep["needs_gt"] = False

    cfg = inst / "tests" / "eval_config.json"
    if cfg.is_file():
        shutil.copy2(cfg, ev / "eval_config.json")
        rep["eval_config"] = True
    else:
        rep["notes"].append("eval_config.json missing (scorer falls back to defaults)")
    vr = inst / "tests" / "test_validation_report.json"
    if vr.is_file():
        shutil.copy2(vr, ev / "test_validation_report.json")
    # the unified VLM component reaches web's judge only from evaluation/ (the scorer
    # loads evaluation/vlm_assertions.json into the judge context); an instance that
    # ships none simply has nothing to place. It must be TRANSLATED, not copied: the
    # unified file is schema v2 and web's judge wants its own flat map (see
    # _web_vlm_from_unified).
    vlm = inst / "vlm_assertions.json"
    if vlm.is_file():
        try:
            payload = json.loads(vlm.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            rep["notes"].append(f"vlm_assertions.json unreadable ({exc})")
            return rep
        flat, stats = _web_vlm_from_unified(payload)
        if flat:
            # The five-platform release has one VLM contract: candidate-only assertions
            # owned by the frozen instance. Do not duplicate them into the legacy Web
            # comparison-checklist input and accidentally create a second grading mode.
            (ev / "vlm_assertions.json").write_text(
                json.dumps(flat, indent=2, ensure_ascii=False)
            )
            rep["vlm_assertions"] = True
            rep["vlm_views"] = len(flat)
            rep["vlm_items"] = stats["items"]
            if stats["dropped"]:
                rep["notes"].append(
                    f"{stats['dropped']} vlm assertion(s) dropped (no screenshot or text)"
                )
        else:
            rep["notes"].append(
                "vlm_assertions.json carried no placeable items "
                f"(shape={stats['shape']})"
            )
    return rep


def _web_vlm_from_unified(payload: dict) -> tuple[dict, dict]:
    """schema-v2 ``vlm_assertions.json`` -> web's ``{"<page>/<viewport>.png": [item]}``.

    web's judge groups by splitting the key on "/" (page, viewport) and accepts each item
    as a bare string or ``{"text", "weight"}``; see ``vlm_judge._group_by_page_viewport``
    and ``_norm_item``. The unified file is a different shape entirely
    (``{schema_version, instance_id, grading, totals, assertions: [...]}``), so copying it
    verbatim would have the judge treat "schema_version" and "grading" as page names.

    ``calibration.weight`` carries through, which is what lets the discriminative,
    page-specific assertions outweigh generic shared-chrome ones. ``discriminative`` and
    ``decoy_hit`` are deliberately NOT applied here: filtering on them would change the
    scored set, and the release carries them precisely so that is an explicit decision.
    Absence assertions (``expected: False``) have no representation in web's format —
    they are dropped rather than silently inverted; no web-origin assertion sets it.
    """
    if not isinstance(payload, dict):
        return {}, {"items": 0, "dropped": 0, "shape": type(payload).__name__}
    assertions = payload.get("assertions")
    if not isinstance(assertions, list):
        return {}, {"items": 0, "dropped": 0, "shape": "no assertions[]"}
    out: dict = {}
    items = dropped = 0
    for a in assertions:
        if not isinstance(a, dict):
            dropped += 1
            continue
        shot = str(a.get("screenshot") or "").strip()
        text = str(a.get("assertion") or "").strip()
        if not shot or not text or a.get("expected") is False:
            dropped += 1
            continue
        weight = (
            (a.get("calibration") or {})
            if isinstance(a.get("calibration"), dict)
            else {}
        ).get("weight")
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            weight = 1.0
        out.setdefault(shot, []).append({"text": text, "weight": weight})
        items += 1
    return out, {"items": items, "dropped": dropped, "shape": "schema_v2"}


def stage_instance(instance_dir: str | Path, dest: str | Path) -> dict:
    """Materialise a unified instance into the layout a platform eval already expects.

    The unified release standardises the eval INPUTS, but each platform runner still
    looks for them where it always did: a tests dir it can point pytest / its own
    harness at, a ``test_manifest.json`` for the frozen VLM denominator, and a launch
    script. Staging is that adaptation, kept in one place so no platform grows its own
    copy:

        <dest>/tests/                 the scoring suite + kit + conftest.py
        <dest>/test_manifest.json     derived from vlm_assertions.json (frozen VLM set)
        <dest>/reference/             launch script (+ build recipe, patches/archive) as shipped

    Returns a report naming what was staged, so a caller can log it instead of
    discovering a missing piece mid-run. Any reference archive is not unpacked here;
    web's frozen site and legacy source/APK fallbacks remain platform work.
    """
    inst, out = Path(instance_dir), Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    rep: dict = {"tests": 0, "manifest": 0, "reference": [], "notes": []}

    tests_src = inst / "tests"
    if tests_src.is_dir():
        shutil.copytree(tests_src, out / "tests", dirs_exist_ok=True)
        rep["tests"] = sum(1 for _ in (out / "tests").rglob("*") if _.is_file())
    else:
        rep["notes"].append("tests/ missing")

    if (inst / "vlm_assertions.json").is_file():
        man = manifest_from_instance(inst)
        (out / "test_manifest.json").write_text(
            json.dumps(man, indent=2, ensure_ascii=False)
        )
        rep["manifest"] = len(man["vlm_assertions"])
    else:
        # judge-time VLM (web/windows): no frozen set to stage, and the eval must not
        # invent one — a manifest of zero assertions is not the same as "none frozen".
        rep["notes"].append("vlm_assertions.json absent (judge-time VLM)")

    ref_src = inst / "reference"
    if ref_src.is_dir():
        ref_dest = out / "reference"
        shutil.rmtree(ref_dest, ignore_errors=True)
        shutil.copytree(ref_src, ref_dest)
        for item in sorted(path for path in ref_dest.rglob("*") if path.is_file()):
            relative = item.relative_to(ref_dest).as_posix()
            if item.name in {"build.sh", "launch.sh"}:
                item.chmod(item.stat().st_mode | 0o111)
            rep["reference"].append(relative)
    else:
        rep["notes"].append("reference/ missing")
    return rep


def _main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Unified eval input/output bridge")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("manifest", help="vlm_assertions.json -> test_manifest.json")
    m.add_argument("instance_dir")
    m.add_argument("out")

    am = sub.add_parser(
        "android-manifest",
        help="replace Android test_manifest.json's VLM subset from vlm_assertions.json",
    )
    am.add_argument("vlm_assertions")
    am.add_argument("test_manifest")

    s = sub.add_parser("stage", help="unified instance -> eval-ready layout")
    s.add_argument("instance_dir")
    s.add_argument("dest")

    r = sub.add_parser(
        "resolve",
        help="print the artifact store prefix of a task's unified instance (candidate order)",
    )
    r.add_argument("--task-id", required=True)
    r.add_argument("--prefix", required=True)
    r.add_argument("--platform", required=True)
    r.add_argument("--override", default="")
    r.add_argument(
        "--probe",
        action="store_true",
        help="check the configured artifact store and print only the prefix that exists;"
        " without it, print every candidate so a caller can probe with its own client",
    )

    f = sub.add_parser("finalize", help="programmatic + vlm results -> {prog,vlm,avg}")
    f.add_argument("--programmatic", help="programmatic_results.json")
    f.add_argument("--vlm", help="vlm_results.json")
    f.add_argument("--combined", help="atspi_eval.json ({programmatic,vlm})")
    f.add_argument("--instance", default="")
    f.add_argument("--platform", default="")
    f.add_argument("--out", required=True)

    args = ap.parse_args(argv)
    if args.cmd == "resolve":
        # The app key needs RESOLVING, not string-concatenating: a submitted task id may
        # carry a platform-specific variant suffix while the release key does not. Shell
        # callers (android's main.sh) had their own concatenation and were
        # one variant away from silently matching nothing, which is how the macOS canary
        # spent two hours authoring tests instead of restoring the frozen suite.
        cands = unified_prefixes(
            args.task_id, args.prefix, args.platform, args.override
        )
        if not args.probe:
            for c in cands:
                print(c)
            return 0
        import os as _os

        from infrastructure.artifacts import artifact_store_from_environment

        store = artifact_store_from_environment(
            dict(_os.environ),
            log=lambda _message: None,
        )
        for c in cands:
            if next(iter(store.iter_keys(c)), None) is not None:
                print(c)
                return 0
        return 1  # nothing matched: the caller must fall back and SAY so

    if args.cmd == "stage":
        rep = stage_instance(args.instance_dir, args.dest)
        print(json.dumps(rep, ensure_ascii=False))
        return 0

    if args.cmd == "manifest":
        payload = manifest_from_instance(args.instance_dir)
        Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"manifest: {len(payload['vlm_assertions'])} assertions -> {args.out}")
        return 0

    if args.cmd == "android-manifest":
        payload = canonicalize_android_manifest(args.vlm_assertions, args.test_manifest)
        print(
            "android manifest: "
            f"{payload['vlm_total']} VLM / {payload['total']} total -> "
            f"{args.test_manifest}"
        )
        return 0

    if args.combined:
        combined = json.loads(Path(args.combined).read_text())
        scores = finalize_from_combined(
            combined, instance_id=args.instance, platform=args.platform
        )
    else:
        prog = (
            json.loads(Path(args.programmatic).read_text())
            if args.programmatic
            else None
        )
        vlm = json.loads(Path(args.vlm).read_text()) if args.vlm else None
        scores = finalize_scores(
            prog, vlm, instance_id=args.instance, platform=args.platform
        )
    Path(args.out).write_text(json.dumps(scores, indent=2))
    print(json.dumps(scores))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
