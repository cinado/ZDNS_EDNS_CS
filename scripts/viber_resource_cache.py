#!/usr/bin/env python3
"""Probe Viber media resource cache hits across known CloudFront edge IPs."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO
from urllib.parse import urlencode


HOST = "media.cdn.viber.com"
DLID_PREFIX = "0-03-05-"
RESOURCE_PATH = "/media/user_photo"

DEFAULT_QUERY_PARAMS = {
    "fltp": "jpg",
    "rqvr": "1",
    "sdcc": "43",
    "styp": "95",
    "udid": "9fc39a6d473127668f8126e5646414f4d07ee8ff",
    "vcpv": "51",
    "xuat": "5AABA9DB62538829",
    "xuav": "27.7.0.6-c49ffa7",
}

DEFAULT_HEADERS = {
    "accept-encoding": "deflate, gzip, br, zstd",
    "accept": "*/*",
}

'''
Removed default headers
    "cache-control": "only-if-cached, max-stale=2147483647",
    "x-client-phone-cc": "43",
    "x-client-os": "18.4.1",
    "x-client-version": "27.7.0.2",
    "x-client-device": "iPhone12,8",
    "x-client-language": "EN",
    "x-client-type": "ios",
    "x-request-id": "1711A538-2414-4EAF-AB06-7AA46EBEDB20",
    "accept-language": "en-GB,en;q=0.9",
'''

DEFAULT_USER_AGENT = "Viber/27.7.0.2 CFNetwork/3826.500.111.2.2 Darwin/24.4.0"

DEFAULT_INPUT = Path("enriched.jsonl")
DEFAULT_SENDERS = 10000
HEADER_DELIMITER = "|"


@dataclass(frozen=True)
class ResourceCacheConfig:
    did: str
    input: Path
    output: Path | None
    senders: int


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


def parse_args(argv: list[str] | None = None) -> ResourceCacheConfig:
    parser = argparse.ArgumentParser(
        description="Find CloudFront locations where a Viber media resource is cached.",
    )
    parser.add_argument("did", help="bare Viber resource id without the 0-03-05- prefix")
    parser.add_argument(
        "--input", type=Path, default=DEFAULT_INPUT,
        help="enriched JSONL file from enrich_ipv4.py (default: enriched.jsonl)",
    )
    parser.add_argument(
        "--output", type=Path,
        help="JSON report path (default: timestamped resource_cache file beside the input)",
    )
    parser.add_argument(
        "--senders",
        type=int,
        default=DEFAULT_SENDERS,
        help=f"zgrab2 worker count, defaults to {DEFAULT_SENDERS}.",
    )
    args = parser.parse_args(argv)
    return ResourceCacheConfig(
        did=args.did,
        input=args.input,
        output=args.output,
        senders=args.senders,
    )


def run(config: ResourceCacheConfig) -> int:
    did = normalize_did(config.did)
    senders = require_positive_int(config.senders, "--senders")
    input_path = resolve_path(config.input)
    try:
        records = read_enriched_records(input_path)
    except (OSError, UnicodeError) as error:
        raise SystemExit(f"cannot read {input_path}: {error}") from error
    dlid = DLID_PREFIX + did
    checked_at = utc_timestamp()
    output_path = resolve_output_path(
        config.output or default_output_path(input_path.parent, did, checked_at),
    )
    if output_path.exists() or output_path.is_symlink():
        raise SystemExit(f"output file already exists: {output_path}")

    ensure_binary("zgrab2")
    command = build_zgrab_command(
        build_endpoint(dlid),
        senders,
    )
    hits = run_zgrab(command, records)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        output_path,
        {
            "did": did,
            "dlid": dlid,
            "input_file": str(input_path),
            "checked_at": checked_at,
            "total_ips": len(records),
            "hit_count": len(hits),
            "locations": hits,
        },
    )
    print(f"wrote {len(hits)} cache hit locations to {output_path}")
    return 0


def normalize_did(value: str) -> str:
    did = value.strip()
    if not did:
        raise SystemExit("DID must not be empty")
    if did.startswith(DLID_PREFIX):
        raise SystemExit(f"pass the bare DID without the {DLID_PREFIX} prefix")
    return did


def read_enriched_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSON on {path}:{line_number}") from exc
            if not isinstance(record, dict):
                raise SystemExit(f"non-object JSON record on {path}:{line_number}")
            if not record.get("ip"):
                raise SystemExit(f"missing ip field on {path}:{line_number}")
            ip = record["ip"]
            try:
                if not isinstance(ip, str):
                    raise ValueError("expected an IPv4 string")
                socket.inet_pton(socket.AF_INET, ip)
            except (OSError, ValueError) as exc:
                raise SystemExit(f"invalid IPv4 address on {path}:{line_number}") from exc
            if "error" in record:
                raise SystemExit(f"failure record on {path}:{line_number}; use the enriched output, not its failures file")
            if ip in seen:
                continue
            seen.add(ip)
            records.append(record)

    if not records:
        raise SystemExit(f"input result file has no records: {path}")
    return records


def build_endpoint(dlid: str) -> str:
    params = {"dlid": dlid, **DEFAULT_QUERY_PARAMS}
    return RESOURCE_PATH + "?" + urlencode(params)


def build_zgrab_command(
    endpoint: str,
    senders: int,
) -> list[str]:
    command = ["zgrab2", "--senders", str(senders)]
    command.extend(
        [
            "http",
            "--use-https",
            "--port",
            "443",
            "--method",
            "HEAD",
            "--user-agent",
            DEFAULT_USER_AGENT,
            "--endpoint",
            endpoint,
            "--max-redirects",
            "0",
            "--verify-server-certificate",
            "--custom-headers-names",
            HEADER_DELIMITER.join(DEFAULT_HEADERS.keys()),
            "--custom-headers-values",
            HEADER_DELIMITER.join(DEFAULT_HEADERS.values()),
            "--custom-headers-delimiter",
            HEADER_DELIMITER,
        ],
    )
    return command


def run_zgrab(
    command: list[str],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    writer = threading.Thread(
        target=write_targets,
        args=(process.stdin, records),
        daemon=True,
    )
    writer.start()

    hits: list[dict[str, Any]] = []
    records_by_ip = {str(record["ip"]): record for record in records}

    assert process.stdout is not None
    for line in process.stdout:
        zgrab_record = parse_json_line(line)
        if zgrab_record is None:
            continue
        ip = str(zgrab_record.get("ip") or "")
        source_record = records_by_ip.get(ip)
        if source_record is None or not is_cache_hit(zgrab_record):
            continue

        hit = location_hit(source_record)
        hits.append(hit)
        print_hit(hit)

    writer.join()
    return_code = process.wait()
    if return_code != 0:
        raise SystemExit(f"zgrab2 exited with status {return_code}")

    return sorted(hits, key=lambda item: item["ip"])


def write_targets(stdin: TextIO | None, records: Iterable[dict[str, Any]]) -> None:
    if stdin is None:
        return

    try:
        for record in records:
            stdin.write(f"{record['ip']},{HOST},,443\n")
    except BrokenPipeError:
        pass
    finally:
        try:
            stdin.close()
        except BrokenPipeError:
            pass


def is_cache_hit(record: dict[str, Any]) -> bool:
    response = record
    for key in ("data", "http", "result", "response"):
        if not isinstance(response, dict):
            return False
        response = response.get(key)
    if not isinstance(response, dict):
        return False

    headers = normalize_headers(response.get("headers", {}))
    return "hit from cloudfront" in headers.get("x-cache", "").lower()


def normalize_headers(raw_headers: Any) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if not isinstance(raw_headers, dict):
        return normalized

    for key, value in raw_headers.items():
        name = str(key).strip().lower().replace("_", "-")
        if name == "unknown" and isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("key"):
                    normalized[str(item["key"]).strip().lower().replace("_", "-")] = (
                        flatten_header_value(item.get("value"))
                    )
            continue
        normalized[name] = flatten_header_value(value)
    return normalized


def flatten_header_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def location_hit(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "airport_code": record.get("airport_code"),
        "airport_name": record.get("airport_name"),
        "country": record.get("country"),
        "subdivision": record.get("subdivision"),
        "city": record.get("city"),
        "ip": record.get("ip"),
    }


def print_hit(hit: dict[str, Any]) -> None:
    print(
        "Hit from "
        f"{display_value(hit.get('airport_code'))}, "
        f"{display_value(hit.get('country'))}, "
        f"{display_value(hit.get('subdivision'))}, "
        f"{display_value(hit.get('city'))}, "
        f"IP-Address: {display_value(hit.get('ip'))}",
    )


def display_value(value: Any) -> str:
    return str(value) if value else "unknown"


def parse_json_line(line: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def default_output_path(results_dir: Path, did: str, timestamp: str) -> Path:
    safe_did = re.sub(r"[^A-Za-z0-9_.-]+", "_", did)
    return resolve_path(results_dir) / f"resource_cache_{safe_did}_{timestamp}.json"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, document: dict[str, Any]) -> None:
    temp_path = path.with_name(path.name + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temp_path.replace(path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


def ensure_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"missing required binary on PATH: {name}")
    return path


def require_positive_int(value: int, name: str) -> int:
    if value < 1:
        raise SystemExit(f"{name} must be >= 1")
    return value


def resolve_output_path(path: Path) -> Path:
    return resolve_path(path)


def resolve_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()


if __name__ == "__main__":
    raise SystemExit(main())
