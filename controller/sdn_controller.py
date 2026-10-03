from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.topology import event
from ryu.topology.api import get_switch, get_link
import networkx as nx

FLOW_COOKIE = 0x1
FLOW_PRIORITY = 10
IDLE_TIMEOUT = 30


class SDNBackupController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dps = {}          # dpid -> datapath
        self.hosts = {}        # mac -> (dpid, port)
        self.net = nx.Graph()  # switch graph
        self.tree = nx.Graph() # spanning tree, used for loop-free flooding
        self.link_ports = {}   # dpid -> ports that connect to other switches
        self.edge_ports = {}   # dpid -> ports that face hosts

    # ---------- switch connect ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.dps[dp.id] = dp
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, match, actions, cookie=0, idle_timeout=0)
        self.logger.info("Switch s%d connected", dp.id)

    def add_flow(self, dp, priority, match, actions, cookie=FLOW_COOKIE, idle_timeout=IDLE_TIMEOUT):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                instructions=inst, cookie=cookie,
                                idle_timeout=idle_timeout)
        dp.send_msg(mod)

    # ---------- topology ----------
    @set_ev_cls([event.EventSwitchEnter, event.EventSwitchLeave,
                 event.EventLinkAdd, event.EventLinkDelete])
    def topology_changed(self, ev):
        if isinstance(ev, event.EventSwitchLeave):
            self.dps.pop(ev.switch.dp.id, None)
        self.rebuild_topology()
        if isinstance(ev, (event.EventLinkDelete, event.EventSwitchLeave)):
            self.logger.info("Link/switch removed -> flushing path flows")
            self.flush_flows()

    def rebuild_topology(self):
        net = nx.Graph()
        link_ports, all_ports = {}, {}
        for sw in get_switch(self, None):
            d = sw.dp.id
            net.add_node(d)
            all_ports[d] = {p.port_no for p in sw.ports
                            if p.port_no < ofproto_v1_3.OFPP_MAX}
            link_ports[d] = set()
        for l in get_link(self, None):
            a, b = l.src.dpid, l.dst.dpid
            net.add_edge(a, b)
            ports = net[a][b].setdefault("ports", {})
            ports[a] = l.src.port_no
            ports[b] = l.dst.port_no
            link_ports.setdefault(a, set()).add(l.src.port_no)
            link_ports.setdefault(b, set()).add(l.dst.port_no)
        self.net = net
        self.tree = nx.minimum_spanning_tree(net)
        self.link_ports = link_ports
        self.edge_ports = {d: all_ports.get(d, set()) - link_ports.get(d, set())
                           for d in net.nodes}
        # forget hosts that were wrongly learned on inter-switch ports
        self.hosts = {m: loc for m, loc in self.hosts.items()
                      if loc[1] not in link_ports.get(loc[0], set())}
        self.logger.info("Topology: %d switches, %d links",
                         net.number_of_nodes(), net.number_of_edges())

    def flush_flows(self):
        for dp in self.dps.values():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            mod = parser.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE,
                                    out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                    cookie=FLOW_COOKIE, cookie_mask=0xffffffffffffffff,
                                    match=parser.OFPMatch())
            dp.send_msg(mod)

    # ---------- packets ----------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match["in_port"]
        eth = packet.Packet(msg.data).get_protocols(ethernet.ethernet)[0]

        if eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return
        src, dst = eth.src, eth.dst

        # learn host location (only on host-facing ports)
        if in_port not in self.link_ports.get(dpid, set()):
            if self.hosts.get(src) != (dpid, in_port):
                self.hosts[src] = (dpid, in_port)
                self.logger.info("Host %s is at s%d port %d", src, dpid, in_port)

        is_multicast = int(dst.split(":")[0], 16) & 1
        if is_multicast or dst not in self.hosts:
            self.flood(dp, in_port, msg)
            return

        dst_dpid, dst_port = self.hosts[dst]
        try:
            path = nx.shortest_path(self.net, dpid, dst_dpid)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            self.logger.warning("No path s%d -> s%d, dropping", dpid, dst_dpid)
            return

        self.logger.info("PATH %s -> %s : %s", src, dst,
                         " -> ".join("s%d" % d for d in path))
        self.install_path(path, src, dst, dst_port)
        if src in self.hosts and self.hosts[src][0] == dpid:
            self.install_path(list(reversed(path)), dst, src, self.hosts[src][1])

        out_port = dst_port if len(path) == 1 else self.net[dpid][path[1]]["ports"][dpid]
        self.send_out(dp, msg, in_port, [out_port])

    def install_path(self, path, src, dst, last_port):
        for i, d in enumerate(path):
            dp = self.dps.get(d)
            if dp is None:
                continue
            if i == len(path) - 1:
                out = last_port
            else:
                out = self.net[d][path[i + 1]]["ports"][d]
            parser = dp.ofproto_parser
            match = parser.OFPMatch(eth_src=src, eth_dst=dst)
            self.add_flow(dp, FLOW_PRIORITY, match, [parser.OFPActionOutput(out)])

    def flood(self, dp, in_port, msg):
        dpid = dp.id
        ports = set(self.edge_ports.get(dpid, set()))
        if dpid in self.tree:
            for n in self.tree[dpid]:
                ports.add(self.net[dpid][n]["ports"][dpid])
        ports.discard(in_port)
        self.send_out(dp, msg, in_port, sorted(ports))

    def send_out(self, dp, msg, in_port, ports):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        actions = [parser.OFPActionOutput(p) for p in ports]
        out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)
