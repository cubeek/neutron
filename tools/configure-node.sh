#!/bin/bash

INTERFACES="eth2 eth3"
index=2
rack=$(hostname | sed 's/r\([0-9]\)-compute.*/\1/')

for interface in $INTERFACES; do
   eth_ip=$(ip -4 addr show dev $interface | awk '/inet / {print $2}' | grep -v '^127\.' | head -n 1)

   ovs-vsctl add-br br-$interface \
     -- add-port br-$interface $interface

   ip addr add $eth_ip dev br-$interface
   ip addr del $eth_ip dev $interface
   ip link set dev br-$interface up
   ovs-ofctl add-flow br-$interface priority=10,ip,in_port=$interface,nw_dst=$eth_ip,actions=NORMAL

   # Parse IPv4 addresses (excluding 127.0.0.1)
   ip -4 addr show lo | awk '/inet / {print $2}' | grep -v '^127\.' | while read ip; do
       ip=${ip%%/*}
       ovs-ofctl add-flow br-$interface priority=10,ip,in_port=$interface,nw_dst=$ip,actions=NORMAL
   done

   # Parse IPv6 addresses (excluding ::1)
   ip -6 addr show lo | awk '/inet6 / {print $2}' | grep -v '^::1' | while read ip; do
       ip=${ip%%/*}
       ovs-ofctl add-flow br-$interface priority=10,ipv6,in_port=$interface,ipv6_dst=$ip,actions=NORMAL
   done

   ovs-ofctl add-flow br-$interface priority=10,arp,actions=NORMAL
   ovs-ofctl add-flow br-$interface priority=10,icmp6,icmp_type=135,actions=NORMAL
   ovs-ofctl add-flow br-$interface priority=10,icmp6,icmp_type=136,actions=NORMAL
   ovs-ofctl add-flow br-$interface priority=10,icmp6,icmp_type=133,actions=NORMAL
   ovs-ofctl add-flow br-$interface priority=10,icmp6,icmp_type=134,actions=NORMAL
   ovs-ofctl add-flow br-$interface priority=10,ipv6,in_port=$interface,ipv6_dst=fe80::/64,actions=NORMAL
   ovs-ofctl add-flow br-$interface priority=8,in_port=$interface,actions=mod_dl_dst:00:de:ad:2${rack}:0${index}:00,NORMAL

   bms=$(ovs-vsctl get open . external_ids:ovn-bridge-mappings | tr -d '"')
   ovs-vsctl set open . external_ids:ovn-bridge-mappings="$bms,net-$interface:br-$interface"
   ovs-vsctl remove open . external_ids ovn-chassis-mac-mappings

   index=$(($index+1))
done

ofport1=$(ovs-vsctl get interface $(ovs-vsctl list-ports br-ex | head -n1) ofport)
ofport2=$(ovs-vsctl get interface $(ovs-vsctl list-ports br-ex | tail -n1) ofport)
ovs-ofctl del-flows br-ex
ovs-ofctl add-flow br-ex priority=10,in_port=$ofport1,actions=output:$ofport2
ovs-ofctl add-flow br-ex priority=10,in_port=$ofport2,actions=output:$ofport1


# configure frr
cat <<EOF > /var/lib/config-data/ansible-generated/frr/etc/frr/frr.conf
! Ansible managed

frr version 7.0
frr defaults traditional
hostname $(hostname)
log stdout informational
log file /var/log/frr/frr.log debug
log timestamp precision 3
service integrated-vtysh-config
line vty
debug bgp updates
debug bgp neighbor-events
debug bgp update-groups
debug bgp graceful-restart

router bgp 64999 vrf ovnvrf42
  bgp log-neighbor-changes
  no bgp default ipv4-unicast
  no bgp ebgp-requires-policy

  address-family ipv4 unicast
    redistribute kernel
  exit-address-family

router bgp 64999
  bgp router-id 99.99.$rack.2
  bgp log-neighbor-changes
  bgp graceful-shutdown
  no bgp default ipv4-unicast
  no bgp ebgp-requires-policy

  neighbor uplink peer-group
  neighbor uplink remote-as internal
  neighbor uplink password f00barZ
  neighbor 100.64.$rack.1 peer-group uplink
  neighbor 100.65.$rack.1 peer-group uplink

  neighbor uplink ttl-security hops 1

  address-family ipv4 unicast
    redistribute connected
    redistribute static
    import vrf ovnvrf42
    neighbor uplink activate
    neighbor uplink allowas-in origin
    neighbor uplink prefix-list only-host-prefixes out
  exit-address-family

!route-map set-nh-to-lrp permit 5
!  match ip address prefix-list nexthop-route
!route-map set-nh-to-lrp permit 10
!  match ip address prefix-list workload-routes
!  set ip next-hop 99.98.2.2

route-map rm-only-default permit 10
  match ip address prefix-list only-default
  set src 99.99.$rack.2

ip prefix-list only-default permit 0.0.0.0/0
ip prefix-list only-host-prefixes permit 0.0.0.0/0 ge 32

ip protocol bgp route-map rm-only-default

ip nht resolve-via-default
EOF

systemctl restart edpm_frr