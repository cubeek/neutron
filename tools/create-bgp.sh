#!/bin/bash

ovn_nbctl='oc exec ovsdbserver-nb-0 -- ovn-nbctl --no-leader-only'
ovn_sbctl='oc exec ovsdbserver-sb-0 -- ovn-sbctl --no-leader-only'
inter_switch=ls-interconnect
bgp_router=bgp-main-router
public_subnet=192.168.111.0/24

# Convert MAC to IPv6 LLA
# Generated with claude-4-sonnet-2025-07-22
function mac_to_ipv6_lla() {
    local mac=$1
    # Convert MAC to lower case and replace '-' with ':'
    mac=$(echo "$mac" | tr 'A-F' 'a-f' | tr '-' ':' | tr -d '"')
    IFS=':' read -r -a octets <<< "$mac"

    # Flip the 7th bit of the first octet
    first_octet=$(( 0x${octets[0]} ^ 0x02 ))
    first_octet=$(printf "%02x" $first_octet)

    # Construct EUI-64: insert ff:fe in the middle
    eui64="$first_octet:${octets[1]}:${octets[2]}:ff:fe:${octets[3]}:${octets[4]}:${octets[5]}"

    # Format as IPv6 LLA
    printf "fe80::%s%s:%sff:fe%s:%s%s\n" \
        "$first_octet" "${octets[1]}" "${octets[2]}" "${octets[3]}" "${octets[4]}" "${octets[5]}"
}


function connect_router_and_switch() {
    local router=$1
    local switch=$2
    local mac=$3
    local network=$4

    local lsp_name=lsp-$switch-to-$router
    local lrp_name=lrp-$router-to-$switch

    echo "Connecting $router with $switch, using $mac and $network"

    $ovn_nbctl lrp-add $router $lrp_name $mac $network \
           -- lsp-add $switch $lsp_name \
           -- lsp-set-addresses $lsp_name router \
           -- lsp-set-type $lsp_name router \
           -- lsp-set-options $lsp_name router-port=$lrp_name
}

function add_localnet() {
    local switch_name=$1
    local physnet_name=$2

    local lsp_localnet_name=lsp-$switch_name-localnet

    $ovn_nbctl lsp-add "$switch_name" "$lsp_localnet_name" \
      -- set Logical_Switch_Port "$lsp_localnet_name" type=localnet \
         options:network_name="$physnet_name" addresses=unknown
}

function configure_chassis() {
    local chassis=$1

    local chassis_name=$($ovn_sbctl --column hostname list chassis $chassis |  awk '{ print $3 }' | cut -d \. -f 1)
    local rack_num=$(echo "$chassis_name" | sed -n 's/^r\([0-9]\+\)-compute.*/\1/p')
    local chassis_router=bgp-router-$chassis_name
    local lrp_chassis_router=lrp-$chassis_router-to-$bgp_router
    local lrp_main_router=lrp-$bgp_router-to-$chassis_router

    $ovn_nbctl ha-chassis-group-add ha-group-$chassis_name \
      -- ha-chassis-group-add-chassis ha-group-$chassis_name $chassis 10
    ha_chassis_group_id=$($ovn_nbctl --columns _uuid find ha-chassis-group name=ha-group-$chassis_name | awk '{ print $3 }')

    # Connect chassis router to bgp router
    $ovn_nbctl lr-add $chassis_router \
      -- set Logical_Router $chassis_router options:chassis=$chassis \
      -- lrp-add $chassis_router $lrp_chassis_router 00:de:ad:00:0${rack_num}:00 \
      -- lrp-add $bgp_router $lrp_main_router 00:de:ad:00:1${rack_num}:00 \
      -- set Logical_Router_Port $lrp_chassis_router peer=$lrp_main_router \
      -- set Logical_Router_Port $lrp_chassis_router networks=169.254.$rack_num.1/30 \
      -- set Logical_Router_Port $lrp_main_router peer=$lrp_chassis_router \
      -- set Logical_Router_Port $lrp_main_router networks=169.254.$rack_num.2/30 \
      -- set Logical_Router_Port $lrp_main_router ha_chassis_group=$ha_chassis_group_id \
      -- set Logical_Router_Port $lrp_main_router options:dynamic-routing-maintain-vrf="true"

    # Connect chassis router to chassis switches
    for i in 2 3; do
      switch_name=ls-$chassis_name-eth$i
      $ovn_nbctl ls-add $switch_name
      connect_router_and_switch $chassis_router $switch_name 00:de:ad:2${rack_num}:0$i:00 100.$((62+$i)).$rack_num.2
      add_localnet $switch_name  net-eth$i
      $ovn_nbctl --ecmp lr-route-add $chassis_router 0.0.0.0/0 100.$((62+$i)).$rack_num.1 lrp-$chassis_router-to-$switch_name
    done
#    lrp_main_router_ip=$(mac_to_ipv6_lla $($ovn_nbctl get logical-router-port $lrp_main_router mac))
    $ovn_nbctl lr-route-add $chassis_router $public_subnet 169.254.$rack_num.2 $lrp_chassis_router

    $ovn_nbctl lr-policy-add $bgp_router 10 "inport==\"lrp-$bgp_router-to-$inter_switch\" && is_chassis_resident(\"cr-$lrp_main_router\")" "reroute" "169.254.$rack_num.1"
}

function configure_bgp_distributed_router() {
    local gw_ip=$1
    local neutron_switch=$2
    local lrp_bgp_router_to_inter_switch=lrp-$bgp_router-to-$inter_switch

    $ovn_nbctl lr-add $bgp_router

    $ovn_nbctl lsp-add $inter_switch lsp-$inter_switch-to-$bgp_router \
      -- set Logical_Switch_Port lsp-$inter_switch-to-$bgp_router type=router \
      -- set Logical_Switch_Port lsp-$inter_switch-to-$bgp_router options:router-port=lrp-$bgp_router-to-$inter_switch \
      -- set Logical_Switch_Port lsp-$inter_switch-to-$bgp_router addresses=router \
      -- lrp-add $bgp_router lrp-$bgp_router-to-$inter_switch 00:de:ad:10:00:00 $gw_ip/24 \
      -- set Logical_Router $bgp_router options:dynamic-routing="true" \
      -- set Logical_Router $bgp_router options:dynamic-routing-redistribute=nat \
      -- set Logical_Router $bgp_router options:requested-tnl-key="42"

    connect_router_and_switch $bgp_router $neutron_switch 00:de:ad:de:ad:00

    $ovn_nbctl lr-route-add $bgp_router $public_subnet $gw_ip $lrp_bgp_router_to_inter_switch \
      -- lr-route-add $bgp_router 0.0.0.0/0 $gw_ip
}

neutron_localnet=$($ovn_nbctl --columns _uuid find logical-switch-port type=localnet options:network_name=datacentre | awk '{ print $3 }')
neutron_switch=$($ovn_nbctl --columns name find logical-switch "ports{>=}$neutron_localnet" | awk '{ print $3 }')

$ovn_nbctl ls-add $inter_switch
add_localnet $inter_switch datacentre
configure_bgp_distributed_router 192.168.111.30 $neutron_switch

for chassis in $($ovn_sbctl --columns hostname,name list chassis | grep -A1 "hostname.*: .*-compute-" | awk '/^name/{ print $3 }' | tr -d '"'); do
    configure_chassis $chassis
done
