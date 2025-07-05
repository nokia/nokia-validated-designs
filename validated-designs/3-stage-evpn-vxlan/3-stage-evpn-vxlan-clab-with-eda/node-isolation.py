# © 2025 Nokia  
# Licensed under the BSD 3-Clause License  
# SPDX-License-Identifier: BSD-3-Clause  

import sys
import json

# count_bgp_sessions_established returns the number of monitored BGP sessions that are established  {established=up}
def count_bgp_sessions_established(paths):
    up_cnt = 0
    for path in paths:
        if path.get("value") == "established":
            up_cnt = up_cnt + 1
    return up_cnt

# required_bgp_sessions_established returns the value of the `required-bgp-sessions-established` option
def required_bgp_sessions_established(options):
    return int(options.get("required-bgp-sessions-established", 1))

# hold down timer after recovery
def hold_time(options):
    return int(options.get('hold-down-time', '0'))

def bool_to_oper_state(val):
    return ('down','up')[bool(val)]

# main entry function for event handler
def event_handler_main(in_json_str):
    # parse input json string passed by event handler
    in_json = json.loads(in_json_str)
    paths = in_json["paths"]
    options = in_json["options"]
    persist = in_json.get('persistent-data', {})

    num_up_bgp_sessions = count_bgp_sessions_established(paths)
    downlink_should_be_up = required_bgp_sessions_established(options) <= num_up_bgp_sessions
    needs_hold_down = False

    # down->up transition will be held for optional hold-time
    if (hold_time(options) > 0) and downlink_should_be_up:
        needs_hold_down = persist.get("last-state", "up") == "down"

    if options.get("debug") == "true":
        print(
            f"hold down time = {hold_time(options)}ms\n\
num of required bgp_sessions = {required_bgp_sessions_established(options)}\n\
detected num of bgp_sessions = {num_up_bgp_sessions}\n\
downlinks new state = {bool_to_oper_state(downlink_should_be_up)}\n\
needs_hold_down = {str(needs_hold_down)}"
        )

    response_actions = []

    oper_state_str = bool_to_oper_state(not needs_hold_down and downlink_should_be_up)
    for downlink in options.get('down-links'):
        response_actions.append({'set-ephemeral-path' : {'path':'interface {0} oper-state'.format(downlink),'value':oper_state_str}})

    if needs_hold_down:
        response_actions.append({'reinvoke-with-delay' : hold_time(options)})
    response_persistent_data = {'last-state':bool_to_oper_state(downlink_should_be_up)}

    response = {'actions':response_actions,'persistent-data':response_persistent_data}
    return json.dumps(response)
