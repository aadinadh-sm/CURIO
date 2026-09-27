"""Train CURIO models from independently labeled, controlled 5-second feature windows.

CSV columns: session_id,label,<the feature columns shown in curio.py>.
Never split windows randomly: adjacent windows from one run must remain together.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
import joblib

from curio import FEATURES, MODEL_PATH

ALLOWED = {"normal", "cpu_saturation", "memory_pressure", "disk_io_bottleneck", "thermal_stress"}


def main():
    parser = argparse.ArgumentParser(description="Train CURIO's classifier and normal-state anomaly detector.")
    parser.add_argument("csv_path", type=Path, help="Controlled feature-window CSV with human-verified labels")
    parser.add_argument("--output", type=Path, default=MODEL_PATH, help="Model artifact path (default: curio_model.joblib)")
    parser.add_argument("--group-by-machine", action="store_true", help="Hold out whole machines instead of whole sessions; requires at least 3 machines per class")
    args = parser.parse_args()
    rows = list(csv.DictReader(args.csv_path.open(newline="", encoding="utf-8-sig")))
    required = {"session_id", "label", *FEATURES}
    if not rows or not required.issubset(rows[0]):
        raise SystemExit("CSV needs session_id, label, and every feature column. Export CURIO windows, then fill the label column from your controlled experiment protocol.")
    if any(not row.get("label", "").strip() for row in rows):
        raise SystemExit("Every row needs an independently verified label. Blank labels are never inferred from CURIO's own rule result.")
    unknown = sorted({r["label"].strip() for r in rows} - ALLOWED)
    if unknown:
        raise SystemExit(f"Unsupported label(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(ALLOWED))}")
    if args.group_by_machine and any(not r.get("machine_id", "").strip() for r in rows):
        raise SystemExit("--group-by-machine needs an anonymized machine_id on every row (for example M1, M2).")
    groups = [r["machine_id"].strip() if args.group_by_machine else r["session_id"] for r in rows]
    labels = [r["label"].strip() for r in rows]
    by_class = defaultdict(set)
    for index, row in enumerate(rows):
        by_class[row["label"].strip()].add(groups[index])
    weak = [f"{name}: {len(sessions)}" for name, sessions in by_class.items() if len(sessions) < 3]
    if weak:
        raise SystemExit("Need at least 3 independent sessions per included class for a session-held-out evaluation; found " + ", ".join(weak))
    X = [[float(row[name]) if row.get(name, "").strip() else float("nan") for name in FEATURES] for row in rows]
    splitter = GroupShuffleSplit(n_splits=500, test_size=.34, random_state=37)
    split = next(((tr, te) for tr, te in splitter.split(X, labels, groups)
                  if set(labels[i] for i in tr) == set(labels) and set(labels[i] for i in te) == set(labels)), None)
    if split is None:
        raise SystemExit("Could not make a session-held-out split containing every class on both sides. Collect more sessions per class.")
    train_idx, test_idx = split
    classifier = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(),
                               RandomForestClassifier(n_estimators=300, min_samples_leaf=2, class_weight="balanced_subsample", random_state=37))
    classifier.fit([X[i] for i in train_idx], [labels[i] for i in train_idx])
    predicted = classifier.predict([X[i] for i in test_idx])
    class_names = sorted(set(labels))
    metrics = {"accuracy": round(accuracy_score([labels[i] for i in test_idx], predicted), 3),
               "macro_f1": round(f1_score([labels[i] for i in test_idx], predicted, average="macro"), 3),
               "report": classification_report([labels[i] for i in test_idx], predicted, labels=class_names, output_dict=True, zero_division=0),
               "confusion_matrix": confusion_matrix([labels[i] for i in test_idx], predicted, labels=class_names).tolist(),
               "class_order": class_names,
               "train_groups": sorted({groups[i] for i in train_idx}),
               "test_groups": sorted({groups[i] for i in test_idx}),
               "row_count": len(rows), "session_count": len(set(groups)),
               "split_method": "GroupShuffleSplit by machine_id; all sessions from a machine are held together." if args.group_by_machine else "GroupShuffleSplit by session_id; adjacent windows from one session never cross train/test."}
    normal_train = [X[i] for i in train_idx if labels[i] == "normal"]
    anomaly_model = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(),
                                  IsolationForest(n_estimators=250, contamination="auto", random_state=37))
    anomaly_model.fit(normal_train)
    artifact = {"feature_names": FEATURES, "classifier": classifier, "anomaly_model": anomaly_model,
                "metrics": metrics, "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, args.output)
    report_path = args.output.with_suffix(".evaluation.json")
    report_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps({"model": str(args.output), "evaluation": str(report_path), "accuracy": metrics["accuracy"],
                      "macro_f1": metrics["macro_f1"], "train_groups": len(metrics["train_groups"]),
                      "test_groups": len(metrics["test_groups"])}, indent=2))


if __name__ == "__main__":
    main()
