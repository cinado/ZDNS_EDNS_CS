#!/usr/bin/env python3
"""Extract unique IPv4 A answers from successful ZDNS JSON Lines results."""

import argparse
import json
from pathlib import Path
import socket
import sys


def collect_addresses(record, addresses):
    """Collect answer-section A records, checking status separately per module."""
    if not isinstance(record, dict) or not isinstance(record.get("results"), dict):
        raise ValueError("expected a ZDNS record with a 'results' object")

    for module in record["results"].values():
        if not isinstance(module, dict):
            raise ValueError("expected a module result object")
        if module.get("status") != "NOERROR":
            continue
        data = module.get("data")
        if not isinstance(data, dict):
            continue
        answers = data.get("answers", [])
        if not isinstance(answers, list):
            raise ValueError("expected an 'answers' array")
        for answer in answers:
            if not isinstance(answer, dict):
                raise ValueError("expected an answer object")
            if answer.get("type") != "A":
                continue
            address = answer.get("answer")
            if not isinstance(address, str):
                raise ValueError("expected an IPv4 string in an A record's 'answer'")
            if address in addresses:
                continue
            # Validate only new addresses, using the native IPv4 parser.
            try:
                socket.inet_pton(socket.AF_INET, address)
            except (OSError, ValueError) as error:
                raise ValueError(f"invalid IPv4 A answer: {address!r}") from error
            addresses.add(address)


def extract_addresses(paths):
    addresses = set()
    for path in paths:
        with path.open("r", encoding="utf-8", buffering=1024 * 1024) as source:
            for line_number, line in enumerate(source, start=1):
                if line.isspace():
                    continue
                try:
                    collect_addresses(json.loads(line), addresses)
                except ValueError as error:
                    raise ValueError(f"{path}:{line_number}: {error}") from error
    return addresses


def main():
    parser = argparse.ArgumentParser(
        description="Extract unique IPv4 A answers from NOERROR ZDNS module results.",
        epilog="Inputs must be JSON Lines (one ZDNS result per line), as produced by zdns. "
        "Output is unsorted. Existing output files are not overwritten.",
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="ZDNS .json or .jsonl files")
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("unique_ipv4.txt"),
        help="output file (default: unique_ipv4.txt)",
    )
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error(f"output file already exists: {args.output}")

    try:
        addresses = extract_addresses(args.inputs)
        # Delay output creation until all input has been successfully read.
        with args.output.open("x", encoding="ascii", buffering=1024 * 1024, newline="\n") as output:
            output.writelines(address + "\n" for address in addresses)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"Wrote {len(addresses):,} unique IPv4 addresses to {args.output}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled. Any output file already created may be incomplete.", file=sys.stderr)
        sys.exit(1)
