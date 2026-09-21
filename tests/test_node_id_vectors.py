"""The node_id format, checked against the canonical vectors.

The shape of a node_id is hand-copied into four repositories in two languages,
which is how owl-os came to carry two derivations that disagreed. A shared
library across four repos is not realistic; a shared *artefact* is, so each repo
vendors the same vectors and checks itself against them.

`tests/node-id-test-vectors.json` is a verbatim copy of
`owl-os/configuration/mender/identity/node-id-test-vectors.json`. owl-os owns the
generator, so it owns the vectors. Re-copy the file rather than editing this one.
"""

import json
import pathlib

import pytest

from remote_access import _NODE_ID_RE

VECTORS = json.loads(
    (pathlib.Path(__file__).parent / "node-id-test-vectors.json").read_text()
)


def test_our_pattern_is_the_canonical_one():
    """A vendored copy that has drifted is worse than no copy, because it still
    looks authoritative."""
    assert _NODE_ID_RE.pattern == VECTORS["pattern"]


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["node_id"])
def test_valid_ids_match(case):
    assert _NODE_ID_RE.match(case["node_id"]), case["why"]


@pytest.mark.parametrize("case", VECTORS["invalid"], ids=lambda c: c["node_id"])
def test_invalid_ids_do_not_match(case):
    """Each of these would otherwise be compared against a hostname as though it
    were this node's name."""
    assert not _NODE_ID_RE.match(case["node_id"]), case["why"]


@pytest.mark.parametrize("case", VECTORS["derive"], ids=lambda c: c["serial"])
def test_every_id_owl_os_can_derive_is_recognised(case):
    """This repo never derives one. It must recognise all of them."""
    assert _NODE_ID_RE.match(case["node_id"]), case["serial"]
