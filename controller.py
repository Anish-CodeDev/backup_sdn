"""Ryu SDN controller (OpenFlow 1.3) for the Distributed Backup System.

* discovers topology (run ryu-manager with --observe-links)
* learns host locations from ARP/IPv4 packet-ins (no flooding -> no loops)
* picks a path per host pair with Dijkstra (NetworkX) and installs flow rules
  on every switch of the path, both directions
* on link failure removes the link from its graph and proactively re-routes
  every affected flow (logs the reroute time)
* PATH_POLICY=hops (default) : fewest hops
  PATH_POLICY=load           : avoids links with high measured tx rate

Run:  PATH_POLICY=hops ryu-manager --observe-links controller.py
"""
import os
import time

import networkx as nx
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.lib.packet import ether_types, ethernet, packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event

FLOW_PRIORITY = 10
STATS_PERIOD = 2.0


def name(mac):
    """00:00:00:00:00:03 -> h3 (pretty logs)."""
    return "h%d" % int(mac.split(":")[-1], 16)


class BackupSDNController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.policy = os.environ.get("PATH_POLICY", "hops")
        self.graph = nx.DiGraph()      # dpid -> dpid, attr port = out port
        self.datapaths = {}
        self.link_ports = set()        # (dpid, port) that face another switch
        self.hosts = {}                # mac -> (dpid, port)
        self.paths = {}                # (macA, macB) sorted -> [dpids] A->B
        self.port_rate = {}            # (dpid, port) -> tx Mbit/s
        self._last = {}
        self.logger.info("Path policy: %s", self.policy)
        hub.spawn(self._monitor)

    # ------------------------------------------------------------ switches
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=0,
                                      match=parser.OFPMatch(), instructions=inst))

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state(self, ev):
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[ev.datapath.id] = ev.datapath
        else:
            self.datapaths.pop(ev.datapath.id, None)

    # ------------------------------------------------------------ topology
    @set_ev_cls(event.EventLinkAdd)
    def _link_add(self, ev):
        s, d = ev.link.src, ev.link.dst
        self.graph.add_edge(s.dpid, d.dpid, port=s.port_no)
        self.link_ports.add((s.dpid, s.port_no))
        self.logger.info("LINK UP   s%s:%s -> s%s", s.dpid, s.port_no, d.dpid)
        self._reoptimize()

    @set_ev_cls(event.EventLinkDelete)
    def _link_delete(self, ev):
        t0 = time.time()
        s, d = ev.link.src, ev.link.dst
        if self.graph.has_edge(s.dpid, d.dpid):
            self.graph.remove_edge(s.dpid, d.dpid)
        self.link_ports.discard((s.dpid, s.port_no))
        self.logger.info("LINK DOWN s%s:%s -> s%s  (t=%.3f)", s.dpid, s.port_no, d.dpid, t0)
        self._reroute_broken()
        self.logger.info("reroute handling took %.1f ms", (time.time() - t0) * 1000)

    # ------------------------------------------------------- path computing
    def _weight(self, u, v, d):
        if self.policy == "load":
            return 1.0 + self.port_rate.get((u, d["port"]), 0.0)
        return 1.0

    def _usable_graph(self):
        """Only links that are alive in BOTH directions."""
        h = nx.DiGraph()
        for u, v, d in self.graph.edges(data=True):
            if self.graph.has_edge(v, u):
                h.add_edge(u, v, **d)
        return h

    def _compute_path(self, src_dpid, dst_dpid):
        if src_dpid == dst_dpid:
            return [src_dpid]
        try:
            return nx.shortest_path(self._usable_graph(), src_dpid, dst_dpid,
                                    weight=self._weight)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def _path_valid(self, path):
        return all(self.graph.has_edge(u, v) and self.graph.has_edge(v, u)
                   for u, v in zip(path, path[1:]))

    def _cost(self, path):
        return sum(self._weight(u, v, self.graph[u][v]) for u, v in zip(path, path[1:]))

    # ------------------------------------------------------- flow handling
    def _add_flow(self, dp, src, dst, out_port):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        match = parser.OFPMatch(eth_src=src, eth_dst=dst)
        inst = [parser.OFPInstructionActions(
            ofp.OFPIT_APPLY_ACTIONS, [parser.OFPActionOutput(out_port)])]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=FLOW_PRIORITY,
                                      match=match, instructions=inst))

    def _del_flow(self, dp, src, dst):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, command=ofp.OFPFC_DELETE, out_port=ofp.OFPP_ANY,
            out_group=ofp.OFPG_ANY, match=parser.OFPMatch(eth_src=src, eth_dst=dst)))

    def _program(self, src, dst, path, last_port):
        for i, dpid in enumerate(path):
            out = last_port if i == len(path) - 1 else self.graph[dpid][path[i + 1]]["port"]
            if dpid in self.datapaths:
                self._add_flow(self.datapaths[dpid], src, dst, out)

    def _install(self, key, path):
        a, b = key
        self._program(a, b, path, self.hosts[b][1])
        self._program(b, a, path[::-1], self.hosts[a][1])
        self.paths[key] = path

    def _remove(self, key):
        a, b = key
        for dpid in self.paths.pop(key, []):
            if dpid in self.datapaths:
                self._del_flow(self.datapaths[dpid], a, b)
                self._del_flow(self.datapaths[dpid], b, a)

    def _move(self, key, new_path):
        old = self.paths.get(key)
        self._remove(key)
        if new_path is None:
            self.logger.warning("NO PATH %s <-> %s", name(key[0]), name(key[1]))
            return
        self._install(key, new_path)
        self.logger.info("REROUTE %s<->%s: %s -> %s", name(key[0]), name(key[1]),
                         ["s%s" % d for d in (old or [])], ["s%s" % d for d in new_path])

    def _reroute_broken(self):
        for key, path in list(self.paths.items()):
            if not self._path_valid(path):
                self._move(key, self._compute_path(self.hosts[key[0]][0],
                                                   self.hosts[key[1]][0]))

    def _reoptimize(self):
        """A link came up: switch only if the new path is strictly cheaper."""
        for key, path in list(self.paths.items()):
            new = self._compute_path(self.hosts[key[0]][0], self.hosts[key[1]][0])
            if new and new != path and (not self._path_valid(path)
                                        or self._cost(new) < self._cost(path) - 1e-9):
                self._move(key, new)

    # ---------------------------------------------------------- packet-in
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        in_port = msg.match["in_port"]
        eth = packet.Packet(msg.data).get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype not in (ether_types.ETH_TYPE_ARP,
                                                ether_types.ETH_TYPE_IP):
            return
        src, dst = eth.src, eth.dst
        if (dp.id, in_port) not in self.link_ports:      # edge port -> host
            if self.hosts.get(src) != (dp.id, in_port):
                self.hosts[src] = (dp.id, in_port)
                self.logger.info("HOST %s at s%s:%s", name(src), dp.id, in_port)
        if dst not in self.hosts or src not in self.hosts:
            return                                       # unknown / broadcast: drop
        key = tuple(sorted((src, dst)))
        path = self.paths.get(key)
        if path is None or not self._path_valid(path):
            path = self._compute_path(self.hosts[key[0]][0], self.hosts[key[1]][0])
            if path is None:
                return
            self._remove(key)
            self._install(key, path)
            self.logger.info("PATH %s<->%s via %s  [%s]", name(key[0]), name(key[1]),
                             ["s%s" % d for d in path], self.policy)
        parser, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=in_port,
            actions=[parser.OFPActionOutput(ofp.OFPP_TABLE)], data=msg.data))

    # ------------------------------------------------ stats (load policy)
    def _monitor(self):
        while True:
            hub.sleep(STATS_PERIOD)
            if self.policy != "load":
                continue
            for dp in list(self.datapaths.values()):
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(
                    dp, 0, dp.ofproto.OFPP_ANY))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _stats_reply(self, ev):
        dpid, now = ev.msg.datapath.id, time.time()
        for st in ev.msg.body:
            key, prev = (dpid, st.port_no), self._last.get((dpid, st.port_no))
            if prev and now > prev[1]:
                self.port_rate[key] = (st.tx_bytes - prev[0]) * 8 / (now - prev[1]) / 1e6
            self._last[key] = (st.tx_bytes, now)
