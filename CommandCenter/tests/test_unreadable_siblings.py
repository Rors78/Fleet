"""Unreadable position state must ARM, in every bot — not just TurtleSue.

TurtleSue._load_positions distinguishes absent / empty / UNREADABLE, sets
_state_unreadable, skips its orphan sweep, blocks new entries and preserves
the bytes. That defence was written after the failure it names: an empty
positions dict makes the orphan sweep release EVERY reservation booked to
the bot while the positions stay open with no capital behind them.

Its siblings were never given the same treatment (audit 2026-08-13):

  NexusBrain._load_positions   `except Exception: saved = {}` — identical to
                               the FileNotFoundError branch. Its orphan sweep
                               (nexus_brain.py) then releases every pool
                               reservation booked to nexusbrain.

  Arbitrageur._load_positions  `except Exception: log.warning(...)` — leaves
                               open_positions empty, and the scan loop's
                               lease heartbeat then declares an EMPTY rid list
                               to the pool. Command Center's confirm() treats
                               an empty declaration as "holds nothing" and
                               sweeps the lot. CC's own defence is its
                               active_positions cross-check, which reads what
                               the bot REPORTS — also nothing after a corrupt
                               load. Both layers fail together.

This pins the ported defence in both bots by SOURCE (the consumers are deep
inside scan loops that need live HTTP, so behavioural execution here would
test a reimplementation rather than the shipped path — see the pin comments).
"""
import re
import sys

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


BOTS = {
    'nexusbrain': ('D:/NexusBrain/nexus_brain.py', '_POSITIONS_FILE'),
    'arbitrageur': ('D:/Arbitrageur/arbitrageur.py', '_positions_file'),
}
SRC = {}
for _name, (_p, _) in BOTS.items():
    SRC[_name] = open(_p, encoding='utf-8', errors='replace').read()

TURTLE = open('D:/TurtleSue/turtlebot.py', encoding='utf-8',
              errors='replace').read()

# ── 0. The reference implementation must still be intact ──
check('self._state_unreadable = True' in TURTLE,
      'TurtleSue lost the reference _state_unreadable pattern the siblings '
      'are modelled on')

for name, src in SRC.items():
    # ── 1. The three cases must be distinguishable ──
    check('_state_unreadable = True' in src,
          '[%s] an unreadable position file must set _state_unreadable — '
          'without it, corrupt is indistinguishable from a first run' % name)
    # Absent must be handled on a DIFFERENT path from unreadable. Two valid
    # spellings are in use: an `except FileNotFoundError` branch
    # (nexusbrain) or an os.path.exists early-return before the try
    # (arbitrageur). Assert the PROPERTY, not one spelling.
    check(('except FileNotFoundError' in src
           or re.search(r'if not os\.path\.exists\([^)]*\):\s*\n\s*return', src)
           is not None),
          '[%s] absent must be handled SEPARATELY from unreadable; one path '
          'for both is the collapse itself' % name)

    # ── 2. It must be loud. A silent arm is its own failure mode. ──
    check(re.search(r'log(ger)?\.error\(\s*\n?\s*["\']UNREADABLE POSITION STATE',
                    src) is not None,
          '[%s] an unreadable state file must log at ERROR — a warning about '
          'lost positions is not a warning about a disarmed safety gate'
          % name)

    # ── 3. The bytes must be preserved before anything overwrites them ──
    check('corrupt_%d' in src,
          '[%s] the unparseable file must be quarantined — it is the only '
          'record of what was open' % name)

    # ── 4. Entries must be BLOCKED, not merely logged ──
    check(re.search(r'if getattr\(self, "_state_unreadable", False\):\s*\n\s*return',
                    src) is not None,
          '[%s] new entries must be refused while state is unknown — the bot '
          'cannot tell "flat" from "holding positions it forgot"' % name)

# ── 5. The specific capital-releasing consumer in each bot must be gated ──
# NexusBrain: the orphan sweep.
_nb = SRC['nexusbrain']
_sweep = _nb.find('Orphan sweep:')
_release = _nb.find('self._portfolio.release(rid, pnl=0.0)', _sweep)
_guard = _nb.find('if self._state_unreadable:', _sweep)
check(_sweep != -1 and _release != -1 and _guard != -1 and _guard < _release,
      '[nexusbrain] the orphan sweep must be SKIPPED when state is '
      'unreadable — an empty local rid set is not evidence the pool holds '
      'orphans')

# NexusBrain: must not overwrite the quarantined state with an empty file.
check(re.search(r'if not self\._state_unreadable:\s*\n\s*self\._save_positions\(\)',
                _nb) is not None,
      '[nexusbrain] _save_positions must not run after an unreadable load — '
      'it would overwrite the only remaining record with an empty file')

# Arbitrageur: the lease heartbeat.
_ab = SRC['arbitrageur']
check(re.search(r'if self\._portfolio and not getattr\(self, "_state_unreadable", False\):\s*\n\s*self\._portfolio\.confirm_reservations',
                _ab) is not None,
      '[arbitrageur] the lease heartbeat must not declare an empty rid list '
      'while state is unknown — CC reads that as "holds nothing" and sweeps '
      'every reservation booked to this bot')

# ── 6. The genuine first-run path must still work (no false arming) ──
# A bot that has never traded must NOT arm — that would refuse to ever start.
# nexusbrain: the FileNotFoundError branch must not set the flag.
_m = re.search(r'except FileNotFoundError:\s*\n\s*(.+)', _nb)
check(_m is not None and '_state_unreadable = True' not in _m.group(1),
      '[nexusbrain] a genuinely ABSENT file is a first run and must NOT arm '
      '— arming here would block a new bot from ever opening a position')
# arbitrageur: the exists-check returns BEFORE the flag could be set, and
# the flag is initialised False on the line above it.
check(re.search(r'self\._state_unreadable = False\s*\n\s*if not os\.path\.exists',
                _ab) is not None,
      '[arbitrageur] the absent-file early return must come AFTER the flag '
      'is cleared, so a first run leaves the bot disarmed and able to trade')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  unreadable state arms in nexusbrain and arbitrageur, first run still runs')
