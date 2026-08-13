"""A reaction whose message can't be filled in must not page subscribers.

Subscriber-facing Telegram cards read:

    HIGH CONVICTION SIGNAL
    Pair      (empty)
    Detail    Fleet convergence on
    Conviction 91%

Traced to a self-referential chain, none of which involves a bot proposing
a trade:

    STRUCTURE_FORMING -> (dissipative_structure_forming rule)
      -> SIGNAL  {type, reason, suggested_action}   <- no pair, no direction
        -> (convergent_signal rule, templating {data.pair}/{data.direction})
          -> HIGH_CONVICTION "Fleet convergence on  "
            -> Telegram card

Measured on the durable bus, 24h: 346 HIGH_CONVICTION events, 100% with NO
direction, 53% being the empty template. The root cause is that
_fire_reaction substituted `obj.get(p, "")` — a missing field became an
empty string with no signal that anything was wrong. Absent rendered as
empty, one layer deeper than the usual place.

The condition layer could NOT fix this: _evaluate_condition regex-matches
only `count(...) >= N` and returns False for anything else, so a guard like
"data.pair AND count(...)" would have made the rule permanently silent —
the same class of defect (a gate that cannot fire) in a new location. I
tried that first; this test exists partly to record why it is wrong.
"""
import json
import logging
import sys

sys.path.insert(0, 'D:/CommandCenter')

import event_bus as eb

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def fresh_bus():
    bus = eb.EventBus()
    sent = []
    bus.publish = lambda ev: sent.append(ev)
    return bus, sent


RULE = {
    "name": "convergent_signal", "action": "broadcast",
    "message": {"type": "HIGH_CONVICTION",
                "reason": "Fleet convergence on {data.pair} {data.direction}",
                "pair": "{data.pair}", "direction": "{data.direction}"},
}

# ── 1. The real broken case: trigger carries none of the templated fields ──
bus, sent = fresh_bus()
bus._fire_reaction(RULE, {"type": "SIGNAL", "id": "x", "data": {
    "type": "SIGNAL", "reason": "Dissipative structure forming",
    "suggested_action": "prepare_entry"}})
check(len(sent) == 0,
      'a reaction whose templates cannot be resolved must NOT fire — this is '
      'the "Fleet convergence on  " card, 184 of them a day; got %d event(s)'
      % len(sent))

# ── 2. A complete trigger must still fire, unchanged ──
bus, sent = fresh_bus()
bus._fire_reaction(RULE, {"type": "SIGNAL", "id": "y", "data": {
    "pair": "BTC/USD", "direction": "LONG"}})
check(len(sent) == 1,
      'a SIGNAL carrying pair AND direction must still produce the reaction '
      '— the fix must not silence legitimate convergence; got %d' % len(sent))
if sent:
    check(sent[0]["data"]["reason"] == "Fleet convergence on BTC/USD LONG",
          'the resolved message must be intact; got %r'
          % sent[0]["data"]["reason"])
    check(sent[0]["data"]["pair"] == "BTC/USD",
          'templated fields must carry real values')

# ── 3. A PARTIALLY resolvable message must not fire either ──
# Half a card is still a card that says something it cannot support.
bus, sent = fresh_bus()
bus._fire_reaction(RULE, {"type": "SIGNAL", "id": "z", "data": {
    "pair": "ETH/USD"}})          # direction missing
check(len(sent) == 0,
      'a message with SOME placeholders unresolved must not fire — "Fleet '
      'convergence on ETH/USD " is as misleading as the empty one; got %d'
      % len(sent))

# ── 4. Whitespace-only is empty ──
bus, sent = fresh_bus()
bus._fire_reaction(RULE, {"type": "SIGNAL", "id": "w", "data": {
    "pair": "   ", "direction": "LONG"}})
check(len(sent) == 0,
      'a whitespace-only value must count as unresolved, not as a pair name')

# ── 5. Templateless rules are untouched ──
bus, sent = fresh_bus()
bus._fire_reaction({"name": "static", "action": "broadcast",
                    "message": {"type": "FLEET_ALERT", "reason": "no fields"}},
                   {"type": "ANY", "id": "s", "data": {}})
check(len(sent) == 1,
      'a rule with no placeholders must fire normally — the guard must only '
      'affect messages that actually template something; got %d' % len(sent))

# ── 6. The reactions file itself must not route regime news as a signal ──
_rx = json.load(open('D:/CommandCenter/reactions.json', encoding='utf-8'))
_by = {r.get("name"): r for r in _rx.get("rules", [])}
_q = _by.get("quantum_collapse_convergence")
check(_q is not None, 'quantum_collapse_convergence rule is missing')
if _q:
    check(_q["message"]["type"] == "REGIME_CHANGE",
          'a quantum-collapse regime transition must not be typed '
          'HIGH_CONVICTION — it reached subscribers titled "HIGH CONVICTION '
          'SIGNAL" with a confidence number but no direction, entry or '
          'invalidation; got %r' % _q["message"]["type"])

# ── 7. The condition layer cannot express this guard — pin the reason ──
_ev = open('D:/CommandCenter/event_bus.py', encoding='utf-8',
           errors='replace').read()
check('count(' in _ev and 'if not m:' in _ev,
      '_evaluate_condition still regex-matches only count(...) forms, so a '
      'condition-based guard would silently never match — the guard belongs '
      'in template resolution, where this test exercises it')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  hollow reactions are skipped; complete ones still fire')
