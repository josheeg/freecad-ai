"""Tests that the planning artifacts and the code still agree.

The spec, the spine and `AGENTS.md` all quote facts about the code: how many
capabilities exist, how many numbered decisions there are, which tool a caller
should reach for. Every one of those counts drifts the moment an AD is added,
and a stale count is worse than no count — it reads as authoritative.

That is not hypothetical. `AGENTS.md` said "nineteen numbered decisions" after
AD-20 and AD-21 existed, and the spine's Deferred table listed three
settled questions as open. Both were found by hand, twice. These tests make
that a failure instead.

No FreeCAD needed, so unlike test_boundaries.py this file is not
integration-marked.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INITIATIVE = ROOT / "_bmad-output" / "initiative-freecad-mcp-server"
SPEC = INITIATIVE / "spec-freecad-ai-server" / "spec-freecad-ai-server.md"
SKETCH_SPEC = INITIATIVE / "spec-freecad-ai-sketches" / "spec-freecad-ai-sketches.md"
SPINE = (
    INITIATIVE / "architecture-freecad-ai-server" / "architecture-freecad-ai-server.md"
)
AGENTS = ROOT / "AGENTS.md"
README = ROOT / "README.md"
SERVER = ROOT / "src" / "freecad_ai" / "server.py"

_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
    20: "twenty",
    21: "twenty-one",
    22: "twenty-two",
    23: "twenty-three",
    24: "twenty-four",
    25: "twenty-five",
    26: "twenty-six",
    27: "twenty-seven",
    28: "twenty-eight",
    29: "twenty-nine",
    30: "thirty",
    31: "thirty-one",
    32: "thirty-two",
    33: "thirty-three",
    34: "thirty-four",
    35: "thirty-five",
    36: "thirty-six",
}


def _require(path: Path) -> str:
    if not path.is_file():
        pytest.fail(
            f"{path.relative_to(ROOT)} is missing. The planning artifacts are "
            f"referenced by AGENTS.md as binding, so their absence is a "
            f"documentation failure, not something to skip."
        )
    return path.read_text(encoding="utf-8")


def test_every_agents_md_path_exists() -> None:
    """A pointer to a file that moved is worse than no pointer.

    AGENTS.md is the only thing an agent reads before touching this repo, so a
    dead path there sends the reader nowhere.
    """
    text = _require(AGENTS)
    paths = re.findall(r"`([\w./-]*(?:_bmad-output|docs|src)/[^`]+)`", text)
    assert paths, "AGENTS.md no longer points at any artifact path"
    missing = [p for p in paths if not (ROOT / p).exists()]
    assert not missing, f"AGENTS.md points at paths that do not exist: {missing}"


def test_agents_md_decision_count_matches_the_spine() -> None:
    """AGENTS.md states how many decisions the spine holds. Keep it true.

    Matching on the spelled-out word as well as the digits, because the line
    reads "Twenty-one numbered decisions, AD-1…AD-21" and either half going
    stale would mislead.
    """
    agents = _require(AGENTS)
    spine = _require(SPINE)

    ids = {int(n) for n in re.findall(r"^\| AD-(\d+) \|", spine, re.MULTILINE)}
    assert ids, "no AD rows found in the spine"
    highest = max(ids)
    assert ids == set(range(1, highest + 1)), (
        f"AD ids are not contiguous from 1 to {highest}: "
        f"{sorted(set(range(1, highest + 1)) - ids)} missing"
    )

    count = _WORDS.get(highest)
    assert count is not None, f"add a spelling for {highest} to _WORDS"
    assert f"{count.capitalize()} numbered decisions" in agents, (
        f"the spine holds {highest} decisions ({count}), which AGENTS.md does "
        f"not say. A stale count reads as authoritative."
    )
    assert f"AD-1…AD-{highest}" in agents, (
        f"AGENTS.md should name the range AD-1…AD-{highest}"
    )


def test_agents_md_covers_the_packaging_traps() -> None:
    """AGENTS.md is the first thing an agent reads, so an absent trap is hit.

    The managed block is refreshed by `bmad-project-context`, so these live
    outside it — but "outside" is not self-enforcing, and a refresh that moved
    the section inward would drop the content without failing anything.

    Asserted on the substance rather than the heading: the three traps are the
    things that cannot be derived by reading the code, since each one produces
    an executable that appears to work. A section heading alone would pass and
    teach nothing.
    """
    agents = _require(AGENTS)
    _, _, outside = agents.partition("<!-- /bmad:context -->")

    for needed in (
        "freeze",
        "_freecad_bridge.py",
        "__main__.py",
        "_MEIPASS",
    ):
        assert needed in outside, (
            f"AGENTS.md's unmanaged section does not mention {needed!r}. It is "
            f"outside the managed block precisely so it survives a refresh, and "
            f"an agent hitting that trap would otherwise rediscover it the hard "
            f"way."
        )


def test_agents_md_paths_exist_covers_the_packaging_paths() -> None:
    """Every path AGENTS.md names must exist, packaging ones included.

    The existing path check only looks inside the managed block's own regex
    shape, so a path introduced in the sections below the markers is unchecked.
    """
    agents = _require(AGENTS)
    _, _, outside = agents.partition("<!-- /bmad:context -->")
    paths = re.findall(
        r"`([\w./-]*(?:packaging|scripts|src|tests|dist)/[\w./-]+)`", outside
    )
    assert paths, "the unmanaged section of AGENTS.md names no artifact path"
    missing = [p for p in paths if not (ROOT / p).exists()]
    assert not missing, f"AGENTS.md names paths that do not exist: {missing}"


def test_agents_md_capability_count_matches_the_spec() -> None:
    agents = _require(AGENTS)
    spec = _require(SPEC)
    caps = {int(n) for n in re.findall(r"\*\*CAP-(\d+)\*\*", spec)}
    assert caps, "no capabilities found in the spec"
    highest = max(caps)
    assert caps == set(range(1, highest + 1)), (
        f"CAP ids are not contiguous from 1 to {highest}"
    )
    assert f"CAP-1…CAP-{highest}" in agents, (
        f"the spec holds {highest} capabilities, which AGENTS.md does not say"
    )


def _registered_tool_names() -> list[str]:
    """Every tool name passed to the `_tool` decorator, in registration order.

    Read from the source rather than from the imported registry: the count
    matters as documentation, and importing would make this test depend on the
    module it is checking. The name is the first string literal after the
    decorator, however the call is wrapped across lines.
    """
    source = SERVER.read_text(encoding="utf-8")
    names = re.findall(r'@_tool\(\s*"(\w+)"', source)
    assert names, "no @_tool registrations found in server.py"
    return names


def _fenced_blocks(markdown: str) -> list[str]:
    """The bodies of every fenced code block, correctly paired.

    A naive ``r"```(?:python)?\\n(.*?)```"`` matches a *closing* fence as readily
    as an opening one, so it pairs each close with the next open and returns
    spans made of ordinary prose and markdown tables. That is not a cosmetic
    mistake: the example check built on it passed a mutation that corrupted an
    example, because the example was never inside the text it was scanning.

    The opening fence carries an optional info string and the closing one does
    not, which is what makes the pairing unambiguous.
    """
    return re.findall(r"^```[\w]*\n(.*?)^```[ \t]*$", markdown, re.DOTALL | re.M)


def test_readme_documents_every_tool() -> None:
    """A tool absent from the README is a tool nobody knows exists.

    The other checks in this file guard counts. This one guards *coverage*, and
    the gap it covers is the one a count cannot see: the README's tool table
    said "36 tools" while omitting 13 of them - the entire sketch surface plus
    `distance`, `is_inside` and `cross_section`. A correct count next to a table
    that lists two thirds of the surface reads as complete.

    Naming the tool in backticks is the bar. Prose about what it does is not
    required, because that is a judgement call; presence is not.
    """
    readme = _require(README)
    names = _registered_tool_names()
    # A *table row*, not merely the name somewhere in the file. The first
    # version of this test accepted a mention in prose, and then passed a
    # mutation that had renamed a tool in the table while leaving an example
    # using it - which is not a documentation gap, but it did prove the check
    # was measuring the wrong thing.
    rows = [line for line in readme.splitlines() if line.startswith("|")]
    table = "\n".join(rows)
    missing = [name for name in names if f"`{name}`" not in table]
    assert not missing, (
        f"the README's tool tables do not list {len(missing)} of {len(names)} "
        f"tools: {missing}. The table says how many there are, so a reader has "
        f"no way to tell which are absent rather than merely undescribed."
    )


def test_readme_tool_count_matches_the_registry() -> None:
    """The number the README states, checked against the registry itself.

    Separate from the coverage check because they fail differently. This one
    catches a stale total after a tool is removed; the coverage check catches a
    tool that exists but is unlisted. Passing both means the table is neither
    wrong nor incomplete.
    """
    readme = _require(README)
    expected = len(_registered_tool_names())
    # The count appears as "N tools" in the architecture listing.
    stated = re.findall(r"MCP server, (\d+) tools", readme)
    assert stated, "README no longer states a tool count"
    assert int(stated[0]) == expected, (
        f"the README says {stated[0]} tools; {expected} are registered"
    )


def test_every_readme_example_names_a_real_tool() -> None:
    """Backticked snake_case words in code blocks must be real tools.

    The examples are the part of the README most likely to be copied verbatim,
    so a tool renamed or removed leaves a snippet that fails at the first call
    with no indication the documentation is the problem. Restricted to fenced
    blocks and to snake_case identifiers, so ordinary prose and CamelCase type
    names are not mistaken for tools.
    """
    readme = _require(README)
    names = set(_registered_tool_names())
    blocks = _fenced_blocks(readme)
    assert blocks, "README has no fenced code blocks to check"

    used: set[str] = set()
    for block in blocks:
        # Only *calls* count: a name followed by an opening parenthesis. The
        # first version matched any snake_case word, so it flagged the response
        # keys the examples legitimately show - `edge_count`, `solid_count` -
        # along with `freecad_ai` and `freecad_procs`, which are a module and a
        # script. A tool call and a JSON field are not the same thing.
        used |= set(re.findall(r"\b([a-z][a-z0-9]*_[a-z0-9_]*)\s*\(", block))
    unknown = sorted(used - names)
    assert not unknown, (
        f"the README's examples call {unknown}, which are not tools. Either the "
        f"examples are stale or they use a name the surface does not have."
    )


def test_readme_claims_the_index_convention() -> None:
    """The 1-based rule is load-bearing and easy to lose in a rewrite.

    Every index in the surface is 1-based - edges, faces, geometry, constraint
    elements - and constraint indices were the last holdout at 0-based until
    AD-27. A README that documents the sketch tools without saying so would
    leave a caller guessing, and a 0-based guess fails silently by addressing
    the wrong element.
    """
    readme = _require(README)
    assert "1-based" in readme, (
        "the README documents the sketch tools but never states the indexing "
        "convention, so a caller has nothing to go on"
    )


def _deferred_table_lines(spine: str) -> list[str]:
    """Every pipe-table line in the Deferred section, header and all.

    Deliberately unfiltered. Callers decide which lines matter, because a
    malformed table - one missing its header, say - still represents an open
    question and must not be able to hide by being unparseable.
    """
    section = spine.split("## Deferred", 1)[-1]
    section = re.split(r"^## ", section, maxsplit=1, flags=re.MULTILINE)[0]
    return [
        line
        for line in section.splitlines()
        if line.startswith("|") and "---" not in line
    ]


def _deferred_rows(spine: str) -> list[str]:
    """Body rows of the Deferred section's own table.

    Scoped twice over, deliberately. Rows only, so the prose underneath - which
    exists precisely to explain what is settled - is not read as a to-do entry.
    And only up to the next heading, so a later section's table cannot be swept
    in and mistaken for this one's.

    The header is dropped only when a separator follows it. Dropping
    `rows[0]` unconditionally meant a headerless table lost its first real row,
    so a deferred item could sit there and be invisible to every check.
    """
    section = spine.split("## Deferred", 1)[-1]
    section = re.split(r"^## ", section, maxsplit=1, flags=re.MULTILINE)[0]
    lines = section.splitlines()
    rows: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("|") and "---" not in line:
            following = lines[index + 1] if index + 1 < len(lines) else ""
            if following.startswith("|") and "---" in following:
                index += 2  # header plus its separator
                continue
            rows.append(line)
        index += 1
    return rows


def test_spine_defers_nothing_it_has_settled() -> None:
    """A settled question must not still be listed as deferred.

    The spine's Deferred table is read as a to-do list. An entry there that is
    actually decided sends the next unit to re-open work that was closed on
    purpose.
    """
    spine = _require(SPINE)
    settled = {
        "parametric array": "AD-21 settles this permanently",
        "gui target": "headless is sufficient; AD-15 is now absolute",
        "publishing": "local installation is the end state",
    }
    rows = " ".join(_deferred_rows(spine)).lower()
    for topic, reason in settled.items():
        assert topic not in rows, (
            f"the spine's Deferred table still lists '{topic}', but {reason}. "
            f"Remove the row or record it under the settled decisions instead."
        )


def test_agents_md_sketch_capability_count_matches_the_sketch_spec() -> None:
    """The sketch spec is governed too, so its count is checked the same way.

    The main spec's counter cannot see CAP-S ids: they are a separate sequence
    in a separate file, and a stale count for it would read exactly as
    authoritative as a stale count for the main one.
    """
    agents = _require(AGENTS)
    spec = _require(SKETCH_SPEC)
    caps = {int(n) for n in re.findall(r"\*\*CAP-S(\d+)\*\*", spec)}
    assert caps, "no CAP-S capabilities found in the sketch spec"
    highest = max(caps)
    assert caps == set(range(1, highest + 1)), (
        f"CAP-S ids are not contiguous from 1 to {highest}: "
        f"{sorted(set(range(1, highest + 1)) - caps)} missing"
    )
    assert f"CAP-S1…CAP-S{highest}" in agents, (
        f"the sketch spec holds {highest} capabilities, which AGENTS.md does not say"
    )


def test_readme_decision_count_matches_the_spine() -> None:
    """The README quotes the decision count too, and it drifted once already.

    The existing test covered AGENTS.md, the spine and the specs, so this
    second prose copy of the same binding number was outside the net. A count
    that appears in two places needs both checked.
    """
    readme = _require(README)
    spine = _require(SPINE)
    highest = max(int(n) for n in re.findall(r"^\| AD-(\d+) \|", spine, re.MULTILINE))
    count = _WORDS.get(highest)
    assert count is not None, f"add a spelling for {highest} to _WORDS"
    assert f"AD-1…AD-{highest}" in readme, (
        f"the spine holds {highest} decisions ({count}), which the README does not say"
    )
    assert f"{count.capitalize()} numbered decisions" in readme, (
        f"the README's spelled-out decision count does not match {highest}"
    )


def test_readme_sketch_example_values_are_correct() -> None:
    """The README's worked example must print the numbers it will actually get.

    Every figure in that block is derived here from the closed form for a
    rounded rectangle, not copied from a previous run, so a wrong value fails
    rather than being re-blessed.

    Worth having because this is the class of error the README has actually
    made: it claimed a volume of 1600 for this profile, which is the
    sharp-cornered area, and a `dof` of 16 that was never measured. Both are
    plausible-looking numbers in the one block a reader is most likely to copy
    from, and no other test reads the README at all.
    """
    readme = _require(README)
    width, height, corner = 40.0, 20.0, 5.0
    # A 40x20 rectangle with 5mm rounded corners: the four corner squares of
    # side r lose (4 - pi) * r^2 to the quarter circles.
    area = width * height - (4 - math.pi) * corner**2
    plate = area * 4.0
    boss = area * 2.0

    for value in (f"{area:.3f}", f"{plate:.2f}", f"{boss:.2f}"):
        assert value in readme, (
            f"the README's sketch example does not print {value}, which is "
            f"what FreeCAD returns for the profile it draws"
        )
    # The degrees of freedom that profile actually has. Arcs and lines are
    # fully determined by their coordinates, so there is nothing left to solve.
    assert '"dof": 0' in readme, (
        "the README quotes a degrees-of-freedom figure for a profile with no "
        "constraints; the real value is 0"
    )
    # The z span the attached extrusion occupies: the box top is z=4.
    assert "z 4..6" in readme, (
        "the README's face-attachment example does not state the z range the "
        "boss occupies, so a caller cannot tell the attachment took effect"
    )


def test_readme_primitive_example_values_are_correct() -> None:
    """The first worked example's volumes, checked against the same arithmetic.

    Guarded alongside the sketch block so the README cannot drift in one place
    and stay correct in another. Both figures were re-measured against
    FreeCAD 1.1.3 when this was written; a 40x20x4 plate is 3200, a 2mm-radius
    hole through 4mm removes pi*4*4 = 50.265, and filleting two edges of
    2mm removes the remaining 6.86.
    """
    readme = _require(README)
    # The README uses a Unicode minus (U+2212) in prose, so the dash is
    # normalised before matching rather than being hardcoded as ASCII.
    plain = readme.replace("\u2212", "-")
    plate = 40.0 * 20.0 * 4.0
    # A 2mm-radius hole through 4mm of plate. Exact to 2dp: pi*4*4 = 50.265...
    drilled = plate - math.pi * 2.0**2 * 4.0
    assert f"{drilled:.2f}" in readme, (
        f"the README should show {drilled:.2f} for a 40x20x4 plate with a 2mm "
        f"hole through it; {drilled:.2f} is what FreeCAD 1.1.3 returns"
    )
    # The filleted figure is the kernel's own arithmetic, not closed form, so it
    # is pinned to the measured value rather than derived. Checked exactly: a
    # range here passed a 3149.74-for-3149.73 near-miss, which is precisely the
    # error this is meant to catch.
    filleted = 3142.87
    assert f"volume {filleted:.2f}" in readme, (
        f"the README's filleted volume should be {filleted:.2f}, measured "
        f"against FreeCAD 1.1.3"
    )
    # Re-measured before this was pinned: the fillet removes 6.86, and the
    # README said 6.87. Its own two figures did not add up, which is the kind
    # of thing a reader checks and concludes the docs are careless.
    removed = round(drilled - filleted, 2)
    assert (
        f"volume {filleted:.2f}" in readme and f"{removed:.2f} of rounding" in plain
    ), (
        f"the README should say {removed:.2f} of rounding, being "
        f"{drilled:.2f} - {filleted:.2f}"
    )
    # And the subtraction the README shows must add up. Checking the volume
    # alone let a wrong figure through, because the parenthetical still said
    # the right thing; both halves have to agree with each other.
    assert f"{plate:.0f} plate - {plate - drilled:.2f} hole" in plain, (
        "the README's breakdown of the drilled volume does not add up: it "
        f"should read '{plate:.0f} plate - {plate - drilled:.2f} hole'"
    )
    assert f"{drilled:.2f} - {drilled - filleted:.2f}" in plain, (
        "the README's breakdown of the filleted volume does not add up: it "
        f"should read '{drilled:.2f} - {drilled - filleted:.2f}'"
    )


def test_readme_tool_count_matches_the_code() -> None:
    """The README states how many tools the server exposes. Keep it true.

    It said "14 tools" for a long while, when there were 24. No test compared
    prose to the code, and a wrong count in a document is easy to read past -
    unlike a missing file, which announces itself.
    """
    readme = _require(README)
    server = _require(SERVER)
    actual = len(re.findall(r"^@_tool\(", server, re.MULTILINE))
    assert actual > 0, "no @_tool decorators found in server.py"
    match = re.search(r"(\d+)\s+tools over stdio", readme)
    assert match, "the README no longer states a tool count near the layout"
    claimed = int(match.group(1))
    assert claimed == actual, (
        f"the README says {claimed} tools over stdio; server.py defines "
        f"{actual}. A stale count reads as authoritative."
    )


def test_sketch_spec_status_matches_the_code() -> None:
    """AGENTS.md must not claim the sketch tools are absent once they exist.

    The inverse of the check that caught them landing: whatever AGENTS.md says
    about the sketch tools' status has to agree with server.py, in both
    directions. A stale "not built yet" is the same defect as a stale count —
    it tells a reader to distrust a tool surface that is real.
    """
    agents = _require(AGENTS)
    server = (ROOT / "src" / "freecad_ai" / "server.py").read_text(encoding="utf-8")

    tools = (
        "add_sketch",
        "add_sketch_line",
        "add_sketch_arc",
        "add_sketch_circle",
        "remove_sketch_geometry",
        "add_sketch_constraint",
        "sketch_status",
        "extrude_sketch",
        "attach_sketch_to_face",
        "sketch_to_face",
    )
    present = [name for name in tools if f"def {name}(" in server]
    assert present, "no sketch tools found in server.py at all"

    if len(present) == len(tools):
        assert "Not built yet" not in agents, (
            "every sketch tool exists in server.py, so AGENTS.md must not say "
            "'Not built yet'"
        )
        for name in tools:
            assert f"`{name}`" in agents, (
                f"{name} exists but AGENTS.md does not name it, so a reader "
                f"cannot discover it from the instructions"
            )
    else:
        absent = [name for name in tools if name not in present]
        assert "Not built yet" in agents, (
            f"these sketch tools are missing from server.py ({', '.join(absent)}), "
            f"so AGENTS.md must say the spec is unbuilt rather than listing them"
        )


def test_spine_defers_only_named_topics() -> None:
    """Every Deferred row must name what it is and why it is open.

    A row that is only prose is a note, and a note in a to-do list is
    indistinguishable from work nobody has claimed.

    An empty table is valid and is what `status: final` looks like - every row
    was settled. The check is on the shape of any row that exists, not on
    there being one.
    """
    spine = _require(SPINE)
    for row in _deferred_rows(spine):
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert len(cells) == 3, f"Deferred row does not have three cells: {row}"
        assert all(cells), f"Deferred row has an empty cell: {row}"


def test_spine_final_implies_no_unsettled_assumptions() -> None:
    """`status: final` must not coexist with an open [ASSUMPTION] anywhere.

    The point of promoting the spine is that its decisions are settled. An
    assumption still sitting anywhere in it is the one thing that would make
    "final" a claim rather than a fact.

    Scans the whole document, not the tail after `## Deferred`. An earlier
    version split there and searched only what followed, which passed on a
    spine whose assumptions were all still open - the [ASSUMPTION] tags lived
    in the deferred table's first column, above the heading, and the check
    could not have failed. It was cited as the reason `final` was safe.
    """
    spine = _require(SPINE)
    if _spine_status(spine) != "final":
        return
    offenders = [
        f"L{number}: {line.strip()}"
        for number, line in enumerate(spine.splitlines(), start=1)
        if "[ASSUMPTION]" in line
    ]
    assert not offenders, (
        "the spine is marked final but still carries an [ASSUMPTION] at "
        + "; ".join(offenders)
        + ". Either confirm the assumption and drop the tag, or leave the "
        "status at draft."
    )


def _spine_status(spine: str) -> str:
    """The status from the frontmatter, not any mention of the word in prose.

    A substring search for "status: final" also matched a sentence explaining
    why the status had been returned to draft, which made a draft spine look
    final. Frontmatter only: the line must be the one after `type:`'s sibling
    block, which is the first `status:` at the start of a line before any `##`.
    """
    for line in spine.splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def test_spine_final_has_no_deferred_rows() -> None:
    """`status: final` means nothing is deferred, so the table must be empty.

    Checks the raw table lines rather than the parsed rows. A malformed table
    - one missing its header - would otherwise defeat the row parser and hide
    a genuinely open question, which is the failure this whole check exists to
    prevent. An empty Deferred section with no table at all is the valid shape
    for `status: final`.
    """
    spine = _require(SPINE)
    if _spine_status(spine) != "final":
        return
    lines = _deferred_table_lines(spine)
    assert not lines, (
        "the spine is marked final but its Deferred section still contains a "
        "table: " + "; ".join(line.strip() for line in lines)
    )
