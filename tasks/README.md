# Task manifests

This directory indexes the released 250-task RecreationBench suite, with 50 tasks
for each platform.

| File | Platform | Tasks |
| --- | --- | ---: |
| [ubuntu.jsonl](ubuntu.jsonl) | Ubuntu/Linux | 50 |
| [windows.jsonl](windows.jsonl) | Windows | 50 |
| [macos.jsonl](macos.jsonl) | macOS | 50 |
| [android.jsonl](android.jsonl) | Android | 50 |
| [web.jsonl](web.jsonl) | Web | 50 |

Each line contains one JSON object:

```json
{"instance_id":"adrienverge-photocollage","difficulty":"easy"}
```

`instance_id` matches the application directory in the released dataset. All five
platforms use their original, unprefixed IDs. `difficulty` is one of `easy`,
`medium`, or `hard`.

Files are sorted by `instance_id`. Use `(platform, instance_id)` for cross-platform
identity; runtime platform `linux` corresponds to manifest platform `ubuntu`.
