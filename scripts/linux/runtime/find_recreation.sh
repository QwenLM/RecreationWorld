
EVAL_RECREATION=/workspace/evaluation/recreation
if [ -f /workspace/recreation/build.sh ]; then
    rm -rf "$EVAL_RECREATION"
    mkdir -p "$EVAL_RECREATION"
    tar -C /workspace/recreation \
        --exclude='./build' \
        --exclude='./bin' \
        --exclude='./node_modules' \
        --exclude='./target' \
        --exclude='./dist' \
        --exclude='./.git' \
        -cf - . | tar -C "$EVAL_RECREATION" -xf -
    cd "$EVAL_RECREATION"
    if ! bash build.sh > /tmp/eval_recreation_build.log 2>&1; then
        echo "ERROR: recreation build.sh failed"
        tail -80 /tmp/eval_recreation_build.log
        echo '{"error": "candidate_build_failed"}' > "$RESULTS_DIR/atspi_eval.json"
        exit 1
    fi
    tail -20 /tmp/eval_recreation_build.log
fi
BINARY=$(find "$EVAL_RECREATION/bin" -maxdepth 1 -type f -executable -not -name "*.so" -not -name "*.so.*" -not -name "*.node" -not -name "chrome*" 2>/dev/null | head -1)
LAUNCH_SH="$EVAL_RECREATION/launch.sh"
