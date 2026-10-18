#!/usr/bin/env python3
"""Prepare an Xray config with mainland China IP bypass; never overwrite the running config."""
import argparse
import copy
import json
import os
from pathlib import Path


def bypass_cn(config, proxy_tag=None, direct_tag="direct", *, balancer_tag=None):
    out = copy.deepcopy(config)
    outbounds = {o.get("tag"): o for o in out.get("outbounds", [])}
    if direct_tag not in outbounds or outbounds[direct_tag].get("protocol") != "freedom":
        raise ValueError(f"{direct_tag!r} must name an existing freedom outbound")
    proxies = {tag for tag, o in outbounds.items() if tag and o.get("protocol")
               and o["protocol"] not in ("freedom", "blackhole", "dns", "loopback")}
    routing = out.setdefault("routing", {})
    balancers = {b.get("tag") for b in routing.get("balancers", [])}
    if bool(proxy_tag) == bool(balancer_tag):
        raise ValueError("select exactly one proxy outbound or balancer")
    if proxy_tag and proxy_tag not in proxies:
        raise ValueError(f"{proxy_tag!r} must name an existing proxy outbound")
    if balancer_tag and balancer_tag not in balancers:
        raise ValueError(f"{balancer_tag!r} must name an existing balancer")
    if routing.get("script"):
        raise ValueError("custom routing scripts require reviewing the live config first")
    # Keep internal API, DNS and blocking rules in their original order. Replace the old
    # domain lists and catch-all routes, which otherwise override IP-based bypass.
    controls = {tag for tag, o in outbounds.items() if o.get("protocol") in ("dns", "blackhole")}
    api_tag = out.get("api", {}).get("tag")
    if api_tag:
        controls.add(api_tag)
    retained, exceptions = [], []
    for rule in routing.get("rules", []):
        target = rule.get("outboundTag")
        balance = rule.get("balancerTag")
        if bool(target) == bool(balance) or (balance and balance not in balancers):
            raise ValueError("a routing rule with an unknown or ambiguous target requires manual review")
        if target and target not in controls | proxies | {direct_tag}:
            raise ValueError(f"route to {target!r} requires manual review; candidate was not written")
        if target in controls or rule.get("inboundTag"):
            retained.append(rule)
        elif target != direct_tag:
            # Keep explicit proxy choices (e.g. OpenAI's working exit), after China IP bypass.
            # Old catch-all rules must go so the selected default is authoritative.
            conditions = set(rule) - {"type", "ruleTag", "outboundTag", "balancerTag", "network"}
            if conditions:
                exceptions.append(rule)
    routing["domainStrategy"] = "IPOnDemand"
    default = {"balancerTag": balancer_tag} if balancer_tag else {"outboundTag": proxy_tag}
    routing["rules"] = retained + [
        {"type": "field", "ip": ["geoip:private", "geoip:cn"], "outboundTag": direct_tag},
    ] + exceptions + [{"type": "field", "network": "tcp,udp", **default}]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="the active standalone JSON config")
    parser.add_argument("candidate", type=Path, help="a new file; must not already exist")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--proxy-tag", help="existing proxy outbound tag")
    target.add_argument("--balancer-tag", help="existing balancer tag (preserves node selection)")
    parser.add_argument("--direct-tag", default="direct")
    args = parser.parse_args()
    try:
        config = bypass_cn(json.loads(args.source.read_text()), args.proxy_tag, args.direct_tag,
                           balancer_tag=args.balancer_tag)
        # Configs contain subscription credentials. Exclusive creation also prevents overwriting
        # the live config (including through a symlink) or a previous candidate.
        fd = os.open(args.candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"Candidate written to {args.candidate}; validate with Xray before installing it.")


if __name__ == "__main__":
    main()
