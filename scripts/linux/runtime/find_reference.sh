
EVAL_RECREATION=/workspace/recreation
BINARY=$(python3 "$RB_CODE_DIR/scripts/linux/runtime/runtime_metadata.py" \
    executable /workspace/reference_meta/build_result.json --require-existing)
if [ -z "$BINARY" ]; then
    # EVAL_RECREATION is a symlink to the preserved reference install.  GNU
    # find does not descend through a command-line symlink under its default
    # -P mode, which made every install without a top-level bin/ look empty.
    BINARY=$(find -L "$EVAL_RECREATION/bin" "$EVAL_RECREATION" -type f -executable \
        -not -name "*.sh" -not -name "*.py" -not -name "*.so" \
        -not -name "*.so.*" -not -name "*.node" -not -name "*.js" \
        -not -name "chrome*" \
        2>/dev/null | head -1)
fi
LAUNCH_SH="$EVAL_RECREATION/launch.sh"
