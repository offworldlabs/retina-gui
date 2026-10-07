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


def _include_summary():
    """Whether this run tours the Summary page.

    Only with more than one node, or in demo mode so the whole tour can be
    reviewed on a single node. See docs/features/tutorial.md#which-steps-run.
    """
    from routes.fleet import discovered_nodes

    return _demo() or len(discovered_nodes()) > 1


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
    return redirect(tutorial.first_url(_include_summary(), _demo()))


def tutorial_context():
    """The tour payload for this request's page, or None when no tour is running."""
    step_arg = request.args.get("tutorial")
    if step_arg is None:
        return None
    return tutorial.payload(request.path, step_arg, _include_summary(), _demo())
