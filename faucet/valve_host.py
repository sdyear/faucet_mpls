"""Manage host learning on VLANs."""

# Copyright (C) 2013 Nippon Telegraph and Telephone Corporation.
# Copyright (C) 2015 Brad Cowie, Christopher Lorier and Joe Stringer.
# Copyright (C) 2015 Research and Education Advanced Network New Zealand Ltd.
# Copyright (C) 2015--2019 The Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from faucet import valve_of
from faucet.valve_manager_base import ValveManagerBase


class ValveHostManager(ValveManagerBase):
    """Manage host learning on VLANs."""

    def __init__(self, logger, ports, vlans, eth_src_table, eth_dst_table,
                 eth_dst_hairpin_table, pipeline, learn_timeout, learn_jitter,
                 learn_ban_timeout, cache_update_guard_time, idle_dst, stack,
                 has_externals, stack_root_flood_reflection):
        self.logger = logger
        self.ports = ports
        self.vlans = vlans
        self.eth_src_table = eth_src_table
        self.eth_dst_table = eth_dst_table
        self.eth_dst_hairpin_table = eth_dst_hairpin_table
        self.pipeline = pipeline
        self.learn_timeout = learn_timeout
        self.learn_jitter = learn_jitter
        self.learn_ban_timeout = learn_ban_timeout
        self.low_priority = self._LOW_PRIORITY
        self.host_priority = self._MATCH_PRIORITY
        self.cache_update_guard_time = cache_update_guard_time
        self.output_table = self.eth_dst_table
        self.idle_dst = idle_dst
        self.stack = stack
        self.has_externals = has_externals
        self.stack_root_flood_reflection = stack_root_flood_reflection
        if self.eth_dst_hairpin_table:
            self.output_table = self.eth_dst_hairpin_table

    def ban_rules(self, pkt_meta):
        """Limit learning to a maximum configured on this port/VLAN.

        Args:
            pkt_meta: PacketMeta instance.
        Returns:
            list: OpenFlow messages, if any.
        """
        ofmsgs = []

        port = pkt_meta.port
        eth_src = pkt_meta.eth_src
        vlan = pkt_meta.vlan

        entry = vlan.cached_host(eth_src)
        if entry is None:
            if port.max_hosts:
                if port.hosts_count() == port.max_hosts:
                    ofmsgs.append(self._temp_ban_host_learning(
                        self.eth_src_table.match(in_port=port.number)))
                    port.dyn_learn_ban_count += 1
                    self.logger.info(
                        'max hosts %u reached on %s, '
                        'temporarily banning learning on this port, '
                        'and not learning %s' % (
                            port.max_hosts, port, eth_src))
            if vlan is not None and vlan.max_hosts:
                hosts_count = vlan.hosts_count()
                if hosts_count == vlan.max_hosts:
                    ofmsgs.append(self._temp_ban_host_learning(self.eth_src_table.match(vlan=vlan)))
                    vlan.dyn_learn_ban_count += 1
                    self.logger.info(
                        'max hosts %u reached on VLAN %u, '
                        'temporarily banning learning on this VLAN, '
                        'and not learning %s on %s' % (
                            vlan.max_hosts, vlan.vid, eth_src, port))
        return ofmsgs

    def del_port(self, port):
        ofmsgs = []
        ofmsgs.append(
            self.eth_src_table.flowdel(self.eth_src_table.match(in_port=port.number)))
        for table in (self.eth_dst_table, self.eth_dst_hairpin_table):
            if table:
                # per OF 1.3.5 B.6.23, the OFA will match flows
                # that have an action targeting this port.
                ofmsgs.append(table.flowdel(out_port=port.number))
        vlans = port.vlans()
        if port.stack:
            vlans = self.vlans.values()
        for vlan in vlans:
            vlan.clear_cache_hosts_on_port(port)
        return ofmsgs

    def initialise_tables(self):
        ofmsgs = []
        for vlan in self.vlans.values():
            ofmsgs.append(self.eth_src_table.flowcontroller(
                match=self.eth_src_table.match(vlan=vlan),
                priority=self.low_priority,
                inst=[self.eth_src_table.goto(self.output_table)]))
        return ofmsgs

    def _temp_ban_host_learning(self, match):
        return self.eth_src_table.flowdrop(
            match,
            priority=(self.low_priority + 1),
            hard_timeout=self.learn_ban_timeout)

    def delete_host_from_vlan(self, eth_src, vlan):
        """Delete a host from a VLAN."""
        ofmsgs = [self.eth_src_table.flowdel(
            self.eth_src_table.match(vlan=vlan, eth_src=eth_src))]
        for table in (self.eth_dst_table, self.eth_dst_hairpin_table):
            if table:
                ofmsgs.append(table.flowdel(table.match(vlan=vlan, eth_dst=eth_src)))
        return ofmsgs

    def expire_hosts_from_vlan(self, vlan, now):
        """Expire hosts from VLAN cache."""
        expired_hosts = vlan.expire_cache_hosts(now, self.learn_timeout)
        if expired_hosts:
            vlan.dyn_last_time_hosts_expired = now
            self.logger.info(
                '%u recently active hosts on VLAN %u, expired %s' % (
                    vlan.hosts_count(), vlan.vid, expired_hosts))
        return expired_hosts

    def _jitter_learn_timeout(self, base_learn_timeout, port, eth_dst):
        """Calculate jittered learning timeout to avoid synchronized host timeouts."""
        # Hosts on this port never timeout.
        if port.permanent_learn:
            return 0
        if not base_learn_timeout:
            return 0
        # Jitter learn timeout based on eth address, so timeout processing is jittered,
        # the same hosts will timeout approximately the same time on a stack.
        jitter = hash(eth_dst) % self.learn_jitter
        min_learn_timeout = base_learn_timeout - self.learn_jitter
        return int(max(abs(min_learn_timeout + jitter), self.cache_update_guard_time))

    def learn_host_timeouts(self, port, eth_src):
        """Calculate flow timeouts for learning on a port."""
        learn_timeout = self._jitter_learn_timeout(self.learn_timeout, port, eth_src)

        # Update datapath to no longer send packets from this mac to controller
        # note the use of hard_timeout here and idle_timeout for the dst table
        # this is to ensure that the source rules will always be deleted before
        # any rules on the dst table. Otherwise if the dst table rule expires
        # but the src table rule is still being hit intermittantly the switch
        # will flood packets to that dst and not realise it needs to relearn
        # the rule
        # NB: Must be lower than highest priority otherwise it can match
        # flows destined to controller
        src_rule_idle_timeout = 0
        src_rule_hard_timeout = learn_timeout
        dst_rule_idle_timeout = learn_timeout + self.cache_update_guard_time
        if not self.idle_dst:
            dst_rule_idle_timeout = 0
        return (src_rule_idle_timeout, src_rule_hard_timeout, dst_rule_idle_timeout)

    def learn_host_intervlan_routing_flows(self, port, vlan, eth_src, eth_dst):
        """Returns flows for the eth_src_table that enable packets that have been
           routed to be accepted from an adjacent DP and then switched to the destination.
           Eth_src_table flow rule to match on port, eth_src, eth_dst and vlan

        Args:
            port (Port): Port to match on.
            vlan (VLAN): VLAN to match on
            eth_src: source MAC address (should be the router MAC)
            eth_dst: destination MAC address
        """
        ofmsgs = []
        (src_rule_idle_timeout, src_rule_hard_timeout, _) = self.learn_host_timeouts(port, eth_src)
        src_match = self.eth_src_table.match(vlan=vlan, eth_src=eth_src, eth_dst=eth_dst)
        src_priority = self.host_priority - 1
        inst = [self.eth_src_table.goto(self.output_table)]
        ofmsgs.extend([self.eth_src_table.flowmod(
            match=src_match,
            priority=src_priority,
            inst=inst,
            hard_timeout=src_rule_hard_timeout,
            idle_timeout=src_rule_idle_timeout)])
        return ofmsgs

    def learn_host_on_vlan_port_flows(self, port, vlan, eth_src,
                                      delete_existing, refresh_rules,
                                      src_rule_idle_timeout,
                                      src_rule_hard_timeout,
                                      dst_rule_idle_timeout):
        """Return flows that implement learning a host on a port."""
        ofmsgs = []

        # Delete any existing entries for MAC.
        if delete_existing:
            ofmsgs.extend(self.delete_host_from_vlan(eth_src, vlan))

        # Associate this MAC with source port.
        src_match = self.eth_src_table.match(
            in_port=port.number, vlan=vlan, eth_src=eth_src)
        src_priority = self.host_priority - 1

        inst = []

        inst.append(self.eth_src_table.goto(self.output_table))

        ofmsgs.append(self.eth_src_table.flowmod(
            match=src_match,
            priority=src_priority,
            inst=inst,
            hard_timeout=src_rule_hard_timeout,
            idle_timeout=src_rule_idle_timeout))

        hairpinning = port.hairpin or port.hairpin_unicast
        # If we are refreshing only and not in hairpin mode, leave existing eth_dst alone.
        if refresh_rules and not hairpinning:
            return ofmsgs

        external_forwarding_requested = None
        match_dict = {
            'vlan': vlan, 'eth_dst': eth_src, valve_of.EXTERNAL_FORWARDING_FIELD: None}
        if self.has_externals:
            match_dict.update({
                valve_of.EXTERNAL_FORWARDING_FIELD: valve_of.PCP_EXT_PORT_FLAG})
            if port.tagged_vlans and port.loop_protect_external and self.stack:
                external_forwarding_requested = False
            elif not port.stack:
                external_forwarding_requested = True

        inst = self.pipeline.output(
            port, vlan, external_forwarding_requested=external_forwarding_requested)

        # Output packets for this MAC to specified port.
        ofmsgs.append(self.eth_dst_table.flowmod(
            self.eth_dst_table.match(**match_dict),
            priority=self.host_priority,
            inst=inst,
            idle_timeout=dst_rule_idle_timeout))

        if self.has_externals and not port.loop_protect_external:
            match_dict.update({
                valve_of.EXTERNAL_FORWARDING_FIELD: valve_of.PCP_NONEXT_PORT_FLAG})
            ofmsgs.append(self.eth_dst_table.flowmod(
                self.eth_dst_table.match(**match_dict),
                priority=self.host_priority,
                inst=inst,
                idle_timeout=dst_rule_idle_timeout))

        # If port is in hairpin mode, install a special rule
        # that outputs packets destined to this MAC back out the same
        # port they came in (e.g. multiple hosts on same WiFi AP,
        # and FAUCET is switching between them on the same port).
        if hairpinning:
            ofmsgs.append(self.eth_dst_hairpin_table.flowmod(
                self.eth_dst_hairpin_table.match(in_port=port.number, vlan=vlan, eth_dst=eth_src),
                priority=self.host_priority,
                inst=self.pipeline.output(port, vlan, hairpin=True),
                idle_timeout=dst_rule_idle_timeout))

        return ofmsgs

    def learn_host_on_vlan_ports(self, now, port, vlan, eth_src,
                                 delete_existing=True,
                                 last_dp_coldstart_time=None):
        """Learn a host on a port."""
        ofmsgs = []
        cache_port = None
        cache_age = None
        entry = vlan.cached_host(eth_src)
        refresh_rules = False

        # Host not cached, and no hosts expired since we cold started
        # Enable faster learning by assuming there's no previous host to delete
        if entry is None:
            if (last_dp_coldstart_time and
                    (vlan.dyn_last_time_hosts_expired is None or
                     vlan.dyn_last_time_hosts_expired < last_dp_coldstart_time)):
                delete_existing = False
        elif entry.port.permanent_learn:
            if entry.port != port:
                ofmsgs.extend(self.pipeline.filter_packets(
                    {'eth_src': eth_src, 'in_port': port.number}))
            return (ofmsgs, entry.port, False)
        else:
            cache_age = now - entry.cache_time
            cache_port = entry.port

        if cache_port is not None:
            # packet was received on same member of a LAG.
            same_lag = (port.lacp and port.lacp == cache_port.lacp)
            # stacks of size > 2 will have an unknown MAC flooded towards the root,
            # and flooded down again. If we learned the MAC on a local port and
            # heard the reflected flooded copy, discard the reflection.
            local_stack_learn = (
                self.stack_root_flood_reflection and port.stack and not cache_port.stack)
            guard_time = self.cache_update_guard_time
            if cache_port == port or same_lag or local_stack_learn:
                # aggressively re-learn on LAGs, and prefer recently learned
                # locally learned hosts on a stack.
                if same_lag or local_stack_learn:
                    guard_time = 2
                # recent cache update, don't do anything.
                if cache_age < guard_time:
                    return (ofmsgs, cache_port, False)
                # skip delete if host didn't change ports or on same LAG.
                if cache_port == port or same_lag:
                    delete_existing = False
                    refresh_rules = True

        if port.loop_protect:
            ban_age = None
            learn_ban = False

            # if recently in loop protect mode and still receiving packets,
            # prolong the ban
            if port.dyn_last_ban_time:
                ban_age = now - port.dyn_last_ban_time
                if ban_age < self.cache_update_guard_time:
                    learn_ban = True

            # if not in protect mode and we get a rapid move, enact protect mode
            if not learn_ban and entry is not None:
                if port != cache_port and cache_age < self.cache_update_guard_time:
                    learn_ban = True
                    port.dyn_learn_ban_count += 1
                    self.logger.info('rapid move of %s from %s to %s, temp loop ban %s' % (
                        eth_src, cache_port, port, port))

            # already, or newly in protect mode, apply the ban rules.
            if learn_ban:
                port.dyn_last_ban_time = now
                ofmsgs.append(self._temp_ban_host_learning(
                    self.eth_src_table.match(in_port=port.number)))
                return (ofmsgs, cache_port, False)

        (src_rule_idle_timeout,
         src_rule_hard_timeout,
         dst_rule_idle_timeout) = self.learn_host_timeouts(port, eth_src)

        ofmsgs.extend(self.learn_host_on_vlan_port_flows(
            port, vlan, eth_src, delete_existing, refresh_rules,
            src_rule_idle_timeout, src_rule_hard_timeout,
            dst_rule_idle_timeout))

        return (ofmsgs, cache_port, True)

    def flow_timeout(self, _now, _table_id, _match):
        """Handle a flow timed out message from dataplane."""
        return []


class ValveHostFlowRemovedManager(ValveHostManager):
    """Trigger relearning on flow removed notifications.

    .. note::

        not currently reliable.
    """

    def flow_timeout(self, now, table_id, match):
        ofmsgs = []
        if table_id in (self.eth_src_table.table_id, self.eth_dst_table.table_id):
            if 'vlan_vid' in match:
                vlan = self.vlans[valve_of.devid_present(match['vlan_vid'])]
                in_port = None
                eth_src = None
                eth_dst = None
                for field, value in match.items():
                    if field == 'in_port':
                        in_port = value
                    elif field == 'eth_src':
                        eth_src = value
                    elif field == 'eth_dst':
                        eth_dst = value
                if eth_src and in_port:
                    port = self.ports[in_port]
                    ofmsgs.extend(self._src_rule_expire(vlan, port, eth_src))
                elif eth_dst:
                    ofmsgs.extend(self._dst_rule_expire(now, vlan, eth_dst))
        return ofmsgs

    def expire_hosts_from_vlan(self, _vlan, _now):
        return []

    def learn_host_timeouts(self, port, eth_src):
        """Calculate flow timeouts for learning on a port."""
        learn_timeout = self._jitter_learn_timeout(self.learn_timeout, port, eth_src)

        # Disable hard_time, dst rule expires after src rule.
        src_rule_idle_timeout = learn_timeout
        src_rule_hard_timeout = 0
        dst_rule_idle_timeout = learn_timeout + self.cache_update_guard_time
        return (src_rule_idle_timeout, src_rule_hard_timeout, dst_rule_idle_timeout)

    def _src_rule_expire(self, vlan, port, eth_src):
        """When a src rule expires, the host is probably inactive or active in
        receiving but not sending. We mark just mark the host as expired."""
        ofmsgs = []
        entry = vlan.cached_host_on_port(eth_src, port)
        if entry is not None:
            vlan.expire_cache_host(eth_src)
            self.logger.info('expired src_rule for host %s' % eth_src)
        return ofmsgs

    def _dst_rule_expire(self, now, vlan, eth_dst):
        """Expiring a dst rule may indicate that the host is actively sending
        traffic but not receving. If the src rule not yet expires, we reinstall
        host rules."""
        ofmsgs = []
        entry = vlan.cached_host(eth_dst)
        if entry is not None:
            ofmsgs.extend(self.learn_host_on_vlan_ports(
                now, entry.port, vlan, eth_dst, delete_existing=False))
            self.logger.info(
                'refreshing host %s from VLAN %u' % (eth_dst, vlan.vid))
        return ofmsgs
