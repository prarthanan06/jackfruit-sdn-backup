from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import (
    packet,
    ethernet,
    ether_types,
)
from ryu.topology import event
from ryu.topology.api import get_switch, get_link
import networkx as nx
import os


FLOW_COOKIE = 0x1
FLOW_PRIORITY = 10
IDLE_TIMEOUT = 30

REROUTE_ON_FAILURE = os.getenv(
    "SDN_REROUTE",
    "1",
).lower() not in {"0", "false", "no"}


class SDNBackupController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Datapath objects indexed by switch DPID.
        self.dps = {}

        # Learned host locations:
        # MAC address -> (switch DPID, switch port)
        self.hosts = {}

        # Graph containing only switches and switch-to-switch links.
        self.net = nx.Graph()

        # Spanning tree used for loop-free flooding.
        self.tree = nx.Graph()

        # Switch-to-switch ports:
        # switch DPID -> set of port numbers
        self.link_ports = {}

        # Host-facing ports:
        # switch DPID -> set of port numbers
        self.edge_ports = {}

    # ------------------------------------------------------------
    # Switch connection
    # ------------------------------------------------------------

    @set_ev_cls(
        ofp_event.EventOFPSwitchFeatures,
        CONFIG_DISPATCHER,
    )
    def switch_features(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        self.dps[dp.id] = dp

        # Send unknown packets to the controller.
        match = parser.OFPMatch()

        actions = [
            parser.OFPActionOutput(
                ofp.OFPP_CONTROLLER,
                ofp.OFPCML_NO_BUFFER,
            )
        ]

        self.add_flow(
            dp,
            priority=0,
            match=match,
            actions=actions,
            cookie=0,
            idle_timeout=0,
        )

        self.logger.info(
            "Switch s%d connected",
            dp.id,
        )

    def add_flow(
        self,
        dp,
        priority,
        match,
        actions,
        cookie=FLOW_COOKIE,
        idle_timeout=IDLE_TIMEOUT,
    ):
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        instructions = [
            parser.OFPInstructionActions(
                ofp.OFPIT_APPLY_ACTIONS,
                actions,
            )
        ]

        flow_mod = parser.OFPFlowMod(
            datapath=dp,
            priority=priority,
            match=match,
            instructions=instructions,
            cookie=cookie,
            idle_timeout=idle_timeout,
        )

        dp.send_msg(flow_mod)

    # ------------------------------------------------------------
    # Topology discovery and changes
    # ------------------------------------------------------------

    @set_ev_cls(
        [
            event.EventSwitchEnter,
            event.EventSwitchLeave,
            event.EventLinkAdd,
            event.EventLinkDelete,
        ]
    )
    def topology_changed(self, ev):
        if isinstance(ev, event.EventSwitchLeave):
            self.dps.pop(ev.switch.dp.id, None)

        self.rebuild_topology()

        if isinstance(ev, event.EventLinkDelete):
            self.logger.warning("LINK_FAILURE detected")

            if REROUTE_ON_FAILURE:
                self.logger.warning(
                    "REROUTING enabled; flushing path flows"
                )
                self.flush_flows()
            else:
                self.logger.warning(
                    "REROUTING disabled; baseline mode"
                )

        elif isinstance(ev, event.EventSwitchLeave):
            self.logger.warning(
                "SWITCH_FAILURE detected; "
                "flushing path flows"
            )
            self.flush_flows()

    def rebuild_topology(self):
        net = nx.Graph()

        link_ports = {}
        all_ports = {}

        # Add all discovered switches.
        for switch in get_switch(self, None):
            dpid = switch.dp.id

            net.add_node(dpid)

            all_ports[dpid] = {
                port.port_no
                for port in switch.ports
                if port.port_no < ofproto_v1_3.OFPP_MAX
            }

            link_ports[dpid] = set()

        # Add switch-to-switch links.
        for link in get_link(self, None):
            source_dpid = link.src.dpid
            destination_dpid = link.dst.dpid

            net.add_edge(
                source_dpid,
                destination_dpid,
                weight=1,
                delay_ms=1,
                bandwidth_mbps=1000,
            )

            ports = net[source_dpid][destination_dpid].setdefault(
                "ports",
                {},
            )

            ports[source_dpid] = link.src.port_no
            ports[destination_dpid] = link.dst.port_no

            link_ports.setdefault(
                source_dpid,
                set(),
            ).add(link.src.port_no)

            link_ports.setdefault(
                destination_dpid,
                set(),
            ).add(link.dst.port_no)

        self.net = net

        # Build a loop-free spanning tree for flooding.
        self.tree = nx.minimum_spanning_tree(net)

        self.link_ports = link_ports

        self.edge_ports = {
            dpid: all_ports.get(dpid, set())
            - link_ports.get(dpid, set())
            for dpid in net.nodes
        }

        # Remove host locations that are no longer host-facing.
        self.hosts = {
            mac: location
            for mac, location in self.hosts.items()
            if location[1]
            not in link_ports.get(location[0], set())
        }

        self.logger.info(
            "Topology: %d switches, %d links",
            net.number_of_nodes(),
            net.number_of_edges(),
        )

    def flush_flows(self):
        for dp in self.dps.values():
            ofp = dp.ofproto
            parser = dp.ofproto_parser

            flow_mod = parser.OFPFlowMod(
                datapath=dp,
                command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY,
                cookie=FLOW_COOKIE,
                cookie_mask=0xFFFFFFFFFFFFFFFF,
                match=parser.OFPMatch(),
            )

            dp.send_msg(flow_mod)

    # ------------------------------------------------------------
    # Path selection
    # ------------------------------------------------------------

    def choose_path(self, source_dpid, destination_dpid):
        try:
            path = nx.shortest_path(
                self.net,
                source_dpid,
                destination_dpid,
                weight="weight",
            )

            path_text = "->".join(
                f"s{switch_id}"
                for switch_id in path
            )

            self.logger.info(
                "PATH_SELECTED source=s%d "
                "destination=s%d path=%s",
                source_dpid,
                destination_dpid,
                path_text,
            )

            return path

        except (
            nx.NetworkXNoPath,
            nx.NodeNotFound,
        ):
            self.logger.warning(
                "NO_PATH source=s%d destination=s%d",
                source_dpid,
                destination_dpid,
            )

            return None

    # ------------------------------------------------------------
    # Packet handling
    # ------------------------------------------------------------

    @set_ev_cls(
        ofp_event.EventOFPPacketIn,
        MAIN_DISPATCHER,
    )
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id

        in_port = msg.match["in_port"]

        parsed_packet = packet.Packet(msg.data)
        ethernet_headers = parsed_packet.get_protocols(
            ethernet.ethernet
        )

        if not ethernet_headers:
            return

        eth = ethernet_headers[0]

        # Ignore LLDP and IPv6 packets for this basic controller.
        if eth.ethertype in (
            ether_types.ETH_TYPE_LLDP,
            ether_types.ETH_TYPE_IPV6,
        ):
            return

        source_mac = eth.src
        destination_mac = eth.dst

        # Learn source hosts only when packets arrive on host-facing ports.
        if in_port not in self.link_ports.get(dpid, set()):
            previous_location = self.hosts.get(source_mac)
            current_location = (dpid, in_port)

            if previous_location != current_location:
                self.hosts[source_mac] = current_location

                self.logger.info(
                    "Host %s is at s%d port %d",
                    source_mac,
                    dpid,
                    in_port,
                )

        # Flood multicast and unknown-destination traffic.
        is_multicast = (
            int(destination_mac.split(":")[0], 16) & 1
        )

        if (
            is_multicast
            or destination_mac not in self.hosts
        ):
            self.flood(
                dp,
                in_port,
                msg,
            )
            return

        destination_dpid, destination_port = (
            self.hosts[destination_mac]
        )

        path = self.choose_path(
            dpid,
            destination_dpid,
        )

        if path is None:
            return

        # Install forward path.
        self.install_path(
            path,
            source_mac,
            destination_mac,
            destination_port,
        )

        # Install reverse path if the source host is already known.
        if (
            source_mac in self.hosts
            and self.hosts[source_mac][0] == dpid
        ):
            self.install_path(
                list(reversed(path)),
                destination_mac,
                source_mac,
                self.hosts[source_mac][1],
            )

        if len(path) == 1:
            output_port = destination_port
        else:
            output_port = self.net[dpid][path[1]][
                "ports"
            ][dpid]

        self.send_out(
            dp,
            msg,
            in_port,
            [output_port],
        )

    def install_path(
        self,
        path,
        source_mac,
        destination_mac,
        last_port,
    ):
        for index, dpid in enumerate(path):
            dp = self.dps.get(dpid)

            if dp is None:
                continue

            if index == len(path) - 1:
                output_port = last_port
            else:
                next_dpid = path[index + 1]

                output_port = self.net[dpid][next_dpid][
                    "ports"
                ][dpid]

            parser = dp.ofproto_parser

            match = parser.OFPMatch(
                eth_src=source_mac,
                eth_dst=destination_mac,
            )

            actions = [
                parser.OFPActionOutput(output_port)
            ]

            self.add_flow(
                dp,
                FLOW_PRIORITY,
                match,
                actions,
            )

    def flood(self, dp, in_port, msg):
        dpid = dp.id

        ports = set(
            self.edge_ports.get(dpid, set())
        )

        if dpid in self.tree:
            for neighbour in self.tree[dpid]:
                switch_port = self.net[dpid][neighbour][
                    "ports"
                ][dpid]

                ports.add(switch_port)

        ports.discard(in_port)

        self.send_out(
            dp,
            msg,
            in_port,
            sorted(ports),
        )

    def send_out(self, dp, msg, in_port, ports):
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        actions = [
            parser.OFPActionOutput(port)
            for port in ports
        ]

        if msg.buffer_id == ofp.OFP_NO_BUFFER:
            data = msg.data
        else:
            data = None

        packet_out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )

        dp.send_msg(packet_out)
