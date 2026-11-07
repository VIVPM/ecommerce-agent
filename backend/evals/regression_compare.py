"""Compare saved agent evals without calling a model or mixing judge and code checks."""

import json
from pathlib import Path


DETERMINISTIC = ("route_ok", "products", "photo", "leak_check", "privacy")


def _passed(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return value.get("pass")
    return None


def compare(baseline_rows, candidate_rows, case_ids):
    baseline = {row["id"]: row for row in baseline_rows}
    candidate = {row["id"]: row for row in candidate_rows}
    missing = [cid for cid in case_ids if cid not in baseline or cid not in candidate]
    if missing:
        raise ValueError(f"Missing comparison cases: {', '.join(missing)}")
    cases = []
    for cid in case_ids:
        before, after = baseline[cid], candidate[cid]
        code = {}
        for name in DETERMINISTIC:
            old, new = _passed(before.get(name)), _passed(after.get(name))
            if old is not None or new is not None:
                code[name] = {"baseline": old, "candidate": new,
                              "regression": old is True and new is False}
        judge = {}
        for name in sorted(set(before.get("geval", {})) | set(after.get("geval", {}))):
            old, new = before.get("geval", {}).get(name), after.get("geval", {}).get(name)
            judge[name] = {"baseline_pass": old.get("pass") if old else None,
                           "candidate_pass": new.get("pass") if new else None,
                           "baseline_score": old.get("score") if old else None,
                           "candidate_score": new.get("score") if new else None,
                           "pass_to_fail": bool(old and old.get("pass") and new and not new.get("pass"))}
        cases.append({"id": cid, "run_error": after.get("error"),
                      "deterministic": code, "llm_judge": judge})
    return {"suite_cases": len(case_ids), "candidate_errors": sum(bool(c["run_error"]) for c in cases),
            "deterministic_regressions": [c["id"] for c in cases if any(
                v["regression"] for v in c["deterministic"].values())],
            "judge_pass_to_fail": [c["id"] for c in cases if any(
                v["pass_to_fail"] for v in c["llm_judge"].values())],
            "cases": cases}


def compare_files(baseline_path, candidate_path, case_ids):
    return compare(json.loads(Path(baseline_path).read_text(encoding="utf-8")),
                   json.loads(Path(candidate_path).read_text(encoding="utf-8")), case_ids)
