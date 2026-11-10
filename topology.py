#!/usr/bin/env python3
"""Mininet topology for the Distributed Backup System.

                 +-- s2 --+
   h1 -- s1 -----+        +---- s4 --+-- h3 (backup server 1)
                 +-- s3 --+          +-- h4 (backup server 2)

Two equal-cost paths between s1 and s4 -> the controller has a real choice
and something to fail over to.

Run (controller must already be running):  sudo python3 topology.py
"""
import argparse
import time

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController

CLIENT_IP, SERVER_IPS = "10.0.0.1", ["10.0.0.3", "10.0.0.4"]


def build_net(bw=20, delay="5ms", ctrl_ip="127.0.0.1", ctrl_port=6653):
    net = Mininet(controller=None, switch=OVSKernelSwitch, link=TCLink,
                  autoSetMacs=False, build=False)
    net.addController("c0", controller=RemoteController, ip=ctrl_ip, port=ctrl_port)

    h1 = net.addHost("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")   # client
    h3 = net.addHost("h3", ip="10.0.0.3/24", mac="00:00:00:00:00:03")   # server 1
    h4 = net.addHost("h4", ip="10.0.0.4/24", mac="00:00:00:00:00:04")   # server 2
    sw = {n: net.addSwitch(n, protocols="OpenFlow13", failMode="secure")
          for n in ("s1", "s2", "s3", "s4")}

    # explicit port numbers so the controller logs / experiments are predictable
    net.addLink(h1, sw["s1"], port2=1, bw=100)
    net.addLink(sw["s1"], sw["s2"], port1=2, port2=1, bw=bw, delay=delay)
    net.addLink(sw["s1"], sw["s3"], port1=3, port2=1, bw=bw, delay=delay)
    net.addLink(sw["s2"], sw["s4"], port1=2, port2=1, bw=bw, delay=delay)
    net.addLink(sw["s3"], sw["s4"], port1=2, port2=2, bw=bw, delay=delay)
    net.addLink(h3, sw["s4"], port2=3, bw=100)
    net.addLink(h4, sw["s4"], port2=4, bw=100)
    net.build()
    return net


def prepare(net, retries=5):
    """Let the controller discover links + hosts, then verify connectivity."""
    info("*** waiting for switches / LLDP discovery\n")
    time.sleep(6)
    net.staticArp()                       # no ARP broadcasts -> no loops
    info("*** announcing hosts to the controller\n")
    for h in net.hosts:                   # ARP request for a dummy IP => packet-in
        h.cmd("ping -c1 -W1 10.0.0.250")
    time.sleep(1)
    for _ in range(retries):
        if net.pingAll(timeout=2) == 0:
            return True
        time.sleep(2)
    return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bw", type=float, default=20, help="core link bandwidth (Mbit/s)")
    ap.add_argument("--delay", default="5ms")
    ap.add_argument("--controller-ip", default="127.0.0.1")
    a = ap.parse_args()
    setLogLevel("info")
    net = build_net(a.bw, a.delay, a.controller_ip)
    net.start()
    if not prepare(net):
        info("*** WARNING: pingAll failed - is the controller running with --observe-links?\n")
    CLI(net)
    net.stop()
