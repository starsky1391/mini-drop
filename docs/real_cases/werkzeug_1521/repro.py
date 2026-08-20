#!/usr/bin/env python3
"""持续复现 Werkzeug #1521，并输出有界的结构化观测。"""

import argparse
import gc
import json
import os
import resource
import time
import types

from werkzeug import routing


def object_counts():
    counts = {"Map": 0, "Rule": 0, "BaseConverter": 0}
    for obj in gc.get_objects():
        if isinstance(obj, routing.Map):
            counts["Map"] += 1
        elif isinstance(obj, routing.Rule):
            counts["Rule"] += 1
        elif isinstance(obj, routing.BaseConverter):
            counts["BaseConverter"] += 1
    return counts


def retained_bound_method():
    rule_map = routing.Map([routing.Rule("/a/<string:b>")])
    rule = next(iter(rule_map.iter_rules()))
    return any(isinstance(value, types.MethodType) for value in rule._build.__code__.co_consts)


def emit(event, iteration, created):
    payload = {
        "event": event,
        "pid": os.getpid(),
        "iteration": iteration,
        "created_maps": created,
        "rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "gc_counts": object_counts(),
        "bound_method_in_co_consts": retained_bound_method(),
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print(json.dumps(payload, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--iterations", type=int, default=1800)
    parser.add_argument("--hold-seconds", type=int, default=300)
    parser.add_argument("--report-every", type=int, default=25)
    args = parser.parse_args()

    emit("start", 0, 0)
    created = 0
    for iteration in range(1, args.iterations + 1):
        for index in range(args.batch_size):
            routing.Map([routing.Rule("/route-{}/<string:value>".format(index))])
        created += args.batch_size
        gc.collect()
        if iteration == 1 or iteration % args.report_every == 0:
            emit("sample", iteration, created)
        time.sleep(args.interval)

    emit("generation_complete", args.iterations, created)
    deadline = time.time() + args.hold_seconds
    while time.time() < deadline:
        gc.collect()
        emit("hold", args.iterations, created)
        time.sleep(min(10, max(0.1, deadline - time.time())))
    emit("complete", args.iterations, created)


if __name__ == "__main__":
    main()
