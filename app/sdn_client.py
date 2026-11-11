"""
SDN-aware backup client.

Before sending, it asks the SDN controller which path the transfer will use.
The controller picks the path, pre-installs the flow rules along it, and
replies. Then the normal client.py runs unchanged, and the result is reported
back to the controller (it shows up on the dashboard).

Run it on a Mininet client host, for example:
    h1 python3 sdn_client.py /tmp/test.bin --src h1 --dst h3

Any extra options are passed straight to client.py (e.g. --corrupt 3).
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Backup server name -> IP address in the Mininet topology.
SERVER_IP = {"h3": "10.0.0.3", "h4": "10.0.0.4"}

# Address of the controller's web API as seen from a Mininet host
# (see the root-namespace link in topology/topo.py).
DEFAULT_CONTROLLER = "http://10.0.0.254:8080"

HERE = os.path.dirname(os.path.abspath(__file__))


def call(controller, endpoint, **params):
    url = "%s%s?%s" % (controller, endpoint, urllib.parse.urlencode(params))
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.load(err)
        except ValueError:
            return err.code, {"ok": False, "error": str(err)}
    except (urllib.error.URLError, OSError, ValueError) as err:
        return None, {"ok": False, "error": str(err)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("file")
    parser.add_argument("--src", required=True, help="this client host, e.g. h1")
    parser.add_argument("--dst", required=True, choices=sorted(SERVER_IP),
                        help="backup server host")
    parser.add_argument("--controller", default=DEFAULT_CONTROLLER)
    args, extra = parser.parse_known_args()

    if not os.path.isfile(args.file):
        sys.exit("File not found: %s" % args.file)

    # 1. Application event -> SDN controller
    print("Asking the SDN controller for a path %s -> %s ..." % (args.src, args.dst))
    status, reply = call(args.controller, "/api/request_path", src=args.src, dst=args.dst)
    if status == 200 and reply.get("ok"):
        print("Controller chose path: %s" % " -> ".join(reply["path"]))
        if reply.get("flows_installed"):
            print("Flow rules were pre-installed on every switch along the path.")
        elif reply.get("note"):
            print("Note: %s" % reply["note"])
    elif status is None:
        print("WARNING: controller not reachable (%s); sending without a path request."
              % reply.get("error"))
    else:
        sys.exit("Controller refused the request: %s" % reply.get("error"))

    # 2. Run the normal client (unchanged)
    command = [sys.executable, os.path.join(HERE, "client.py"), args.file,
               "--host", SERVER_IP[args.dst]] + extra
    started = time.time()
    result = subprocess.run(command)
    seconds = time.time() - started

    # 3. Tell the controller how it went
    call(args.controller, "/api/report", src=args.src, dst=args.dst,
         status="ok" if result.returncode == 0 else "failed",
         seconds="%.2f" % seconds, bytes=os.path.getsize(args.file))
    print("Reported result to controller (%.2f s)." % seconds)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
