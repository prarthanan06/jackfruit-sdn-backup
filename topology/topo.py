from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel


def build():
    net = Mininet(controller=None,
                  switch=lambda name, **kw: OVSSwitch(name, protocols="OpenFlow13", **kw))

    c0 = net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)

    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h2 = net.addHost("h2", ip="10.0.0.2/24")
    h3 = net.addHost("h3", ip="10.0.0.3/24")
    h4 = net.addHost("h4", ip="10.0.0.4/24")

    s1, s2, s3, s4, s5 = [net.addSwitch(f"s{i}") for i in range(1, 6)]

    # hosts
    net.addLink(h1, s1)
    net.addLink(h2, s1)
    net.addLink(h3, s4)
    net.addLink(h4, s5)

    # redundant core
    net.addLink(s1, s2)
    net.addLink(s1, s3)
    net.addLink(s2, s4)
    net.addLink(s2, s5)
    net.addLink(s3, s4)
    net.addLink(s3, s5)

    net.start()
    print("Topology up. Controller must be running at 127.0.0.1:6653")
    CLI(net)
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    build()
