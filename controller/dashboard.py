"""
Live network dashboard for the SDN backup project.

Runs as a second Ryu app next to sdn_controller.py. It does not change the
controller. Once per second it reads the controller's current topology graph,
works out which switches/links are up, which path each client->server pair is
using, and which alternate paths exist, then serves that as:

    http://localhost:8080/            the dashboard page
    http://localhost:8080/api/status  the same data as JSON

Start both apps together:
    ryu-manager --observe-links --ofp-tcp-listen-port 6653 \
        sdn_controller.py dashboard.py
"""

import itertools
import json
import os
import time
from collections import deque

import networkx as nx
from webob import Response

from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.lib import hub

# ---------------------------------------------------------------------------
# Describe the network here. Keep this in sync with topology/topo.py.
# ---------------------------------------------------------------------------
CONTROLLER_APP_NAME = "SDNBackupController"

SWITCHES = [1, 2, 3, 4, 5]
LINKS = [(1, 2), (1, 3), (2, 4), (2, 5), (3, 4), (3, 5)]

# host name -> (switch it is attached to, role)
HOSTS = {
    "h1": (1, "client"),
    "h2": (1, "client"),
    "h3": (4, "server"),
    "h4": (5, "server"),
}

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
MAX_EVENTS = 60
MAX_ALTERNATES = 2


def _key(a, b):
    return (a, b) if a <= b else (b, a)


def _pname(path):
    return "->".join("s%d" % d for d in path)


class DashboardState:
    """Pure logic (no Ryu inside) so it is easy to test."""

    def __init__(self):
        self.switches = set(SWITCHES)
        self.links = {_key(a, b) for a, b in LINKS}
        self.live_nodes = set()
        self.live_edges = set()
        self.graph = nx.Graph()
        self.seen_up_switches = set()
        self.seen_up_links = set()
        self.baseline = {}      # (client, server) -> path when network was healthy
        self.last_path = {}     # (client, server) -> last displayed path (or None)
        self.events = deque(maxlen=MAX_EVENTS)
        self.has_data = False

    # -- events -------------------------------------------------------------
    def log(self, level, msg):
        self.events.appendleft({"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})

    # -- helpers ------------------------------------------------------------
    def pairs(self):
        clients = [h for h, (_, r) in HOSTS.items() if r == "client"]
        servers = [h for h, (_, r) in HOSTS.items() if r == "server"]
        return list(itertools.product(clients, servers))

    def _shortest(self, graph, src, dst):
        try:
            return nx.shortest_path(graph, src, dst, weight="weight")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def _path_intact(self, path):
        if path is None:
            return False
        if any(d not in self.live_nodes for d in path):
            return False
        return all(_key(a, b) in self.live_edges for a, b in zip(path, path[1:]))

    # -- update (called once per second) ------------------------------------
    def update(self, graph):
        """graph: copy of the controller's live networkx graph."""
        self.graph = graph
        live_nodes = set(graph.nodes)
        live_edges = {_key(a, b) for a, b in graph.edges}
        self.links |= live_edges
        self.switches |= live_nodes

        for dpid in sorted(self.switches):
            was_up = dpid in self.live_nodes
            now_up = dpid in live_nodes
            if now_up and dpid not in self.seen_up_switches:
                self.seen_up_switches.add(dpid)
                self.log("ok", "Switch s%d is up" % dpid)
            elif was_up and not now_up:
                self.log("bad", "Switch s%d went DOWN" % dpid)
            elif now_up and not was_up:
                self.log("ok", "Switch s%d is back UP" % dpid)

        for a, b in sorted(self.links):
            was_up = (a, b) in self.live_edges
            now_up = (a, b) in live_edges
            if now_up and (a, b) not in self.seen_up_links:
                self.seen_up_links.add((a, b))
                self.log("ok", "Link s%d-s%d is up" % (a, b))
            elif was_up and not now_up:
                self.log("bad", "Link s%d-s%d went DOWN" % (a, b))
            elif now_up and not was_up:
                self.log("ok", "Link s%d-s%d is back UP" % (a, b))

        self.live_nodes = live_nodes
        self.live_edges = live_edges
        self.has_data = True

        healthy = live_nodes >= set(SWITCHES) and live_edges >= {_key(a, b) for a, b in LINKS}
        unit_graph = nx.Graph()
        for a, b in LINKS:
            unit_graph.add_edge(a, b, weight=1)

        for client, server in self.pairs():
            src, dst = HOSTS[client][0], HOSTS[server][0]
            current = self._shortest(graph, src, dst)
            pair = (client, server)

            if healthy and current is not None:
                self.baseline[pair] = current
            elif pair not in self.baseline:
                self.baseline[pair] = self._shortest(unit_graph, src, dst)

            base = self.baseline[pair]
            # Equal-cost ties are cosmetic: keep showing the baseline while it works.
            if (current is not None and self._path_intact(base)
                    and len(base) == len(current)):
                current = base

            if pair in self.last_path and self.last_path[pair] != current:
                label = "%s -> %s" % (client, server)
                if current is None:
                    self.log("bad", "%s: NO PATH available" % label)
                else:
                    old = self.last_path[pair]
                    if old is None:
                        self.log("ok", "%s: path restored: %s" % (label, _pname(current)))
                    else:
                        self.log("warn", "%s path changed: %s => %s"
                                 % (label, _pname(old), _pname(current)))
            self.last_path[pair] = current

    # -- snapshot for the API -----------------------------------------------
    def snapshot(self):
        switches = [{"id": d, "name": "s%d" % d, "up": d in self.live_nodes}
                    for d in sorted(self.switches)]
        links = [{"a": a, "b": b, "up": (a, b) in self.live_edges}
                 for a, b in sorted(self.links)]
        hosts = [{"name": h, "role": r, "switch": s, "up": s in self.live_nodes}
                 for h, (s, r) in sorted(HOSTS.items())]

        paths = []
        for client, server in self.pairs():
            pair = (client, server)
            src, dst = HOSTS[client][0], HOSTS[server][0]
            current = self.last_path.get(pair)
            base = self.baseline.get(pair)
            if current is None:
                status = "NO PATH"
            elif base is not None and not self._path_intact(base):
                status = "REROUTED"
            else:
                status = "NORMAL"

            alternates = []
            if current is not None and src in self.graph and dst in self.graph:
                try:
                    for p in itertools.islice(
                            nx.shortest_simple_paths(self.graph, src, dst, weight="weight"),
                            MAX_ALTERNATES + 1):
                        # skip long detours: at most one hop longer than the path in use
                        if p != current and len(p) <= len(current) + 1:
                            alternates.append(p)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    pass
            alternates = alternates[:MAX_ALTERNATES]

            paths.append({
                "client": client, "server": server, "status": status,
                "current": current, "normal": base, "alternates": alternates,
            })

        return {
            "time": time.strftime("%H:%M:%S"),
            "connected": self.has_data,
            "summary": {
                "switches_up": sum(1 for s in switches if s["up"]),
                "switches_total": len(switches),
                "links_up": sum(1 for l in links if l["up"]),
                "links_total": len(links),
            },
            "switches": switches, "links": links, "hosts": hosts,
            "paths": paths, "events": list(self.events),
        }


class DashboardApp(app_manager.RyuApp):
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = DashboardState()
        wsgi = kwargs["wsgi"]
        wsgi.register(DashboardWeb, {"app": self})
        hub.spawn(self._loop)
        self.logger.info("Dashboard on http://localhost:8080/")

    def _loop(self):
        while True:
            try:
                ctrl = app_manager.lookup_service_brick(CONTROLLER_APP_NAME)
                if ctrl is not None:
                    self.state.update(ctrl.net.copy())
            except Exception:
                self.logger.exception("dashboard update failed")
            hub.sleep(1)


class DashboardWeb(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data["app"]

    @route("index", "/", methods=["GET"])
    def index(self, req, **kwargs):
        with open(HTML_PATH, "rb") as f:
            body = f.read()
        return Response(content_type="text/html", charset="utf-8", body=body,
                        headers={"Cache-Control": "no-store"})

    @route("status", "/api/status", methods=["GET"])
    def status(self, req, **kwargs):
        body = json.dumps(self.app.state.snapshot()).encode("utf-8")
        return Response(content_type="application/json", body=body,
                        headers={"Cache-Control": "no-store"})
