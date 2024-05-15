import json
import ipaddress
from contextlib import suppress
from radixtarget import RadixTarget

from bbot.modules.base import BaseModule


class portscan(BaseModule):
    flags = ["active", "portscan"]
    watched_events = ["IP_ADDRESS", "IP_RANGE", "DNS_NAME"]
    produced_events = ["OPEN_TCP_PORT"]
    meta = {"description": "Port scan with masscan. By default, scans top 100 ports."}
    options = {
        "top_ports": 100,
        "ports": "",
        # ping scan at 600 packets/s ~= private IP space in 8 hours
        "rate": 600,
        "wait": 5,
        "ping_first": False,
        "ping_only": False,
    }
    options_desc = {
        "top_ports": "Top ports to scan (default 100) (to override, specify 'ports')",
        "ports": "Ports to scan",
        "rate": "Rate in packets per second",
        "wait": "Seconds to wait for replies after scan is complete",
        "ping_first": "Only portscan hosts that reply to pings",
        "ping_only": "Ping sweep only, no portscan",
    }
    deps_shared = ["masscan"]
    batch_size = 1000000

    async def setup(self):
        self.top_ports = self.config.get("top_ports", 100)
        self.rate = self.config.get("rate", 600)
        self.wait = self.config.get("wait", 10)
        self.ping_first = self.config.get("ping_first", False)
        self.ping_only = self.config.get("ping_only", False)
        self.ports = self.config.get("ports", "")
        if self.ports:
            try:
                self.helpers.parse_port_string(self.ports)
            except ValueError as e:
                return False, f"Error parsing ports: {e}"
        self.alive_hosts = dict()
        self.scanned_tracker = RadixTarget()
        self.prep_blacklist()
        self.helpers.depsinstaller.ensure_root(message="Masscan requires root privileges")
        return True

    async def handle_batch(self, *events):
        targets = [str(h) for h in self.make_targets(events)]

        # ping scan
        if self.ping_first or self.ping_only:
            new_targets = []
            async for alive_host, _ in self.masscan(targets, ping=True):
                source_event = self.scanned_tracker.search(alive_host)
                if source_event.type != "DNS_NAME":
                    await self.emit_event(alive_host, "IP_ADDRESS", source=source_event)
                new_targets.append(ipaddress.ip_network(alive_host, strict=False))
            targets = new_targets

        # TCP SYN scan
        if not self.ping_only:
            async for host, port in self.masscan(targets):
                source_event = self.scanned_tracker.search(host)
                if source_event.type == "DNS_NAME":
                    host = source_event.host
                netloc = self.helpers.make_netloc(host, port)
                await self.emit_event(netloc, "OPEN_TCP_PORT", source=source_event)
        else:
            self.verbose("Only ping sweep was requested, skipping TCP SYN scan")

    async def masscan(self, targets, ping=False):
        scan_type = "ping" if ping else "SYN"
        self.verbose(f"Starting masscan {scan_type} scan")
        if not targets:
            self.verbose("No targets specified, aborting.")
            return

        target_file = self.helpers.tempfile(targets, pipe=False)
        command = self._build_masscan_command(target_file, ping=ping)
        stats_file = self.helpers.tempfile_tail(callback=self.verbose)
        try:
            with open(stats_file, "w") as stats_fh:
                async for line in self.run_process_live(command, sudo=True, stderr=stats_fh):
                    for host, port in self.parse_json_line(line):
                        yield host, port
        finally:
            for file in (stats_file, target_file):
                file.unlink()

    def _build_masscan_command(self, target_file=None, dry_run=False, ping=False):
        command = (
            "masscan",
            "--excludefile",
            str(self.exclude_file),
            "--rate",
            self.rate,
            "--wait",
            self.wait,
            "--open-only",
            "-oJ",
            "-",
        )
        if target_file is not None:
            command += ("-iL", str(target_file))
        if ping:
            command += ("--ping",)
        else:
            if self.ports:
                command += ("-p", self.ports)
            else:
                command += ("--top-ports", str(self.top_ports))
        if dry_run:
            command += ("--echo",)
        return command

    def make_targets(self, events):
        # convert events into a list of targets, skipping ones that have already been scanned
        targets = set()
        for e in events:
            # skip events without host
            if not e.host:
                continue
            # skip events that we already scanned
            if self.scanned_tracker.search(e.host):
                self.debug(f"Skipping {e.host} because it was already scanned")
                continue
            try:
                # first assume it's an ip address / ip range
                host = ipaddress.ip_network(e.host, strict=False)
                targets.add(host)
                self.scanned_tracker.insert(host, e)
            except Exception:
                # if it's a hostname, get its IPs from resolved_hosts
                hosts = set()
                for h in e.resolved_hosts:
                    try:
                        h = ipaddress.ip_network(h, strict=False)
                        hosts.add(h)
                    except Exception:
                        continue
                for h in hosts:
                    targets.add(h)
                    self.scanned_tracker.insert(h, e)
        return targets

    def parse_json_line(self, line):
        try:
            j = json.loads(line)
        except Exception:
            return
        ip = j.get("ip", "")
        if not ip:
            return
        ports = j.get("ports", [])
        if not ports:
            return
        for p in ports:
            proto = p.get("proto", "")
            port_number = p.get("port", 0)
            if proto == "" or port_number == "":
                continue
            yield ip, port_number

    def prep_blacklist(self):
        exclude = []
        for t in self.scan.blacklist:
            t = self.helpers.make_ip_type(t.data)
            if not isinstance(t, str):
                if self.helpers.is_ip(t):
                    exclude.append(str(ipaddress.ip_network(t)))
                else:
                    exclude.append(str(t))
        if not exclude:
            exclude = ["255.255.255.255/32"]
        self.exclude_file = self.helpers.tempfile(exclude, pipe=False)

    async def cleanup(self):
        with suppress(Exception):
            self.exclude_file.unlink()
