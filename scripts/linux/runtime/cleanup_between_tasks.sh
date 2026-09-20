
pkill -9 -f bwrap 2>/dev/null || true
pkill -9 -f cua-driver 2>/dev/null || true
pkill -9 -f 'claude.*rb-' 2>/dev/null || true
sleep 1
rm -rf __TASK_DIR__
