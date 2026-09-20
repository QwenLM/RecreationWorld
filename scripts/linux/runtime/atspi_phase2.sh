#!/bin/bash
set -e
trap 'rc=$?; echo "[phase2] FAILED rc=$rc at line $LINENO: $BASH_COMMAND" >&2; exit $rc' ERR
# __VLM_TRANSPORT__
RESULTS_DIR="${RESULTS_DIR:-__RESULTS_DIR__}"
export RESULTS_DIR
RB_EVAL_RESULTS_HELPER="__RB_CODE_DIR__/scripts/linux/runtime/eval_results.py"

merge_results() {
    python3 "$RB_EVAL_RESULTS_HELPER" merge "$RESULTS_DIR"
}

mkdir -p "$RESULTS_DIR/screenshots"
MANIFEST_VLM=0
_MF="$RESULTS_DIR/test_manifest.json"
[ -f "$_MF" ] || _MF="$RESULTS_DIR/../tests/test_manifest.json"
[ -r "$_MF" ] || { echo "ERROR: frozen test_manifest.json missing before VLM eval" >&2; exit 1; }
MANIFEST_VLM=$(python3 "$RB_EVAL_RESULTS_HELPER" manifest-count "$_MF") || {
    echo "ERROR: frozen test_manifest.json unreadable before VLM eval" >&2
    exit 1
}

if [ "$MANIFEST_VLM" -eq 0 ]; then
    echo "No frozen manifest VLM assertions, skipping VLM eval"
    python3 "$RB_EVAL_RESULTS_HELPER" write-skipped "$RESULTS_DIR"
    merge_results
    exit 0
fi

# Replace the candidate-authored map with the frozen assertion descriptions when present.
# --missing-as-fail then records "screenshot not produced by candidate" for every missing image,
# keeping the frozen denominator without maintaining a second judge implementation here.
if [ "$MANIFEST_VLM" -gt 0 ]; then
    python3 "$RB_EVAL_RESULTS_HELPER" apply-manifest \
        "$_MF" "$RESULTS_DIR/screenshots/assertions.json"

    # Phase 1 records every runtime screenshot assertion in an append-only JSONL
    # so parallel pytest modules cannot clobber one another.  That file is useful
    # while authoring a baseline, but candidate evaluation must use only the
    # frozen manifest above.  The shared judge otherwise unions the JSONL with
    # assertions.json and lets runtime-only assertions pollute the denominator.
    rm -f \
        "$RESULTS_DIR/screenshots/screenshot_asserts.jsonl" \
        "$RESULTS_DIR/screenshot_asserts.jsonl"
fi

SS_COUNT=$(find "$RESULTS_DIR/screenshots" -maxdepth 1 -name '*.png' -type f | wc -l)
ASSERT_COUNT=$(python3 "$RB_EVAL_RESULTS_HELPER" assertion-count \
    "$RESULTS_DIR/screenshots/assertions.json" 2>/dev/null || echo 0)
echo "VLM Judge: $SS_COUNT screenshots, $ASSERT_COUNT assertions"

VLM_STATUS=0
python3 "$RESULTS_DIR/common/vlm_judge.py" \
    "$RESULTS_DIR/screenshots" \
    --model "${VLM_MODEL:?VLM_MODEL is required}" \
    --base-url "${VLM_BASE_URL:?VLM_BASE_URL is required}" \
    --missing-as-fail \
    --output "$RESULTS_DIR/vlm_results.json" || VLM_STATUS=$?

if [ "$VLM_STATUS" -ne 0 ] || [ ! -f "$RESULTS_DIR/vlm_results.json" ]; then
    if [ ! -f "$RESULTS_DIR/vlm_results.json" ]; then
        if python3 "$RB_EVAL_RESULTS_HELPER" validate-json \
            "$RESULTS_DIR/vlm_results.partial.json"
        then
            cp "$RESULTS_DIR/vlm_results.partial.json" "$RESULTS_DIR/vlm_results.json"
        else
        python3 "$RB_EVAL_RESULTS_HELPER" write-judge-error "$RESULTS_DIR"
        fi
    fi
    merge_results
    exit 1
fi
merge_results
