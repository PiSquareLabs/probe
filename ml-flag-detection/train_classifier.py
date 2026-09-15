"""
Train a RandomForestClassifier to predict which flagd feature flag was
active from an aggregated per-service telemetry window.

Usage:
    python train_classifier.py --data dataset.csv
"""
import argparse
import json

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import ConfusionMatrixDisplay, classification_report, confusion_matrix
from sklearn.model_selection import train_test_split


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="dataset.csv")
    ap.add_argument("--model-out", default="model.joblib")
    ap.add_argument("--report-out", default="eval_report.txt")
    ap.add_argument("--confusion-out", default="confusion_matrix.png")
    ap.add_argument("--importances-out", default="feature_importances.csv")
    ap.add_argument("--test-size", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    df = pd.read_csv(args.data)

    drop_cols = ["label", "window_start", "window_end"]
    feature_cols = [c for c in df.columns if c not in drop_cols]
    X = df[feature_cols].fillna(0.0)
    y = df["label"]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=args.test_size, random_state=args.seed, stratify=y
    )

    clf = RandomForestClassifier(
        n_estimators=300, max_depth=None, random_state=args.seed,
        class_weight="balanced", n_jobs=-1,
    )
    clf.fit(X_train, y_train)

    y_pred = clf.predict(X_test)
    labels = sorted(y.unique())
    report = classification_report(y_test, y_pred, labels=labels, zero_division=0)
    cm = confusion_matrix(y_test, y_pred, labels=labels)

    train_acc = clf.score(X_train, y_train)
    test_acc = clf.score(X_test, y_test)

    report_text = (
        f"Dataset: {args.data}\n"
        f"Rows: {len(df)}  Features: {len(feature_cols)}  Classes: {len(labels)}\n"
        f"Train/test split: {len(X_train)}/{len(X_test)} (stratified, test_size={args.test_size})\n"
        f"Train accuracy: {train_acc:.3f}\n"
        f"Test accuracy: {test_acc:.3f}\n\n"
        f"Classification report:\n{report}\n"
        f"Confusion matrix (rows=true, cols=predicted), labels={labels}:\n{cm}\n"
    )
    print(report_text)
    with open(args.report_out, "w") as f:
        f.write(report_text)

    fig, ax = plt.subplots(figsize=(12, 10))
    disp = ConfusionMatrixDisplay(confusion_matrix=cm, display_labels=labels)
    disp.plot(ax=ax, xticks_rotation=60, cmap="Blues", colorbar=False)
    plt.tight_layout()
    plt.savefig(args.confusion_out, dpi=150)
    print(f"Saved confusion matrix plot to {args.confusion_out}")

    importances = pd.Series(clf.feature_importances_, index=feature_cols).sort_values(ascending=False)
    importances.to_csv(args.importances_out, header=["importance"])
    print(f"\nTop 30 feature importances:")
    print(importances.head(30).to_string())

    joblib.dump({"model": clf, "feature_cols": feature_cols, "labels": labels}, args.model_out)
    print(f"\nSaved model to {args.model_out}")


if __name__ == "__main__":
    main()
