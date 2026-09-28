"""
name: convert_data
description: Convert data between CSV, TSV, JSON, JSON Lines and YAML (formats taken from the file extensions)
params: INPUT OUTPUT
safety: changes
"""
import csv
import json
import os
import sys


def read(path: str):
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding="utf-8-sig", newline="") as f:
        if ext in (".csv", ".tsv"):
            return list(csv.DictReader(f, delimiter="\t" if ext == ".tsv" else ","))
        if ext == ".jsonl":
            return [json.loads(line) for line in f if line.strip()]
        if ext in (".yml", ".yaml"):
            import yaml
            return yaml.safe_load(f)
        return json.load(f)


def write(path: str, rows) -> None:
    ext = os.path.splitext(path)[1].lower()
    if os.path.exists(path):
        raise SystemExit(f"{path} already exists")
    with open(path, "w", encoding="utf-8", newline="") as f:
        if ext in (".csv", ".tsv"):
            rows = rows if isinstance(rows, list) else [rows]
            fields = list(dict.fromkeys(k for r in rows for k in r))
            w = csv.DictWriter(f, fieldnames=fields, delimiter="\t" if ext == ".tsv" else ",")
            w.writeheader()
            for r in rows:
                w.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in r.items()})
        elif ext == ".jsonl":
            for r in rows if isinstance(rows, list) else [rows]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        elif ext in (".yml", ".yaml"):
            import yaml
            yaml.safe_dump(rows, f, sort_keys=False, allow_unicode=True)
        else:
            json.dump(rows, f, indent=2, ensure_ascii=False)
            f.write("\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__.strip())
        sys.exit(2)
    if not os.path.isfile(sys.argv[1]):
        sys.exit(f"No such file: {sys.argv[1]}")
    data = read(sys.argv[1])
    write(sys.argv[2], data)
    print(f"Wrote {sys.argv[2]} ({len(data) if isinstance(data, list) else 1} record(s)).")
