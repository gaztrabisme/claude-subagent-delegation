"""Score predictions against text/golden.jsonl.

Usage: python3 score.py <predictions.jsonl>

Each prediction line is a JSON object with an "id" and the four answer fields.
Missing predictions count as wrong on every field. Prints exact-match accuracy
per field and the share of golden items with all four fields correct.
Exits 0 unless the arguments or files are unusable.
"""

import json
import os
import sys

FIELDS = ["product", "issue", "order_number", "refund_requested"]
HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "golden.jsonl")


def read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                sys.exit(f"{path}:{lineno}: invalid JSON ({exc})")
    return rows


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: score.py <predictions.jsonl>")

    golden = read_jsonl(GOLDEN)
    preds = {}
    for row in read_jsonl(argv[1]):
        if "id" in row:
            preds[row["id"]] = row

    field_hits = {f: 0 for f in FIELDS}
    all_correct = 0
    missing = 0

    for gold in golden:
        pred = preds.get(gold["id"])
        if pred is None:
            missing += 1
            continue
        row_ok = True
        for f in FIELDS:
            if f in pred and pred[f] == gold[f] and type(pred[f]) is type(gold[f]):
                field_hits[f] += 1
            else:
                row_ok = False
        if row_ok:
            all_correct += 1

    n = len(golden)
    print(f"items: {n}  predictions found: {n - missing}  missing: {missing}")
    for f in FIELDS:
        print(f"{f:<16} {field_hits[f]}/{n}  {100.0 * field_hits[f] / n:5.1f}%")
    print(f"all four correct {all_correct}/{n}  {100.0 * all_correct / n:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
