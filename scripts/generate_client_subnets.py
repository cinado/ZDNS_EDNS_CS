#!/usr/bin/env python3
"""Interactively generate domain,subnet input for zdns --client-subnet-input."""

import csv
import ipaddress
import sys


def main():
    while True:
        domain = input("Domain (e.g. example.com): ").strip()
        if domain and not any(char.isspace() or char in ",/:" for char in domain):
            break
        print("Enter a domain name without a URL scheme, path, or spaces.")

    while True:
        value = input("Subnet prefix (e.g. /24 or 24): ").strip()
        if value.startswith("/"):
            value = value[1:]
        try:
            prefix = int(value)
            if 0 <= prefix <= 32:
                break
        except ValueError:
            pass
        print("Enter an IPv4 prefix from /0 to /32.")

    while True:
        value = input("Sampling interval [1 = all prefixes, 4 = every fourth]: ").strip()
        try:
            sample_every = int(value or "1")
            if sample_every >= 1:
                break
        except ValueError:
            pass
        print("Enter a whole number of at least 1.")

    total = 1 << prefix
    count = (total + sample_every - 1) // sample_every
    address_step = (1 << (32 - prefix)) * sample_every
    print(f"Checking {count:,} of {total:,} IPv4 /{prefix} prefixes for {domain} (interval {sample_every}); keeping global unicast network addresses only.")
    output_path = input("Output CSV file [queries.csv]: ").strip() or "queries.csv"
    written = 0
    try:
        with open(output_path, "x", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, lineterminator="\n")
            for checked, address in enumerate(range(0, 1 << 32, address_step), start=1):
                ip = ipaddress.IPv4Address(address)
                if ip.is_global and not ip.is_multicast:
                    writer.writerow((domain, f"{ip}/{prefix}"))
                    written += 1
                if checked % 100000 == 0:
                    print(f"Checked {checked:,} / {count:,} prefixes; wrote {written:,} rows...", file=sys.stderr)
    except FileExistsError:
        print(f"File already exists: {output_path}. Choose a new filename on the next run.", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Could not write {output_path}: {error}. Any partial output is incomplete.", file=sys.stderr)
        return 1

    print(f"Wrote {written:,} rows to {output_path}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Any output file already created is incomplete.", file=sys.stderr)
        sys.exit(1)
