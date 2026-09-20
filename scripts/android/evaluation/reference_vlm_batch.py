#!/usr/bin/env python3
"""Batch frozen Android VLM assertions during reference-only evaluation."""

from __future__ import annotations

from typing import Any

_REQUIRED = {
    "RESULTS_DIR",
    "_flush",
    "_results",
    "record",
    "save_results",
    "screencap",
    "vlm_assert",
}


def install(namespace: dict[str, Any], shared_judge: Any) -> None:
    """Replace a frozen kit's synchronous VLM hooks with fail-closed batching."""
    missing = sorted(_REQUIRED.difference(namespace))
    if missing:
        raise RuntimeError(
            "Android reference VLM batching requires frozen kit symbols: "
            + ", ".join(missing)
        )

    original_save_results = namespace["save_results"]
    pending: list[dict[str, Any]] = []

    def record_failure(item: dict[str, Any], message: str) -> bool:
        log_assertion = namespace.get("_vlm_log")
        if log_assertion is not None:
            log_assertion(
                item["results_dir"],
                {
                    "test_name": item["test_name"],
                    "question": item["question"],
                    "screenshot": item["screenshot"],
                    "vlm_response": None,
                    "vlm_answer": "",
                    "expected": item["expected"],
                    "passed": False,
                    "tag": item["tag"],
                    "error": message,
                },
            )
        return namespace["record"](
            item["test_name"],
            False,
            message,
            depth=item["depth"],
            jump_kind=item["jump_kind"],
            expect=f"VLM {item['expected']}: {item['question']}",
            tag=item["tag"] or None,
        )

    def vlm_assert(
        test_name: str,
        question: str,
        results_dir: str | None = None,
        expect: str = "YES",
        *,
        depth: int | None = None,
        jump_kind: str = "none",
        tag: str | None = None,
    ) -> bool:
        if not test_name.endswith("_vlm"):
            test_name += "_vlm"
        os_module = namespace["os"]
        result_dir = results_dir or namespace["RESULTS_DIR"]
        screenshot = os_module.path.join(result_dir, "screenshots", f"{test_name}.png")
        item = {
            "test_name": test_name,
            "question": question,
            "screenshot": screenshot,
            "results_dir": result_dir,
            "expected": str(expect).upper(),
            "depth": depth,
            "jump_kind": jump_kind,
            "tag": tag or "",
        }
        if not namespace["screencap"](screenshot):
            return record_failure(item, "setup_failed: screenshot capture failed")

        # The provisional failure satisfies frozen denominator checks and remains
        # fail-closed if the process exits before module finalization.
        namespace["record"](
            test_name,
            False,
            "setup_failed: reference VLM verdict pending",
            depth=depth,
            jump_kind=jump_kind,
            expect=f"VLM {item['expected']}: {question}",
            tag=tag,
        )
        pending.append(item)
        return True

    def finalize() -> None:
        if not pending:
            return
        os_module = namespace["os"]
        config = shared_judge.resolve(
            model=os_module.environ.get("JUDGE_MODEL", ""),
            base_url=os_module.environ.get("VLM_BASE_URL", ""),
        )
        batch_size = shared_judge.DEFAULT_BATCH_SIZE
        records = {item.get("test"): item for item in namespace["_results"]}
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset : offset + batch_size]
            try:
                verdicts = shared_judge.judge_assertions(
                    [item["screenshot"] for item in batch],
                    [item["question"] for item in batch],
                    paired=True,
                    cfg=config,
                )
            except Exception as exc:
                verdicts = [
                    {"pass": None, "error": f"{type(exc).__name__}: {exc}"}
                    for _ in batch
                ]
            for index, item in enumerate(batch):
                verdict = (
                    verdicts[index]
                    if index < len(verdicts)
                    else {"pass": None, "error": "missing batch verdict"}
                )
                raw_pass = verdict.get("pass")
                answer = (
                    "YES" if raw_pass is True else "NO" if raw_pass is False else ""
                )
                if raw_pass is None:
                    passed = False
                    message = "setup_failed: VLM API call failed: " + str(
                        verdict.get("error") or verdict.get("reason") or "unjudged"
                    )
                else:
                    passed = answer == item["expected"]
                    message = (
                        f"VLM: Q='{item['question']}' A={answer} expect={item['expected']}"
                        if passed
                        else f"behavior_wrong: VLM Q='{item['question']}' "
                        f"A={answer} expect={item['expected']}"
                    )
                result = records[item["test_name"]]
                result["passed"] = passed
                result["message"] = message
                log_entry = {
                    "test_name": item["test_name"],
                    "question": item["question"],
                    "screenshot": item["screenshot"],
                    "vlm_response": verdict,
                    "vlm_answer": answer,
                    "expected": item["expected"],
                    "passed": passed,
                    "tag": item["tag"],
                }
                if raw_pass is None:
                    log_entry["error"] = message
                log_assertion = namespace.get("_vlm_log")
                if log_assertion is not None:
                    log_assertion(item["results_dir"], log_entry)
                module = namespace.get("_module", "")
                print(
                    f"  [{'PASS' if passed else 'FAIL'}] {module}:"
                    f"{item['test_name']} {message if not passed else ''}"
                )
                namespace["_flush"]()
        pending.clear()

    def save_results(module_name: str):
        finalize()
        return original_save_results(module_name)

    namespace["vlm_assert"] = vlm_assert
    namespace["save_results"] = save_results
