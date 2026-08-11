"""Vulture dead-code whitelist.

Read by tools/check-dead-code.sh. Every name here is one vulture reports as
dead but which must not be deleted — or which nobody has decided about yet.
The distinction matters, so the two live in separate sections.

Add to CONTRACTS only when the name is genuinely referenced by something
vulture cannot see: a framework calling in, a wire format, a config key. Real
dead code should be deleted, not whitelisted.

The UNREVIEWED section is a backlog, not an exemption. Each entry is code that
appears genuinely unreachable and needs a decision — delete it, or wire up
whatever was left unfinished. The gate is green with these listed so that it
starts catching NEW dead code immediately; working through them is separate.
"""
# ruff: noqa: B018, F821
# B018 — bare-name expressions are how vulture whitelists work.
# F821 — these names are defined in other modules; only vulture reads this file.

_ = type("_", (), {})()

# ── Contracts: referenced by something vulture cannot see ─────────────────────
# CSRFProtect(app) registers on construction; the name is conventional
#   src/app.py:44
csrf

# ── UNREVIEWED: appears dead, needs a decision (delete, or finish wiring) ──────
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:161  (unused variable)
adsb2dd
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:189  (unused variable)
adsb_source_host
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:191  (unused variable)
adsb_source_protocol
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:192  (unused variable)
adsblol_fallback
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:193  (unused variable)
adsblol_radius
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:131  (unused variable)
device_bandwidthNumber
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:129  (unused variable)
device_dabNotch
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:130  (unused variable)
device_rfNotch
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:163  (unused variable)
doppler_tolerance
# TODO: helper function with no callers found
#   src/config_schema.py:64  (unused function)
get_nested_value
# TODO: no reference found anywhere in the estate
#   src/tracker_capture.py:80  (unused variable)
length
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:145  (unused variable)
rx_altitude
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:144  (unused variable)
rx_longitude
# TODO: helper function with no callers found
#   src/config_schema.py:75  (unused function)
set_nested_value
# TODO: no reference found anywhere in the estate
#   src/retina_tracker_client.py:110  (unused method)
_.stop
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:149  (unused variable)
tx_altitude
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:147  (unused variable)
tx_latitude
# TODO: config field: parsed but no reader found — wire it up or drop it
#   src/config_schema.py:148  (unused variable)
tx_longitude
