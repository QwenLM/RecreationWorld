# RB_SHARED_VLM_JUDGE_V1
# Runtime adapter only: frozen assertion and result semantics stay in this module.
import importlib.util as _rb_importlib_util

_rb_judge_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vlm_judge.py")
_rb_judge_spec = _rb_importlib_util.spec_from_file_location(
    "_rb_shared_vlm_judge", _rb_judge_path
)
if _rb_judge_spec is None or _rb_judge_spec.loader is None:
    raise RuntimeError(f"cannot load shared VLM judge: {_rb_judge_path}")
_rb_shared_vlm_judge = _rb_importlib_util.module_from_spec(_rb_judge_spec)
_rb_judge_spec.loader.exec_module(_rb_shared_vlm_judge)


def call_vlm(image_path: str, prompt: str, *, retries: int = 0) -> str | None:
    """Compatibility adapter: use the canonical prompt/transport, return YES/NO."""
    match = re.search(r"Question:\s*(.*?)(?:\n\s*\nAnswer|$)", prompt, re.DOTALL)
    assertion = match.group(1).strip() if match else prompt.strip()
    cfg = _rb_shared_vlm_judge.resolve(
        model=os.environ.get("JUDGE_MODEL", ""),
        base_url=os.environ.get("VLM_BASE_URL", ""),
    )
    # Frozen kits historically define VLM_RETRIES=3 (total attempts), which silently
    # bypassed the shared judge's longer retry policy.  A caller-provided non-zero value
    # still means total attempts for compatibility; otherwise use the canonical retry
    # count (including its RB_VLM_JUDGE_RETRIES environment override).
    retry_count = (
        max(0, retries - 1)
        if retries
        else _rb_shared_vlm_judge.DEFAULT_RETRIES
    )
    verdict = _rb_shared_vlm_judge.judge_screenshot(
        image_path, assertion, cfg=cfg, retries=retry_count
    )
    if verdict.get("pass") is None:
        print(f"[VLM] shared judge failed: {verdict.get('error', 'unjudged')}")
        return None
    return "YES" if verdict["pass"] else "NO"


if os.environ.get("RB_EVAL_TARGET", "").strip().lower() == "reference":
    _rb_batch_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "reference_vlm_batch.py"
    )
    _rb_batch_spec = _rb_importlib_util.spec_from_file_location(
        "_rb_reference_vlm_batch", _rb_batch_path
    )
    if _rb_batch_spec is None or _rb_batch_spec.loader is None:
        raise RuntimeError(f"cannot load Android reference VLM batcher: {_rb_batch_path}")
    _rb_reference_vlm_batch = _rb_importlib_util.module_from_spec(_rb_batch_spec)
    _rb_batch_spec.loader.exec_module(_rb_reference_vlm_batch)
    _rb_reference_vlm_batch.install(globals(), _rb_shared_vlm_judge)
