"""The guided tutorial's entry point and per-request context.

The tour itself is drawn by static/tutorial.js over the real pages; this
module only decides where it starts and which steps a run has.
See docs/features/tutorial.md.
"""

from flask import Blueprint, redirect, request

import tutorial

bp = Blueprint("tutorial", __name__)


def _demo():
    return request.args.get("demo") == "1"


def _starting_run_tours_summary():
    """Whether a run started right now should tour the Summary page.

    Only with more than one node, or in demo mode so the whole tour can be
    reviewed on a single node. Asked once, by `start`.
    See docs/features/tutorial.md#which-steps-run.
    """
    from routes.fleet import discovered_nodes

    return _demo() or len(discovered_nodes()) > 1


def _include_summary():
    """Whether the run this request belongs to tours the Summary page.

    Read from the URL rather than worked out again. A step number is a position
    in the run's own step list, which is 28 steps long with Summary and 23
    without, so asking the fleet again on every request would renumber a tour
    under its owner the moment a peer appeared or dropped.
    See docs/features/tutorial.md#which-steps-run.
    """
    return _demo() or request.args.get("summary") == "1"


@bp.route("/tutorial")
def start():
    """Send the browser to the first step.

    See docs/features/tutorial.md#starting-a-run.
    """
    from app import device_state

    # Home and Config both bounce to the wizard while it is in progress, so a
    # tour started then would lose its place on the first page change.
    if device_state.is_setup_wizard_in_progress():
        return redirect("/set-up")
    return redirect(tutorial.first_url(_starting_run_tours_summary(), _demo()))


def tutorial_context():
    """The tour payload for this request's page, or None when no tour is running."""
    step_arg = request.args.get("tutorial")
    if step_arg is None:
        return None
    return tutorial.payload(request.path, step_arg, _include_summary(), _demo(),
                            owner_name=_owner_name())


def _owner_name():
    """Whom the welcome greets: the contact record's first name, or nothing.
    See docs/features/tutorial.md#the-welcome."""
    from app import device_state

    contact = device_state.get_telemetry_contact() or {}
    return contact.get("first_name") or ""
