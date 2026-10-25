#!/usr/bin/env python3
"""Enrich IPv4 addresses using CloudFront PTR airport codes and airportsdata."""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
from pathlib import Path
import socket
import sys


def enrich_address(ip, airports):
    try:
        socket.inet_pton(socket.AF_INET, ip)
    except (OSError, ValueError):
        return None, {"ip": ip, "error": "invalid_ipv4"}

    try:
        hostname, aliases, _ = socket.gethostbyaddr(ip)
    except OSError as error:
        return None, {"ip": ip, "error": "reverse_dns_failed", "detail": str(error)}

    hostnames = sorted(set([hostname, *aliases]))
    codes = set()
    for name in hostnames:
        labels = name.rstrip(".").lower().split(".")
        if len(labels) >= 5 and labels[-3:] == ["r", "cloudfront", "net"]:
            # server-<ip>.<airport><location>.r.cloudfront.net
            codes.add(labels[-4][:3].upper())
    failure = {"ip": ip, "hostnames": hostnames}
    if not codes:
        return None, {**failure, "error": "unrecognized_hostname"}
    if len(codes) != 1:
        return None, {**failure, "error": "ambiguous_airport_code", "airport_codes": sorted(codes)}

    code = next(iter(codes))
    airport = airports.get(code)
    if airport is None:
        return None, {**failure, "error": "unknown_airport_code", "airport_code": code}
    fields = ("name", "city", "country", "subd")
    if any(not airport.get(field) for field in fields):
        return None, {**failure, "error": "incomplete_airport_data", "airport_code": code}
    return {
        "airport_code": code,
        "airport_name": airport["name"],
        "city": airport["city"],
        "country": airport["country"],
        "ip": ip,
        "subdivision": airport["subd"],
    }, None


def unique_addresses(source):
    seen = set()
    for line in source:
        ip = line.strip()
        if ip and ip not in seen:
            seen.add(ip)
            yield ip


def enrich_addresses(addresses, airports, workers):
    """Keep a bounded queue so a large input cannot allocate millions of futures."""
    iterator = iter(addresses)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = set()
        exhausted = False
        while pending or not exhausted:
            while not exhausted and len(pending) < workers * 2:
                try:
                    ip = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                pending.add(executor.submit(enrich_address, ip, airports))
            if pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    yield future.result()


def positive_integer(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(
        description="Enrich IPv4 addresses with locations inferred from CloudFront PTR airport codes.",
        epilog="Uses the system DNS resolver. Results are written in completion order. "
        "Existing output files are never overwritten.",
    )
    parser.add_argument("input", type=Path, help="text file with one IPv4 address per line")
    parser.add_argument("-o", "--output", type=Path, default=Path("enriched.jsonl"))
    parser.add_argument(
        "--failures", type=Path,
        help="failure JSONL file (default: <output stem>.failures.jsonl beside the output)",
    )
    parser.add_argument(
        "--workers", type=positive_integer, default=32,
        help="concurrent reverse DNS lookups (default: 32)",
    )
    args = parser.parse_args()
    failures_path = args.failures or args.output.with_name(args.output.stem + ".failures.jsonl")
    if args.output.resolve() == failures_path.resolve():
        parser.error("output and failure files must have different paths")
    for path in (args.output, failures_path):
        if path.exists() or path.is_symlink():
            parser.error(f"output file already exists: {path}")

    try:
        import airportsdata
    except ImportError:
        print("Install the airport dataset with: python3 -m pip install -r scripts/requirements.txt", file=sys.stderr)
        return 1

    succeeded = failed = 0
    try:
        airports = airportsdata.load("IATA")
        with (
            args.input.open("r", encoding="utf-8") as source,
            args.output.open("x", encoding="utf-8") as output,
            failures_path.open("x", encoding="utf-8") as failures,
        ):
            for record, failure in enrich_addresses(unique_addresses(source), airports, args.workers):
                if record is not None:
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                    succeeded += 1
                else:
                    failures.write(json.dumps(failure, ensure_ascii=False) + "\n")
                    failed += 1
                if (succeeded + failed) % 1000 == 0:
                    print(f"Processed {succeeded + failed:,} IPs: {succeeded:,} enriched, {failed:,} failed.", file=sys.stderr)
    except (OSError, ValueError) as error:
        print(f"Error: {error}. Any output files already created may be incomplete.", file=sys.stderr)
        return 1

    print(f"Wrote {succeeded:,} enriched IPs to {args.output}; {failed:,} failures to {failures_path}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled. Any output files already created may be incomplete.", file=sys.stderr)
        sys.exit(1)
