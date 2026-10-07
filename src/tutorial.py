"""The guided tutorial: what it points at, in what order, and what it says.

Pure data and URL building, no Flask. See docs/features/tutorial.md.
"""

SUMMARY, HOME, CONFIG = "/summary", "/", "/config"

# Where the Controller step sends an owner who wants every view explained.
NODE_DATA_WIKI_URL = "https://github.com/offworldlabs/owl-os/wiki/3-Understanding-Node-Data"
# Where the closing step sends an owner who wants to read more.
WIKI_URL = "https://github.com/offworldlabs/owl-os/wiki"


def _target(name, arrow=True):
    """One highlighted region: every element whose data-tutorial lists `name`."""
    return {"name": name, "arrow": arrow}


def _image(name, width, height, alt, marks=()):
    """An example image. The size is the file's own, so the card can be laid
    out before the image arrives; tests/test_tutorial.py checks it matches.

    `marks` are (x, y) points in the image's own pixels that the step points
    at. See docs/features/tutorial.md#example-images.
    """
    return {"url": f"/static/tutorial/{name}", "width": width, "height": height, "alt": alt,
            "marks": [{"x": x, "y": y} for x, y in marks]}


def _radar(marks):
    """The Passive Radar capture, pointing at `marks`. Shared by its three steps."""
    return _image("passive-radar.jpg", 1328, 631,
                  "The Passive Radar display: white and orange dots on a delay-Doppler map",
                  marks)


def _step(page, title, body, targets, image=None, link=None, click=False, closing=False):
    """One step. `click` makes it a hand-over: the owner clicks the lit thing
    itself to go on. See docs/features/tutorial.md#click-through-steps.
    `closing` marks the step every run ends on, whatever it toured.
    See docs/features/tutorial.md#which-steps-run."""
    return {
        "page": page,
        "title": title,
        "body": body,
        "targets": [t if isinstance(t, dict) else _target(t) for t in targets],
        "image": image,
        "link": link,
        "click": click,
        "closing": closing,
    }


# Order is the tour. Each body is a sentence or two, and each target name must exist
# as a data-tutorial token on its page: tests/test_tutorial.py checks both.
# See docs/features/tutorial.md#steps.
STEPS = (
    # ── Summary: only toured when there is more than one node ──
    _step(SUMMARY, "List of nodes",
          "Every node on your network is listed here and in the tabs at the top, "
          "and clicking either one opens that node.",
          ["summary-nodes", "fleet-tabs"]),
    _step(SUMMARY, "Resources",
          "These links open the Offworld Labs site, the Retina Wiki, the Network "
          "Map and Dashboard, Passive Radar News and the blah2 community Discord.",
          ["summary-resources"]),
    _step(SUMMARY, "If you need help",
          "When something is not working, ask on the Retina Discord or email us "
          "from here.",
          ["summary-help"]),
    _step(SUMMARY, "Doppler Mapping Tool",
          "Draw a flight path across the sky and press Play to get a feel for the "
          "range and Doppler your node records as an aircraft passes.",
          ["summary-sim"]),
    _step(SUMMARY, "Open your node",
          "Click this node's card or its tab in the bar at the top to continue to "
          "its Home page.",
          ["summary-this-node", "fleet-this-tab"], click=True),

    # ── Home ──
    # Says "highlighted", never "first": tabs keep one fixed order on every
    # node. See docs/features/fleet-and-naming.md#fleet-order.
    _step(HOME, "Welcome to your node",
          "Welcome to this node's Home page, headed with the name of its site, "
          "and the highlighted tab at the top always shows which node you are "
          "looking at.",
          ["home-head", "fleet-this-tab"]),
    _step(HOME, "System information",
          "Listening on names the broadcast tower this node is tuned to, and "
          "Telemetry shows whether it is streaming and the identifier you use to "
          "find its data.",
          ["home-system"]),
    # Passive Radar takes three steps on one picture, each pointing at dots in it.
    _step(HOME, "Passive Radar: detections",
          "Opens the live radar display, where each white dot is an object your "
          "node has detected.",
          ["svc-radar"],
          image=_radar([(983, 513), (1189, 363)])),
    _step(HOME, "Passive Radar: expected flights",
          "Each orange dot is where a flight is expected to appear, worked out "
          "from its ADS-B position.",
          ["svc-radar"],
          image=_radar([(591, 173), (481, 459)])),
    _step(HOME, "Passive Radar: do they line up?",
          "The goal is for the white and orange dots to line up, but a miss does "
          "not always mean a problem, because the antenna's cone may not face "
          "that aircraft.",
          ["svc-radar"],
          image=_radar([(660, 280), (1002, 232)])),
    _step(HOME, "Max-Hold",
          "Opens the same display holding the previous 10 seconds of detection "
          "data, so a moving object leaves a trail.",
          ["svc-maxhold"],
          image=_image("max-hold.jpg", 1257, 570,
                       "The Max-Hold display: the same map with echoes drawn out into trails")),
    _step(HOME, "ADS-B Map",
          "Opens a live map of nearby aircraft, best read with Max-Hold open "
          "beside it while you think about which way your antenna is facing.",
          ["svc-adsb"],
          image=_image("adsb-map.jpg", 1045, 666,
                       "The ADS-B Map: aircraft drawn on a map around the node")),
    _step(HOME, "Tracker",
          "Opens the Tracker, which keeps up to 4 hours of data and lets you sort "
          "by detections not associated with a track, tracks below the SNR "
          "threshold, ADS-B verified tracks, and more.",
          ["svc-tracker"],
          image=_image("tracker.jpg", 1328, 547,
                       "The Tracker: tracks drawn across a delay-Doppler plot, with its filters")),
    _step(HOME, "Controller",
          "Opens the full list of radar views for a few more specialised "
          "displays, and the wiki explains what each one shows.",
          ["svc-controller"],
          link={"label": "Understanding Node Data", "url": NODE_DATA_WIKI_URL}),
    _step(HOME, "Open Configuration",
          "Click Config in the bar under the node tabs to continue to the "
          "Configuration page.",
          ["subnav-config"], click=True),

    # ── Configuration ──
    # After the welcome and Apply steps these follow the page from top to bottom.
    _step(CONFIG, "Welcome to Configuration",
          "Here you tune the radar and manage the node, with every section listed "
          "in the menu on the left. The tour skips the sections you already "
          "completed during setup, but they can all be found here.",
          ["subnav-config", _target("cfg-head", arrow=False), "cfg-side"]),
    # Says "a setting on this page" though only the Radar and ADS-B groups
    # need Apply. See docs/features/tutorial.md#steps.
    _step(CONFIG, "Apply your changes",
          "When you change a setting on this page, press Apply changes before "
          "you leave, and allow 30 seconds to a minute for the change to take "
          "effect.",
          ["cfg-apply"]),
    _step(CONFIG, "Mode",
          "Switch the node between Radar, a live Spectrum analyser and SDRconnect "
          "for a desktop SDR app; the radar pauses in the other two.",
          ["cfg-mode-nav", _target("cfg-mode", arrow=False)]),
    _step(CONFIG, "Wi-Fi network",
          "Shows the Ethernet and Wi-Fi connection and is where you choose the "
          "Wi-Fi network this node joins.",
          ["cfg-network-nav", _target("cfg-network", arrow=False)]),
    _step(CONFIG, "Cached Towers",
          "Lists the broadcast towers found near you during setup, where picking "
          "one fills in the Tower section below and you can add or remove towers "
          "by hand.",
          ["cfg-cached-nav", _target("cfg-cached", arrow=False)]),
    _step(CONFIG, "Tower",
          "Holds the tower the radar listens to, with its position and center "
          "frequency, alongside where your receiver stands.",
          ["cfg-tower-nav", _target("cfg-tower", arrow=False)]),
    # Capture takes four steps and Tracking a fifth: together they are how an
    # owner tunes the node. See docs/features/tutorial.md#tuning-steps.
    _step(CONFIG, "Capture: signal peak",
          "Signal peak shows how strongly each tuner is receiving the tower, in "
          "dBFS, and you want it as high as you can get it without overloading "
          "it.",
          ["cfg-peak", _target("cfg-capture-nav", arrow=False)],
          image=_image("signal-peak.png", 786, 169,
                       "The Signal peak meter with both tuners in the green and amber, near -9 dBFS")),
    _step(CONFIG, "Capture: overload",
          "Push the signal too high and the radio overloads: the reading turns "
          "to a red overload, and a box under the meter offers Quick Calibrate "
          "or the safest settings.",
          ["cfg-peak", _target("cfg-capture-nav", arrow=False)],
          image=_image("signal-overload.png", 774, 167,
                       "The Signal peak meter with one tuner reading overload in red")),
    # Two steps, not one: the buttons and the gain rows are too far apart to
    # both be on screen in a short window.
    _step(CONFIG, "Capture: automatic calibration",
          "The easiest way to get there is an automatic calibration: Quick "
          "Calibrate finds the best gain for your current tower, and "
          "Auto-Calibrate also tries other towers, then waits for a flight "
          "overhead and tunes against it.",
          ["cfg-calibrate", _target("cfg-capture-nav", arrow=False)]),
    _step(CONFIG, "Capture: manual settings",
          "Or experiment by hand with the Reference and Surveillance Gain "
          "Reduction settings, the Low Noise Amplifier State and the DAB and RF "
          "notch filters, applying each change and watching Signal peak.",
          ["cfg-manual", _target("cfg-capture-nav", arrow=False)]),
    _step(CONFIG, "Tracking: Minimum SNR",
          "Experiment with Minimum SNR too, which sets how strong a detection "
          "must be before the tracker will use it. The default is 7, even when "
          "the box is blank.",
          ["cfg-tracking-nav", _target("cfg-tracking", arrow=False)]),
    _step(CONFIG, "ADS-B settings",
          "ADS-B Truth and tar1090 choose where aircraft positions come from, so "
          "radar detections can be matched against known flights.",
          ["cfg-adsb-nav", _target("cfg-adsb", arrow=False)]),
    _step(CONFIG, "SSH Access",
          "Lists the public keys allowed to log in to this node from a terminal, "
          "and is where you add your own.",
          ["cfg-ssh-nav", _target("cfg-ssh", arrow=False)]),

    # ── Back on Summary to close ──
    # Every run ends here, a single node's too: it is where the links are.
    _step(SUMMARY, "That's everything",
          "That's everything! To learn more, visit our GitHub wiki, and don't "
          "forget to reach out to us if you have issues or more questions.",
          ["summary-resources", "summary-help"],
          link={"label": "Retina Wiki", "url": WIKI_URL}, closing=True),
)


def steps_for(include_summary):
    """The steps of one run: everything, or everything but Summary's opening
    steps. The closing step, also on Summary, is in every run."""
    return [s for s in STEPS if include_summary or s["page"] != SUMMARY or s["closing"]]


def step_url(page, number, demo=False):
    """Where step `number` (1-based) lives."""
    return f"{page}?tutorial={number}" + ("&demo=1" if demo else "")


def first_url(include_summary, demo=False):
    """Where a run starts."""
    return step_url(steps_for(include_summary)[0]["page"], 1, demo)


def payload(path, step_arg, include_summary, demo=False):
    """What the page needs to run the tour from this request, or None.

    None unless `step_arg` names a step that lives on `path`, so a stale or
    hand-edited link renders the plain page instead of a tour pointing at
    things that are not there.
    """
    steps = steps_for(include_summary)
    try:
        number = int(step_arg)
    except (TypeError, ValueError):
        return None
    if not 1 <= number <= len(steps) or steps[number - 1]["page"] != path:
        return None

    return {
        "current": number - 1,
        "steps": [dict(s, url=step_url(s["page"], i, demo))
                  for i, s in enumerate(steps, start=1)],
        # Finishing stays on Summary, whose links the closing step has just
        # pointed at.
        "exit_url": SUMMARY + ("?demo=1" if demo else ""),
    }
