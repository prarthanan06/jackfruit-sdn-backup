import argparse
import json
import os
import socket
import subprocess
import sys


def server_available(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Replicate a file to multiple backup servers"
    )
    parser.add_argument("file")
    parser.add_argument("--servers", required=True)
    parser.add_argument("--transfer-id", required=True)

    args = parser.parse_args()

    with open(args.servers, "r", encoding="utf-8") as file_obj:
        servers = json.load(file_obj)

    client_path = os.path.join(
        os.path.dirname(__file__),
        "client.py",
    )

    successful = 0

    for server in servers:
        name = server["name"]
        host = server["host"]
        port = server["port"]

        print(f"\nChecking {name} at {host}:{port}")

        if not server_available(host, port):
            print(f"{name}: unavailable")
            continue

        server_transfer_id = f"{args.transfer_id}-{name}"

        command = [
            sys.executable,
            client_path,
            args.file,
            "--host",
            host,
            "--port",
            str(port),
            "--transfer-id",
            server_transfer_id,
        ]

        result = subprocess.run(command)

        if result.returncode == 0:
            print(f"{name}: successful")
            successful += 1
        else:
            print(f"{name}: failed")

    total = len(servers)

    print(f"\nBackup result: {successful}/{total} servers successful")

    if successful == total:
        return 0

    if successful > 0:
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
