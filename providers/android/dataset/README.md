# Android evaluation dataset

Place the complete released dataset in this directory without changing its hierarchy
or filenames. The runner mounts it read-only into a root-owned worker directory; the
recreation agent cannot read the evaluation files.

```text
providers/android/dataset/
  android/
    <APP_ID>/
      instance.json
      vlm_assertions.json
      reference/
        launch.sh
        patches/              # present only when declared by instance.json
        screenshots/
      tests/
        test_manifest.json
        android_testgen_kit.py
        test_android_*.py
```

Validate one task:

```bash
python3 providers/android/validate-fixed-suite.py --app-id dozingcat-vector-pinball
```

Omit `--app-id` to validate all 50 released tasks. Android reference APKs are built in
the trusted worker from the `repo`, `commit`, and optional patch metadata in
`instance.json`; the wrapsource release does not contain APK archives. The dataset
contents are excluded by `.gitignore`.
