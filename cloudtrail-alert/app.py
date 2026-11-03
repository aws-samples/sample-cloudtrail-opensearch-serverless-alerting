"""Hourly CloudTrail detection task for Amazon OpenSearch Serverless.

Runs a PPL query against the collection, and publishes an Amazon SNS alert when it returns rows.
Exit codes: 0 = success (alert sent or nothing found), 1 = query or publish failed.
"""
import json
import logging
import os
import sys

import boto3
import requests
from requests_aws4auth import AWS4Auth

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cloudtrail-alert")

ENDPOINT = os.environ["COLLECTION_ENDPOINT"].rstrip("/")
INDEX = os.environ.get("INDEX", "cloudtrail-logs")
TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
LOOKBACK = os.environ.get("LOOKBACK", "1 HOUR")
MAX_EVENTS_IN_MESSAGE = int(os.environ.get("MAX_EVENTS_IN_MESSAGE", "25"))

PPL_QUERY = (
    f"source=`{INDEX}` "
    '| where like(eventName, "%Create%") '
    f"| where @timestamp > DATE_SUB(NOW(), INTERVAL {LOOKBACK}) "
    "| fields eventTime, eventName, eventSource, awsRegion, userIdentity.type, userIdentity.arn "
    "| sort - eventTime"
)


def signer() -> AWS4Auth:
    """SigV4 auth for the OpenSearch Serverless data plane ("aoss" service).

    Uses the task role's temporary credentials, including the session token.
    """
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    return AWS4Auth(creds.access_key, creds.secret_key, REGION, "aoss", session_token=creds.token)


def run_ppl(query: str) -> list[dict]:
    resp = requests.post(
        f"{ENDPOINT}/_plugins/_ppl",
        auth=signer(),
        json={"query": query},
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"PPL query failed: HTTP {resp.status_code}: {resp.text[:500]}")
    body = resp.json()
    columns = [c["name"] for c in body.get("schema", [])]
    return [dict(zip(columns, row)) for row in body.get("datarows", [])]


def format_message(events: list[dict]) -> tuple[str, str]:
    count = len(events)
    subject = f"[CloudTrail] {count} Create event(s) in the last {LOOKBACK.lower()}"[:100]
    lines = [
        f"{count} CloudTrail Create* event(s) found in index '{INDEX}' in the last {LOOKBACK.lower()}.",
        "",
    ]
    for e in events[:MAX_EVENTS_IN_MESSAGE]:
        lines.append(
            f"- {e.get('eventTime')}  {e.get('eventName')}  ({e.get('eventSource')}, {e.get('awsRegion')})  "
            f"by {e.get('userIdentity.arn') or e.get('userIdentity.type')}"
        )
    if count > MAX_EVENTS_IN_MESSAGE:
        lines.append(f"... and {count - MAX_EVENTS_IN_MESSAGE} more. Query the collection for the full list.")
    return subject, "\n".join(lines)


def main() -> int:
    try:
        events = run_ppl(PPL_QUERY)
        log.info("Query returned %d event(s)", len(events))
        if not events:
            return 0
        subject, message = format_message(events)
        boto3.client("sns", region_name=REGION).publish(TopicArn=TOPIC_ARN, Subject=subject, Message=message)
        log.info("Published alert to %s", TOPIC_ARN)
        return 0
    except Exception:  # noqa: BLE001 - log and signal failure through the exit code
        log.exception("Detection run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
