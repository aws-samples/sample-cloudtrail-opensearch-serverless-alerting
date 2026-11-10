"""Run an ad-hoc PPL query against the collection and print the rows as JSON lines.

Runs inside the VPC (the collection is VPC-only) as a one-off ECS task that reuses the detection
task definition, its read-only task role, and its environment:

    python query.py "source=`cloudtrail-logs` | where like(eventName, '%Create%') | head 10"

Exit codes: 0 = query succeeded, 1 = query failed, 2 = no query given.
"""
import json
import logging
import sys

from app import run_ppl

log = logging.getLogger("cloudtrail-query")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].strip():
        print('usage: python query.py "<PPL query>"', file=sys.stderr)
        return 2
    query = argv[1]
    try:
        rows = run_ppl(query)
    except Exception:
        log.exception("Query failed: %s", query)
        return 1
    log.info("Query returned %d row(s): %s", len(rows), query)
    for row in rows:
        print(json.dumps(row, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
