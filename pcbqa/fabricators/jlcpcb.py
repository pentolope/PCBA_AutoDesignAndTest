"""The JLCPCB adapter: official sources, and parsers held to what they read.

Three official pages carry the knowledge this adapter extracts:

  * ``impedance`` - https://jlcpcb.com/impedance - the published multilayer
    stackups, layer by layer with material and thickness, one section per
    layer count, each section led by a "No requirement Stackup" (what JLCPCB
    builds when no impedance requirement is stated - their own default,
    captured here as evidence rather than invented as a preference), plus the
    prepreg dielectric-constant table and the core dielectric constant.
  * ``capabilities`` - https://jlcpcb.com/capabilities/pcb-capabilities - the
    manufacturing capability statements: layer counts, FR-4 thickness
    options, copper weights and their stated default, the copper-weight- and
    layer-class-conditioned trace/space table, the separate trace-coil
    limits, drill and via limits, and the FR-4 dielectric constants - the
    only place a two-layer board's is stated, the impedance page covering
    only the layer counts it offers impedance control on.
  * ``copper-weight`` - https://jlcpcb.com/help/article/jlcpcb-copper-weight
    - JLCPCB's own copper-weight guide: the stated 1 oz = 35 um equivalence,
    the available weights per layer position, and a second copper-weight-
    conditioned trace/space table. This is the official bridge between the
    ounce-denominated options and the millimetre-denominated construction
    tables; without it no such conversion would be performed at all.

Both are server-rendered HTML with the data in the page itself; nothing here
executes scripts or guesses at XHR endpoints. The parsers are deliberately
anchored: each extraction matches a specific published structure, keeps the
matched text as its excerpt, and a probe that does not match produces a
``not_extracted`` record instead of a guess. A page redesign therefore shows
up as extraction failures to review, not as silently changed numbers.

PARSER_VERSION identifies the extraction code. It is stored in every
snapshot, so "JLCPCB changed its data" and "we changed how we read it" are
distinguishable facts forever after.

Honestly stated limitations of these sources:

  * every published construction describes exactly one copper build - 0.035
    mm outer and 0.0152 mm inner copper, cores annotated "H/HOZ" - and one
    nominal thickness (1.6 mm, encoded in the JLCnnttXH names); options at
    other weights and thicknesses are orderable but their constructions are
    not published, and none is inferred. Where a stackup's applicability is
    an interpretation of the fabricator's own notation rather than a bare
    verbatim value, the record says so in a `basis` string;
  * the two trace/space tables (capabilities page and copper-weight guide)
    do not fully agree - 2 oz multilayer reads 0.15 mm on one and 0.16 mm on
    the other. Both statements are normalized with their sources; consumers
    are expected to take the stricter;
  * prepreg and core dielectric constants are stated without a frequency;
  * surcharge/price structure is not published on these pages, so
    "standard" here means what JLCPCB itself labels default or lists as the
    ordinary option set - no cost model is fabricated.
"""

from __future__ import annotations

import hashlib
import re

from . import model

from .model import FABRICATOR                             # noqa: F401

#: Bump when extraction logic changes meaning. A changed parser version with
#: unchanged raw sources explains a changed normalized catalog by itself.
PARSER_VERSION = "7"

SOURCES = (
    {"id": "impedance", "kind": "official-stackup-page",
     "url": "https://jlcpcb.com/impedance"},
    {"id": "capabilities", "kind": "official-capabilities-page",
     "url": "https://jlcpcb.com/capabilities/pcb-capabilities"},
    {"id": "copper-weight", "kind": "official-help-article",
     "url": "https://jlcpcb.com/help/article/jlcpcb-copper-weight"},
    {"id": "impedance-calculator", "kind": "official-help-article",
     "url": "https://jlcpcb.com/help/article/"
            "user-guide-to-the-jlcpcb-impedance-calculator"},
    {"id": "thickness-options", "kind": "official-resource-page",
     "url": "https://jlcpcb.com/resources/pcb-thickness"},
)


class ParseError(model.CatalogError):
    """The source could not be read the way it was last known to be written."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

_SPAN = re.compile(r"<span[^>]*>([^<]*)</span>")
_TAG = re.compile(r"<[^>]+>")
_MM = re.compile(r"^([0-9.]+)\s*mm$")


_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


def _text_lines(html):
    # Script and style blocks are not page text: the SPA's data payload
    # repeats whole passages of visible prose inside window.__NUXT__,
    # and reading those as statements would double-count every sentence
    # the page actually shows. Non-breaking spaces are presentation too
    # - the 2-layer via row is written entirely with U+00A0 - and a
    # probe must not fail on which space a template happened to emit.
    html = _SCRIPT.sub("\n", html)
    text = _TAG.sub("\n", html).replace(" ", " ").replace("&nbsp;", " ")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _mm(token, where):
    match = _MM.match(token.strip())
    if not match:
        raise ParseError(
            "{}: expected a millimetre thickness, found {!r}. A value in an "
            "unexpected unit is refused rather than converted on a "
            "guess".format(where, token))
    return float(match.group(1))


# ---------------------------------------------------------------------------
# impedance page: stackups and dielectric constants
# ---------------------------------------------------------------------------

_SECTION = re.compile(r"(\d+)-Layer Impedance Control Stackup")
_TITLE = re.compile(r">\s*\d+\)\s*([^<]+?)\s+Stackup\s*</p>")
_ROW_SPLIT = re.compile(r'class="table-tr"')


def _parse_stackup_rows(fragment, where):
    """The layer rows of one row-block, from its ordered span texts.

    Two published shapes exist. A simple row is four cells: label, material,
    thickness, annotation (usually empty). A core row is a composite of three
    columns of three - labels, materials, thicknesses - plus one annotation,
    describing the copper-core-copper sandwich in one block. Anything else is
    a structure this parser has never seen, and refusing it is what keeps a
    page redesign from being read as a fabrication change.
    """
    spans = [s.strip() for s in _SPAN.findall(fragment)]
    if len(spans) == 4:
        label, material_cell, thickness, annotation = spans
        return [_layer(label, material_cell, thickness,
                       annotation or None, where)]
    if len(spans) == 10:
        labels, materials, thicknesses = spans[0:3], spans[3:6], spans[6:9]
        annotation = spans[9] or None
        rows = []
        for index in range(3):
            rows.append(_layer(labels[index], materials[index],
                               thicknesses[index],
                               annotation if materials[index] == "Core"
                               else None, where))
        return rows
    raise ParseError(
        "{}: a stackup row carries {} cells where 4 or 10 are the published "
        "shapes".format(where, len(spans)))


_PREPREG_CELL = re.compile(r"^(\d{3,4})\*(\d+)$")


def _layer(label, material_cell, thickness, annotation, where):
    if material_cell == "Copper":
        return model.stackup_layer(model.COPPER, _mm(thickness, where),
                                   label=label, annotation=annotation)
    if material_cell == "Core":
        return model.stackup_layer(model.DIELECTRIC, _mm(thickness, where),
                                   label=label if label != "Core" else None,
                                   form=model.CORE, annotation=annotation)
    match = _PREPREG_CELL.match(material_cell)
    if match:
        return model.stackup_layer(
            model.DIELECTRIC, _mm(thickness, where),
            material_name=match.group(1), form=model.PREPREG,
            sheet_count=int(match.group(2)), annotation=annotation)
    raise ParseError(
        "{}: material cell {!r} is not Copper, Core or a prepreg "
        "designation".format(where, material_cell))


def _parse_impedance(html, catalog):
    source = "impedance"

    # -- prepreg dielectric constants --------------------------------------
    marker = html.find("Prepreg dielectric constant")
    if marker < 0:
        raise ParseError("impedance page: the prepreg dielectric-constant "
                         "table marker is gone")
    table = html[marker:marker + 6000]
    pairs = re.findall(
        r"<td[^>]*>\s*(\d{3,4})\s*</td>\s*<td[^>]*>\s*([0-9.]+)\s*</td>",
        table)
    for name, dk in pairs:
        catalog["materials"]["prepreg {}".format(name)] = model.material(
            source, model.PREPREG, name, float(dk),
            excerpt="{} -> {}".format(name, dk))
    if len(pairs) < 3:
        raise ParseError(
            "impedance page: only {} prepreg dielectric constants parsed; "
            "the table has changed shape".format(len(pairs)))

    # -- core dielectric constant ------------------------------------------
    marker = html.find("Core dielectric constant")
    core_dk = None
    if marker >= 0:
        window = _TAG.sub("|", html[marker:marker + 800])
        found = re.search(r"\|\s*([0-9.]+)\s*\|", window)
        if found:
            core_dk = float(found.group(1))
            catalog["materials"]["core"] = model.material(
                source, model.CORE, "core", core_dk,
                excerpt="Core dielectric constant {}".format(core_dk))
    if core_dk is None:
        catalog["not_extracted"].append({
            "source": source, "field": "core dielectric constant",
            "reason": "marker or value not found where last published"})

    # -- per-section options and stackups ----------------------------------
    sections = list(_SECTION.finditer(html))
    if not sections:
        raise ParseError("impedance page: no layer-count stackup sections "
                         "found at all")
    for index, section in enumerate(sections):
        layer_count = int(section.group(1))
        end = sections[index + 1].start() if index + 1 < len(sections) \
            else len(html)
        body = html[section.start():end]
        _parse_section_options(body, layer_count, catalog)
        _parse_section_stackups(body, layer_count, catalog)

    if len(catalog["stackups"]) < 4:
        raise ParseError(
            "impedance page: only {} stackups parsed, which is implausibly "
            "few; refusing a catalog that would silently shrink the offer "
            "list".format(len(catalog["stackups"])))


def _parse_section_options(body, layer_count, catalog):
    """The option lists JLCPCB states per layer-count section."""
    lines = _text_lines(body)
    source = "impedance"
    for anchor, key, pattern, units in (
            ("Thickness", "thickness_options", r"^([0-9.]+)mm$", "mm"),
            ("Outer Copper Weight", "outer_copper_options",
             r"^([0-9.]+)oz$", "oz"),
            ("inner Copper Weight", "inner_copper_options",
             r"^([0-9.]+)oz$", "oz")):
        values, excerpt = _option_list(lines, anchor, pattern)
        identity = "{}L {}".format(layer_count, key)
        if values:
            applies = {"min_layers": layer_count, "max_layers": layer_count}
            if "outer" in key:
                applies["position"] = "outer"
            elif "inner" in key:
                applies["position"] = "inner"
            catalog["capabilities"][identity] = model.capability(
                source, identity, values, units=units,
                conditions="{} copper layers, impedance-capable "
                           "constructions".format(layer_count),
                excerpt=excerpt, category="stackup-options",
                applies=applies)
        else:
            catalog["not_extracted"].append({
                "source": source, "field": identity,
                "reason": "option list not found under its anchor"})


def _option_list(lines, anchor, pattern):
    for index, line in enumerate(lines):
        if line.lower() == anchor.lower():
            values = []
            for candidate in lines[index + 1:index + 12]:
                match = re.match(pattern, candidate)
                if match:
                    values.append(float(match.group(1)))
                elif values:
                    break
            if values:
                return values, "{}: {}".format(anchor, ", ".join(
                    str(v) for v in values))
    return [], None


def _parse_section_stackups(body, layer_count, catalog):
    titles = list(_TITLE.finditer(body))
    for index, title in enumerate(titles):
        name = title.group(1).strip()
        end = titles[index + 1].start() if index + 1 < len(titles) \
            else len(body)
        fragment = body[title.end():end]
        # Each stackup's table lives inside its own <li>; the last title of a
        # section would otherwise run to the section end and sweep up
        # whatever follows the tables.
        closing = fragment.find("</li>")
        if closing >= 0:
            fragment = fragment[:closing]
        if name == "No requirement":
            identity = "JLC-{}L-no-requirement".format(layer_count)
            default = True
        else:
            identity = name
            default = False
            prefix = re.match(r"JLC(\d{2})", name)
            if prefix and int(prefix.group(1)) != layer_count:
                raise ParseError(
                    "stackup {} appears in the {}-layer section; the page "
                    "is not organised the way this parser "
                    "believes".format(name, layer_count))
        layers = []
        row_fragments = _ROW_SPLIT.split(fragment)[1:]
        for row_fragment in row_fragments:
            layers.extend(_parse_stackup_rows(
                row_fragment, "stackup {}".format(identity)))
        if not layers:
            raise ParseError(
                "stackup {} parsed to zero layers".format(identity))
        stackup = {
            "source": "impedance",
            "name": name,
            "layer_count_section": layer_count,
            "layers": layers,
            "default_when_no_impedance_requirement": default,
            "excerpt_sha256": hashlib.sha256(
                fragment.encode("utf-8", "ignore")).hexdigest(),
        }
        copper = model.stackup_copper_count(stackup)
        if copper != layer_count:
            raise ParseError(
                "stackup {} parses to {} copper layers inside the {}-layer "
                "section".format(identity, copper, layer_count))
        stackup["applicability"] = _applicability(
            identity, name, stackup, catalog)
        if identity in catalog["stackups"]:
            raise ParseError(
                "two stackups parse to the identity {}; duplicate process "
                "identifiers cannot be told apart and are refused".format(
                    identity))
        catalog["stackups"][identity] = stackup


_NAME_CODE = re.compile(r"^JLC(\d{2})(\d{2})")


def _applicability(identity, name, stackup, catalog):
    """What build a published construction describes, from its own table.

    Everything here is either a verbatim value from the construction's rows,
    an arithmetic combination of two official statements, or an
    interpretation of the fabricator's own notation - and each field says
    which, in its `basis` string, so a reviewer can weigh the claim. Nothing
    is taken from general PCB knowledge: if the official bridge for a
    conversion is missing, the converted field is absent, not guessed.
    """
    layers = stackup["layers"]
    coppers = [l for l in layers if l["role"] == model.COPPER]
    outer = {coppers[0]["thickness_mm"], coppers[-1]["thickness_mm"]}
    inner = {l["thickness_mm"] for l in coppers[1:-1]}
    if len(outer) != 1 or len(inner) > 1:
        raise ParseError(
            "stackup {} states mixed copper thicknesses (outer {}, inner "
            "{}); every published construction so far is uniform, and a new "
            "shape must be reviewed, not averaged".format(
                identity, sorted(outer), sorted(inner)))
    applicability = {
        "outer_copper_thickness_mm": outer.pop(),
        "outer_basis": "stated in the construction table",
    }
    if inner:
        applicability["inner_copper_thickness_mm"] = inner.pop()

    # Outer weight: the construction's stated thickness at the fabricator's
    # own stated oz equivalence. Only an exact hit on a half-ounce multiple
    # is accepted; anything else stays a thickness without a weight.
    equivalence = catalog["capabilities"].get(
        "copper_weight_equivalence_um_per_oz")
    if equivalence is not None:
        um_per_oz = equivalence["value"]
        computed = (applicability["outer_copper_thickness_mm"] * 1000.0
                    / um_per_oz)
        if abs(computed - round(computed * 2) / 2) < 1e-6:
            applicability["outer_copper_weight_oz"] = round(computed * 2) / 2
            applicability["outer_basis"] = (
                "stated {} mm outer copper at the stated {} um/oz "
                "equivalence [copper-weight]".format(
                    applicability["outer_copper_thickness_mm"], um_per_oz))

    # Inner weight: the core rows carry the fabricator's own cladding
    # notation - "H/HOZ", half-oz/half-oz - which is an oz statement, not a
    # thickness to convert. The finished inner thickness (0.0152 mm) is less
    # than a nominal half-ounce foil, so converting it through the
    # equivalence would contradict the fabricator's own labelling; the
    # labelling wins and the reading is recorded as an interpretation.
    annotations = {l.get("annotation") for l in layers if l.get("annotation")}
    if any("H/H" in annotation for annotation in annotations):
        applicability["inner_copper_weight_oz"] = 0.5
        basis = ("core cladding notation 'H/H OZ' read as half-oz/half-oz, "
                 "corroborated by the stated 0.5 oz inner default "
                 "[copper-weight, capabilities]")
        finished = catalog["capabilities"].get("finished_inner_half_oz_um")
        inner_mm = applicability.get("inner_copper_thickness_mm")
        if finished is not None and inner_mm is not None and \
                abs(inner_mm * 1000.0 - finished["value"]) < 0.05:
            basis += (", and by the stated finished 0.5 oz inner thickness "
                      "of {} um matching this construction's {} mm "
                      "[impedance-calculator]".format(finished["value"],
                                                      inner_mm))
        applicability["inner_basis"] = basis

    # Nominal thickness: named constructions encode it (JLC 04 16 1H -> 4
    # layers, 1.6 mm), and the reading is only accepted when the summed
    # layers corroborate it; a name that contradicts its own table refuses.
    total = model.stackup_total_mm(stackup)
    code = _NAME_CODE.match(name)
    if code:
        nominal = int(code.group(2)) / 10.0
        if total is None or abs(total - nominal) > model.NOMINAL_TOLERANCE_MM:
            raise ParseError(
                "stackup {} encodes {} mm in its name but its layers sum to "
                "{} mm; the naming interpretation no longer holds and must "
                "be re-reviewed".format(identity, nominal, total))
        applicability["nominal_thickness_mm"] = nominal
        applicability["thickness_basis"] = (
            "name-encoded ({}), corroborated by the {} mm layer "
            "sum".format(name[:8], total))
    else:
        # The default construction carries no code; the layer sum against
        # the section's stated thickness options is the only tie available,
        # and it is labelled as the derivation it is.
        options = catalog["capabilities"].get(
            "{}L thickness_options".format(stackup["layer_count_section"]))
        nominal = None
        if options is not None and total is not None:
            near = [v for v in options["value"]
                    if abs(v - total) <= model.NOMINAL_TOLERANCE_MM]
            if len(near) == 1:
                nominal = near[0]
        if nominal is not None:
            applicability["nominal_thickness_mm"] = nominal
            applicability["thickness_basis"] = (
                "layer sum {} mm, nearest stated section thickness option; "
                "the page does not state this construction's nominal "
                "directly".format(total))
        else:
            applicability["thickness_basis"] = (
                "no stated nominal, and the {} mm layer sum does not single "
                "out one stated thickness option".format(total))
    return applicability


# ---------------------------------------------------------------------------
# capabilities page: anchored probes
# ---------------------------------------------------------------------------

def _parse_capabilities(html, catalog):
    source = "capabilities"
    text = "\n".join(_text_lines(html))
    dash = "[-–]"

    probes = (
        ("layer_count_range", "layers",
         r"Layer count\n(\d+)-(\d+) Layers",
         lambda m: {"min": int(m.group(1)), "max": int(m.group(2))}, None,
         None, None),
        ("impedance_tolerance_standard_percent", "impedance",
         r"Impedance Tolerance\n\u00b1(\d+)%",
         lambda m: float(m.group(1)), "percent",
         lambda m: "the fabricator's stated standard impedance control "
                   "tolerance; the page separately states a tighter "
                   "tolerance is available upon special request",
         None),
        ("fr4_thickness_options_mm", "board",
         r"Thickness for FR4 are: ([\d./]+) ?mm \(([^)]*)\)",
         lambda m: [float(v) for v in m.group(1).split("/")], "mm",
         lambda m: m.group(2), None),
        ("board_thickness_range_mm", "board",
         r"Thickness\n0\.4 " + dash + r" ([\d.]+) ?mm",
         lambda m: {"min": 0.4, "max": float(m.group(1))}, "mm", None,
         None),
        ("outer_copper_multilayer_oz", "copper",
         r"Multi-layer: ((?:[\d.]+ ?oz(?: / )?)+)",
         lambda m: [float(v) for v in re.findall(r"[\d.]+", m.group(1))],
         "oz", None,
         {"position": "outer", "min_layers": 4, "max_layers": None}),
        ("outer_copper_2layer_oz", "copper",
         r"2-layer: ((?:[\d.]+ ?oz(?: / )?)+)\nMulti-layer",
         lambda m: [float(v) for v in re.findall(r"[\d.]+", m.group(1))],
         "oz", None,
         {"position": "outer", "min_layers": 2, "max_layers": 2}),
        ("inner_copper_oz", "copper",
         r"Finished Inner Layer Copper\n((?:[\d.]+ ?oz(?: / )?)+)",
         lambda m: [float(v) for v in re.findall(r"[\d.]+", m.group(1))],
         "oz", None,
         {"position": "inner", "min_layers": 4, "max_layers": None}),
        ("inner_copper_default_oz", "copper",
         r"Finished copper weight of inner layer is ([\d.]+)oz by default",
         lambda m: float(m.group(1)), "oz",
         lambda m: "stated default",
         {"position": "inner", "min_layers": 4, "max_layers": None}),
    )

    matched = 0
    for identity, category, pattern, extract, units, conditions, applies \
            in probes:
        found = re.search(pattern, text)
        if not found:
            catalog["not_extracted"].append({
                "source": source, "field": identity,
                "reason": "anchored pattern not found on the capabilities "
                          "page"})
            continue
        matched += 1
        catalog["capabilities"][identity] = model.capability(
            source, identity, extract(found), units=units,
            conditions=conditions(found) if conditions else None,
            excerpt=found.group(0)[:160], category=category,
            applies=applies)
    if matched < 5:
        raise ParseError(
            "capabilities page: only {} of {} anchored probes matched; the "
            "page has been restructured and this parse cannot be "
            "trusted".format(matched, len(probes)))

    _parse_dielectric_constants(text, catalog)
    _parse_traces_table(text, catalog)
    _parse_trace_coils(text, catalog)
    _parse_layer_counts(text, catalog)
    _parse_drills_and_vias(text, catalog)


#: The capability cell states a bare dielectric constant scoped to a board
#: class; the description cell states them per prepreg. JLCPCB spells the
#: word both ways in the same cell ("Prepreg", "Perpreg"), so both are read.
_DK_BOARD_CLASS = re.compile(r"^([0-9.]+) \((\d+)-Layer PCB\)$")
_DK_PREPREG = re.compile(r"^(\d{3,4}) P(?:re|er)preg ([0-9.]+)$")


def _parse_dielectric_constants(text, catalog):
    """The capabilities page's FR-4 dielectric constants, as it scopes them.

    The impedance page states core and prepreg dielectric constants for the
    builds it publishes, which start at four layers. This block is the only
    place JLCPCB states one for a two-layer board, and a two-layer board
    cannot borrow the multilayer core's: they are different laminates and
    the page distinguishes them. Kept under source-qualified keys so the
    impedance page's records stay exactly what they were, and so a future
    disagreement between the two pages surfaces as a difference rather than
    as one silently overwriting the other.
    """
    source = "capabilities"
    marker = text.find("FR-4 Dielectric Constants\n")
    if marker < 0:
        catalog["not_extracted"].append({
            "source": source, "field": "FR-4 dielectric constants",
            "reason": "the block heading is not where it was last "
                      "published; no dielectric constant is read from this "
                      "page and none is assumed"})
        return
    lines = text[marker:].split("\n")[1:]
    read = 0
    for line in lines:
        board_class = _DK_BOARD_CLASS.match(line)
        if board_class:
            layers = int(board_class.group(2))
            identity = "core {}-layer ({})".format(layers, source)
            catalog["materials"][identity] = model.material(
                source, model.CORE, "{}-layer core".format(layers),
                float(board_class.group(1)), excerpt=line,
                context="FR-4 dielectric constants, capabilities page",
                applies={"min_layers": layers, "max_layers": layers})
            read += 1
            continue
        prepreg = _DK_PREPREG.match(line)
        if prepreg:
            identity = "prepreg {} ({})".format(prepreg.group(1), source)
            catalog["materials"][identity] = model.material(
                source, model.PREPREG, prepreg.group(1),
                float(prepreg.group(2)), excerpt=line,
                context="FR-4 dielectric constants, capabilities page")
            read += 1
            continue
        # The block ends at the next feature row, whose text states no
        # dielectric constant. Stopping on the first unreadable line keeps
        # the parse anchored instead of scanning the rest of the page.
        if read:
            break
        catalog["not_extracted"].append({
            "source": source, "field": "FR-4 dielectric constants",
            "reason": "the block's first line {!r} states no dielectric "
                      "constant in either published form".format(line[:80])})
        return


#: The page's board classes, as it words them, with the layer counts each
#: covers. "Multilayer" sits beside explicit 1-layer and 2-layer rows and
#: the discrete offered counts have no 3, so it starts at 4.
_BOARD_CLASSES = (
    ("1-layer", 1, 1),
    ("2-layer", 2, 2),
    ("Multilayer", 4, None),
)


def _parse_drills_and_vias(text, catalog):
    """The per-board-class drill and via rules, scoped as published.

    The page states drill diameter ranges and via hole-size/diameter pairs
    separately for 1-layer, 2-layer and multilayer boards; they are
    normalized separately, one record per class with the class in
    `applies`, so a selector can only ever consume the rule published for
    the board in front of it. "Hole size" and "via diameter" are the
    page's own two quantities and stay two fields; nothing collapses them.
    """
    source = "capabilities"
    emitted = 0
    for label, low, high in _BOARD_CLASSES:
        drill = re.search(
            r"{}: ([\d.]+) [-\u2013] ([\d.]+) ?mm".format(label), text)
        if drill:
            identity = "drill_diameter {} (capabilities)".format(
                label.lower())
            catalog["capabilities"][identity] = model.capability(
                source, identity,
                {"min": float(drill.group(1)), "max": float(drill.group(2))},
                units="mm",
                conditions="drilled hole diameter range for {} "
                           "boards".format(label.lower()),
                excerpt=drill.group(0)[:160], category="drill",
                applies={"min_layers": low, "max_layers": high})
            emitted += 1
        else:
            catalog["not_extracted"].append({
                "source": source,
                "field": "drill_diameter {}".format(label.lower()),
                "reason": "drill row not found where last published"})
        via = re.search(
            r"{}(?: \(([^)]+)\))?: ([\d.]+) ?mm hole size / ([\d.]+) ?mm "
            r"via diameter".format(label), text)
        if via:
            # A row the page itself marks "NPTH only" describes non-plated
            # holes, not interlayer plated vias; it is normalized under its
            # own category so a generic via requirement can never consume
            # it as if a barrel existed. The detection is the page's own
            # annotation, not the layer count.
            npth = bool(via.group(1)) and "NPTH" in via.group(1)
            category = "via-npth" if npth else "via"
            identity = "{} {} (capabilities)".format(
                "npth-via" if npth else "via", label.lower())
            conditions = ("{} hole size and diameter minima for {} "
                          "boards".format(
                              "non-plated (NPTH) via-shaped"
                              if npth else "via", label.lower()))
            if via.group(1):
                conditions += "; the page adds: {}".format(via.group(1))
            catalog["capabilities"][identity] = model.capability(
                source, identity,
                {"hole": float(via.group(2)),
                 "diameter": float(via.group(3))},
                units="mm", conditions=conditions,
                excerpt=via.group(0)[:160], category=category,
                applies={"min_layers": low, "max_layers": high})
            emitted += 1
        else:
            catalog["not_extracted"].append({
                "source": source, "field": "via {}".format(label.lower()),
                "reason": "via row not found where last published"})
    if emitted < 5:
        raise ParseError(
            "capabilities page: only {} of 6 per-class drill/via rules "
            "parsed; the drilling section has changed shape and a catalog "
            "without its scoping would let one board class borrow "
            "another's limits".format(emitted))


_IMPEDANCE_LAYERS = re.compile(
    r"Controlled Impedance\n((?:\d+/)+)\.\.\./(\d+) layers")


def _parse_layer_counts(text, catalog):
    """The discrete copper-layer counts, from the fabricator's own words.

    The page states the general capability as a range ("1-32 Layers") and
    enumerates discrete counts only in the Controlled Impedance row:
    "4/6/8/10/.../32 layers". The enumeration's explicit prefix must
    ascend by two and its closing value must continue that arithmetic, or
    the ellipsis reading is refused - an interpretation is only kept while
    the page corroborates it.

    Two records come out. The impedance list is verbatim-plus-ellipsis.
    The general discrete set is a stated-plus-derived record: {1, 2} from
    the rows conditioned on "1- and 2-layer" boards, the even multilayer
    counts from the enumeration, bounded by the stated 1-32 range. No
    statement anywhere on the official pages supports an odd count of
    three or more, and what no statement supports is not offered here -
    a 3- or 5-layer request must fail on the fabricator's silence, not
    slip through a numeric range.
    """
    source = "capabilities"
    found = _IMPEDANCE_LAYERS.search(text)
    if not found:
        raise ParseError(
            "capabilities page: the Controlled Impedance layer enumeration "
            "is gone; without it neither impedance availability nor the "
            "discrete layer-count set has stated evidence")
    explicit = [int(v) for v in found.group(1).rstrip("/").split("/")]
    last = int(found.group(2))
    steps = [b - a for a, b in zip(explicit, explicit[1:])]
    if not explicit or set(steps) != {2} or (last - explicit[-1]) % 2 != 0 \
            or last <= explicit[-1]:
        raise ParseError(
            "capabilities page: the Controlled Impedance enumeration {} "
            "... {} no longer ascends by two; the ellipsis reading no "
            "longer holds and must be re-reviewed".format(explicit, last))
    counts = list(range(explicit[0], last + 1, 2))
    catalog["capabilities"]["controlled_impedance_layer_counts"] = \
        model.capability(
            source, "controlled_impedance_layer_counts", counts,
            conditions="layer counts for which controlled impedance is "
                       "offered; the page enumerates {} then elides to {} "
                       "and the elision is read as continuing by two".format(
                           "/".join(str(v) for v in explicit), last),
            excerpt=found.group(0)[:160].replace("\n", " | "),
            category="layers")
    catalog["capabilities"]["fr4_copper_layer_options"] = \
        model.capability(
            source, "fr4_copper_layer_options", [1, 2] + counts,
            conditions="discrete supported copper-layer counts: 1 and 2 "
                       "from the rows the page conditions on 1- and "
                       "2-layer boards, the even multilayer counts from "
                       "the Controlled Impedance enumeration, inside the "
                       "stated 1-32 range; no official statement supports "
                       "an odd count of three or more, so none is offered",
            excerpt=found.group(0)[:160].replace("\n", " | "),
            category="layers")

    thick = re.search(r"2\.5 ?mm and above are for (\d+)\+ layer PCBs "
                      r"only", text)
    if thick:
        catalog["capabilities"]["fr4_thickness_2p5mm_plus"] = \
            model.capability(
                source, "fr4_thickness_2p5mm_plus",
                {"min": 2.5}, units="mm",
                conditions="board thicknesses of 2.5 mm and above exist "
                           "only for {}+ layer boards; their discrete "
                           "values are not published".format(
                               thick.group(1)),
                excerpt=thick.group(0)[:160], category="board",
                applies={"min_layers": int(thick.group(1)),
                         "max_layers": None})
    else:
        catalog["not_extracted"].append({
            "source": source, "field": "fr4_thickness_2p5mm_plus",
            "reason": "the 2.5 mm / 12+ layer condition not found where "
                      "last published"})


#: One clause of a trace/space statement: an optional layer-class prefix,
#: then "track / space mm". The published clause prefixes and what they
#: mean in layer counts; a prefix not in this table is an unknown class and
#: the clause is recorded as unread rather than guessed into scope.
_TRACE_CLASSES = {
    "1- and 2-layer": (1, 2),
    "2-layer": (2, 2),
    "2 layer": (2, 2),
    "multilayer": (4, None),
}

_TRACE_ROW = re.compile(
    r"Min\. track width and spacing \(([\d.]+) ?oz\)")
_TRACE_CLAUSE = re.compile(
    r"^(?:([A-Za-z0-9&\- ]+?)\s*:\s*)?"
    r"([\d.]+)\s*/\s*([\d.]+)\s*mm", re.IGNORECASE)


def _parse_traces_table(text, catalog):
    """The copper-weight-conditioned trace/space rows of the Traces table.

    Each published row names a copper weight; its cells subdivide by layer
    class. One capability record is emitted per (weight, layer-class)
    clause, with the machine-readable scope in `applies`, so a selector can
    ask "what limit is published for THIS weight at THIS layer count" and
    get either a stated answer or nothing - never a neighbouring rule.

    A row's headline cell (a bare value with no layer-class prefix) is
    skipped whenever classed clauses exist: the fabricator's own
    subdivision is the stronger statement, and letting the headline stand
    beside it would double-publish the coarse number over the fine one.
    """
    source = "capabilities"
    lines = text.split("\n")
    rows = [(index, _TRACE_ROW.match(line))
            for index, line in enumerate(lines)
            if _TRACE_ROW.match(line)]
    emitted = 0
    for index, row in rows:
        weight = float(row.group(1))
        clauses = []
        for line in lines[index + 1:index + 8]:
            if _TRACE_ROW.match(line) or line.startswith(
                    "Track width tolerance"):
                break
            clause = _TRACE_CLAUSE.match(line)
            if clause:
                clauses.append((clause.group(1), float(clause.group(2)),
                                float(clause.group(3)), line))
            elif clauses:
                # The row's cells are contiguous; the first non-clause line
                # after any clause ends the row, so a stray later line can
                # never be swept into the wrong copper weight.
                break
        classed = [c for c in clauses if c[0]]
        chosen = classed if classed else clauses
        for prefix, track, space, line in chosen:
            if prefix is None:
                span = (1, None)
                label = "any layer count"
            else:
                span = _TRACE_CLASSES.get(prefix.strip().lower())
                if span is None:
                    catalog["not_extracted"].append({
                        "source": source,
                        "field": "trace/space {} oz clause".format(weight),
                        "reason": "unrecognised layer-class prefix "
                                  "{!r}".format(prefix)})
                    continue
                label = prefix.strip()
            identity = "trace_space {}oz {} (capabilities)".format(
                weight, re.sub(r"[^a-z0-9.]+", "-", label.lower()))
            catalog["capabilities"][identity] = model.capability(
                source, identity,
                {"track": track, "space": space}, units="mm",
                conditions="{} oz finished copper, {}".format(weight, label),
                excerpt=line[:160], category="trace",
                applies={"copper_weights_oz": [weight],
                         "min_layers": span[0], "max_layers": span[1]})
            emitted += 1
    if emitted < 4:
        raise ParseError(
            "capabilities page: only {} conditioned trace/space clauses "
            "parsed from the Traces table; the table has changed shape and "
            "a catalog without it would quietly widen or lose the published "
            "limits".format(emitted))


def _parse_trace_coils(text, catalog):
    """The trace-coil limits, normalized as the special case they are.

    These sit under the "Trace coils" feature on the capabilities page and
    historically read like a general trace/space statement. They are not
    one: they describe coil patterns specifically, and they are stored
    under a category no general-routing selector consults, so the special
    rule can never stand in for board manufacturability.
    """
    source = "capabilities"
    for identity, pattern, conditions in (
            ("trace_coils_masked_1oz",
             r"Minimum trace width/clearance: ([\d.]+)/([\d.]+) ?mm,\s*"
             r"when traces are covered by solder mask \(1oz\)",
             "trace coils, covered by solder mask, 1 oz copper"),
            ("trace_coils_unmasked_1oz",
             r"Minimum trace width/clearance: ([\d.]+)/([\d.]+) ?mm,\s*"
             r"when traces are NOT covered by solder mask \(1oz\)",
             "trace coils, not covered by solder mask, 1 oz copper, "
             "ENIG only")):
        found = re.search(pattern, text)
        if not found:
            catalog["not_extracted"].append({
                "source": source, "field": identity,
                "reason": "trace-coil statement not found where last "
                          "published"})
            continue
        catalog["capabilities"][identity] = model.capability(
            source, identity,
            {"track": float(found.group(1)), "space": float(found.group(2))},
            units="mm", conditions=conditions,
            excerpt=found.group(0)[:160], category="trace-coils")



# ---------------------------------------------------------------------------
# impedance-calculator guide: the impedance model's own numbers, scoped
# ---------------------------------------------------------------------------

_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_MIL = re.compile(r"([\d.]+)\s*mil")
_CORE_MM = re.compile(r"^(>\s*)?([\d.]+)\s*mm$")

#: What the impedance-calculator model's records mean. They describe the
#: numbers JLCPCB's own impedance calculation uses - deduced from test
#: results by JLCPCB, per the page's own disclaimer - and they are NOT the
#: generic stackup-page values. Both live in the catalog under distinct
#: identities; a consumer chooses the scope that matches the calculation
#: it is performing, and neither ever overrides the other.
_CALC_CONTEXT = "impedance-calculator model"
_CALC_NOTE = ("stated by JLCPCB as reference values deduced from its own "
              "test results, subject to adjustment")


def _table_rows(html, marker):
    """Cell texts of the first <table> after `marker`, row by row."""
    start = html.find(marker)
    if start < 0:
        return None
    table_start = html.find("<table", start)
    if table_start < 0:
        return None
    table_end = html.find("</table>", table_start)
    rows = []
    for row in _TR.findall(html[table_start:table_end]):
        cells = []
        for cell in _TD.findall(row):
            cell = _TAG.sub(" ", cell).replace("&nbsp;", " ")
            cell = cell.replace("&gt;", ">").replace("&lt;", "<")
            cell = cell.replace("&amp;", "&")
            cells.append(re.sub(r"\s+", " ", cell).strip())
        if cells:
            rows.append(cells)
    return rows


def _parse_impedance_calculator(html, catalog):
    source = "impedance-calculator"
    text = "\n".join(_text_lines(html))

    # -- calculation parameters table (finished copper, soldermask) --------
    rows = _table_rows(html, "Calculation Parameters Used")
    if not rows:
        raise ParseError("impedance-calculator guide: the calculation-"
                         "parameters table is gone")
    parameters = {row[0]: row[1] for row in rows if len(row) == 2}
    copper_rows = (
        ("finished_copper_external_1oz_mil",
         "External copper thickness (1 oz)", "external", 1.0),
        ("finished_copper_internal_0.5oz_mil",
         "Internal copper thickness (0.5 oz)", "internal", 0.5),
        ("finished_copper_internal_1oz_mil",
         "Internal copper thickness (1 oz)", "internal", 1.0),
    )
    for identity, label, position, weight in copper_rows:
        stated = parameters.get(label)
        found = _MIL.match(stated or "")
        if not found:
            raise ParseError(
                "impedance-calculator guide: the finished-copper parameter "
                "{!r} is missing or unreadable; the distinction between "
                "nominal weight and finished conductor thickness cannot be "
                "half-recorded".format(label))
        catalog["capabilities"][identity] = model.capability(
            source, identity, float(found.group(1)), units="mil",
            conditions="finished conductor thickness the impedance "
                       "calculator uses for {} {} oz copper; distinct from "
                       "the nominal foil weight".format(position, weight),
            excerpt="{} | {}".format(label, stated)[:160],
            category="copper-finished",
            applies={"position": position, "copper_weights_oz": [weight]})
    for identity, label in (
            ("soldermask_on_fr4_mil", "Base soldermask thickness"),
            ("soldermask_on_copper_mil", "Copper-surface soldermask "
                                         "thickness"),
            ("soldermask_between_traces_mil", "Soldermask thickness in "
                                              "between traces")):
        stated = parameters.get(label)
        found = _MIL.match(stated or "")
        if found:
            catalog["capabilities"][identity] = model.capability(
                source, identity, float(found.group(1)), units="mil",
                conditions=_CALC_CONTEXT,
                excerpt="{} | {}".format(label, stated)[:160],
                category="soldermask")
        else:
            catalog["not_extracted"].append({
                "source": source, "field": identity,
                "reason": "parameter row not found where last published"})
    mask_dk = parameters.get("Soldermask dielectric constant (\u03b5r)")
    if mask_dk is None:
        for label, stated in parameters.items():
            if label.startswith("Soldermask dielectric constant"):
                mask_dk = stated
    if mask_dk is None:
        raise ParseError("impedance-calculator guide: the soldermask "
                         "dielectric constant is gone")
    catalog["materials"]["soldermask (impedance-calculator)"] =         model.material(source, "soldermask", "soldermask",
                       float(mask_dk), context=_CALC_CONTEXT,
                       excerpt="Soldermask dielectric constant | "
                               "{}".format(mask_dk))
    trapezoid = parameters.get("Trace top width")
    if trapezoid:
        found = _MIL.search(trapezoid)
        if found:
            catalog["capabilities"]["trace_top_vs_base_width_mil"] =                 model.capability(
                    source, "trace_top_vs_base_width_mil",
                    float(found.group(1)), units="mil",
                    conditions="finished trace top width is the base width "
                               "minus this; the etched cross-section is a "
                               "trapezoid, not the drawn rectangle",
                    excerpt="Trace top width | {}".format(trapezoid)[:160],
                    category="trace-geometry")

    # -- the stated finished-vs-nominal inner copper statement -------------
    found = re.search(
        r"0\.5 oz copper on internal layers is ([\d.]+) ?\u03bcm thick.{0,60}?"
        r"nominal ([\d.]+) ?\u03bcm", text, re.S)
    if found:
        catalog["capabilities"]["finished_inner_half_oz_um"] =             model.capability(
                source, "finished_inner_half_oz_um",
                float(found.group(1)), units="um",
                conditions="finished 0.5 oz inner copper thickness; the "
                           "nominal foil is {} um and the difference is "
                           "production loss, per the page".format(
                               found.group(2)),
                excerpt=found.group(0)[:160], category="copper-finished",
                applies={"position": "internal",
                         "copper_weights_oz": [0.5]})
    else:
        catalog["not_extracted"].append({
            "source": source, "field": "finished_inner_half_oz_um",
            "reason": "the finished-vs-nominal statement not found where "
                      "last published"})

    # -- calculator copper-weight support ----------------------------------
    if re.search(r"calculator only supports 0\.5 oz and 1 oz", text):
        catalog["capabilities"]["impedance_calculator_internal_oz"] =             model.capability(
                source, "impedance_calculator_internal_oz", [0.5, 1.0],
                units="oz",
                conditions="internal copper weights the impedance "
                           "calculator supports; 2 oz requires contacting "
                           "customer support, per the page",
                excerpt="The calculator only supports 0.5 oz and 1 oz",
                category="impedance")
    if re.search(r"the calculator only supports 1 oz", text):
        catalog["capabilities"]["impedance_calculator_external_oz"] =             model.capability(
                source, "impedance_calculator_external_oz", [1.0],
                units="oz",
                conditions="external copper weight the impedance "
                           "calculator supports, although heavier is "
                           "manufacturable",
                excerpt="the calculator only supports 1 oz",
                category="impedance")

    # -- accepted impedance ranges -----------------------------------------
    found = re.search(
        r"(\d+) to (\d+) \u03a9 for single-ended and (\d+) to (\d+) "
        r"\u03a9 for differential", text)
    if found:
        catalog["capabilities"]["impedance_range_single_ended_ohm"] =             model.capability(
                source, "impedance_range_single_ended_ohm",
                {"min": float(found.group(1)), "max": float(found.group(2))},
                units="ohm", conditions=_CALC_CONTEXT,
                excerpt=found.group(0)[:160], category="impedance")
        catalog["capabilities"]["impedance_range_differential_ohm"] =             model.capability(
                source, "impedance_range_differential_ohm",
                {"min": float(found.group(3)), "max": float(found.group(4))},
                units="ohm", conditions=_CALC_CONTEXT,
                excerpt=found.group(0)[:160], category="impedance")

    # -- core material family by layer class -------------------------------
    found = re.search(
        r"4- to 8-layer boards are calculated assuming ([A-Za-z ]+?) "
        r"(NP-[0-9A-Z]+) core material, and (?:[A-Za-z]+ )?([A-Z0-9-]+) "
        r"for 10-layer boards and higher", text)
    if not found:
        raise ParseError(
            "impedance-calculator guide: the material-family statement is "
            "gone; without it the two Dk tables have no stated scope")
    families = (
        (found.group(2), 4, 8, "{} {}".format(found.group(1).strip(),
                                              found.group(2))),
        (found.group(3), 10, None, found.group(3)),
    )
    for name, low, high, label in families:
        identity = "impedance_core_material {}-{}L".format(
            low, high if high else "up")
        catalog["capabilities"][identity] = model.capability(
            source, identity, name, conditions="core material family the "
            "impedance calculator assumes for this layer-count class",
            excerpt=found.group(0)[:160], category="impedance-materials",
            applies={"min_layers": low, "max_layers": high})

    # -- the two Dk tables, each scoped to its family and layer class ------
    emitted_cores = emitted_prepregs = 0
    for marker, family, low, high in (
            ("(4 to 8 layers)", found.group(2), 4, 8),
            ("(10+ layers)", found.group(3), 10, None)):
        rows = _table_rows(html, marker)
        if not rows:
            raise ParseError(
                "impedance-calculator guide: the {} Dk table is "
                "gone".format(family))
        applies = {"min_layers": low, "max_layers": high}
        for cells in rows:
            if cells and cells[0].startswith("Core Thickness"):
                continue
            # A row carries a core (thickness, er) pair, a prepreg
            # (type, resin, nominal, er) quadruple, or both side by side.
            core = _CORE_MM.match(cells[0]) if cells else None
            if core and len(cells) >= 2:
                over = bool(core.group(1))
                thickness = float(core.group(2))
                dk = float(cells[1])
                identity = "core {} {}{}mm (impedance-calculator)".format(
                    family, ">" if over else "", thickness)
                properties = ({"core_thickness_over_mm": thickness}
                              if over else
                              {"core_thickness_mm": thickness})
                properties["family"] = family
                catalog["materials"][identity] = model.material(
                    source, model.CORE, family, dk,
                    context=_CALC_CONTEXT, applies=dict(applies),
                    properties=properties,
                    excerpt="{} | {} | {}".format(family, cells[0],
                                                  cells[1])[:160])
                emitted_cores += 1
            if len(cells) >= 6 and cells[2]:
                type_cell = cells[2]
                match = re.match(r"^(\d{3,4})(?:\s*\((\d{3,4})\))?$",
                                 type_cell)
                resin = re.match(r"^([\d.]+)%$", cells[3])
                nominal = _MIL.match(cells[4])
                if match and resin and nominal:
                    name = match.group(1)
                    identity = ("prepreg {} ({}, impedance-calculator)"
                                .format(name, family))
                    properties = {"family": family,
                                  "resin_content_percent":
                                      float(resin.group(1)),
                                  "nominal_thickness_mil":
                                      float(nominal.group(1))}
                    if match.group(2):
                        properties["supersedes"] = match.group(2)
                    catalog["materials"][identity] = model.material(
                        source, model.PREPREG, name, float(cells[5]),
                        context=_CALC_CONTEXT, applies=dict(applies),
                        properties=properties,
                        excerpt=" | ".join(cells[2:6])[:160])
                    emitted_prepregs += 1
    if emitted_cores < 12 or emitted_prepregs < 6:
        raise ParseError(
            "impedance-calculator guide: only {} core and {} prepreg Dk "
            "rows parsed; the tables have changed shape and a partial "
            "model would quietly narrow the published data".format(
                emitted_cores, emitted_prepregs))



# ---------------------------------------------------------------------------
# thickness resource page: the layer-count restrictions the global list hides
# ---------------------------------------------------------------------------

def _parse_thickness_options(html, catalog):
    """The per-thickness layer-count restrictions, as published.

    The capabilities page states one global FR-4 thickness list; this page
    states which of those thicknesses are NOT available for which layer
    counts ("0.6mm ... not available for 1-layer, 4-layer, or 6-layer
    PCBs"). Restrictions are normalized as their own records so ordinary
    feasibility can subtract them from the global list - a pair the
    fabricator forbids must not pass because each half exists somewhere.
    The page's own semantics are list-minus-stated-restrictions; nothing
    here invents a restriction the page does not state, and nothing reads
    the list as narrower than the page says.
    """
    source = "thickness-options"
    text = "\n".join(_text_lines(html))

    stated = re.search(
        r"We offer the following thickness options: ((?:[\d.]+ ?mm, )+"
        r"[\d.]+ ?mm)", text)
    if not stated:
        raise ParseError(
            "thickness page: the stated options list is gone; without it "
            "the restriction statements have no list to restrict")
    values = [float(v) for v in re.findall(r"[\d.]+", stated.group(1))]
    catalog["capabilities"]["fr4_thickness_options_mm (thickness-options)"] \
        = model.capability(
            source, "fr4_thickness_options_mm (thickness-options)",
            values, units="mm",
            conditions="the same stated FR-4 thickness list, from the "
                       "thickness resource page; corroborates the "
                       "capabilities page",
            excerpt=stated.group(0)[:160], category="board")

    # Restrictions are DISCOVERED, not enumerated: every line that says
    # "not available for ...-layer" is treated as a restriction-shaped
    # statement, and each one must either normalize completely - a
    # thickness read from the same line, layer counts read from the
    # clause - or refuse the whole parse. A newly added exclusion (say,
    # 0.8 mm for some count) therefore lands in the catalog and shows up
    # as a reviewable semantic change; one this code cannot read stops
    # the acquisition instead of vanishing while the page "parses fine".
    emitted = 0
    for line in _text_lines(html):
        if "not available for" not in line or "-layer" not in line:
            continue
        thickness_match = re.search(
            r"([\d.]+) ?mm thickness", line)
        clause = re.search(
            r"not available for ((?:[\d]+-layer(?:,? (?:or )?)?)+) "
            r"PCBs", line)
        counts = [int(v) for v in re.findall(r"(\d+)-layer",
                                             clause.group(1))] \
            if clause else []
        if not thickness_match or not counts:
            raise ParseError(
                "thickness page: a restriction-shaped statement could not "
                "be read completely ({!r}); a restriction half-understood "
                "would quietly re-widen a pair the fabricator forbids, so "
                "the parse refuses".format(line[:120]))
        thickness = float(thickness_match.group(1))
        identity = "thickness_restriction {}mm".format(
            "{:g}".format(thickness))
        if identity in catalog["capabilities"]:
            raise ParseError(
                "thickness page: two restriction statements name {} mm; "
                "which one governs cannot be decided here".format(
                    thickness))
        catalog["capabilities"][identity] = model.capability(
            source, identity,
            {"thickness_mm": thickness,
             "excluded_layer_counts": sorted(set(counts))},
            units="mm",
            conditions="stated as not available for these layer counts; "
                       "full sentence: {}".format(line[:120]),
            excerpt=line[:160],
            category="board-thickness-restriction")
        emitted += 1
    if emitted < 2:
        raise ParseError(
            "thickness page: only {} restriction statement(s) discovered "
            "where the page last published two; the statements have moved "
            "or changed shape and must be re-reviewed, not dropped".format(
                emitted))


# ---------------------------------------------------------------------------
# copper-weight guide: the oz bridge and its design-rule table
# ---------------------------------------------------------------------------

_OZ_LIST = re.compile(r"([\d./]+)\s*oz")


def _parse_copper_weight(html, catalog):
    source = "copper-weight"
    lines = _text_lines(html)
    text = "\n".join(lines)

    # -- the stated oz <-> um equivalence ----------------------------------
    found = re.search(
        r"1\s*oz copper = copper thickness of (\d+) ?[\u00b5\u03bcu]m",
        text)
    if not found:
        raise ParseError(
            "copper-weight guide: the stated oz/um equivalence is gone; "
            "without it no thickness-to-weight bridge exists and the parse "
            "cannot stand")
    catalog["capabilities"]["copper_weight_equivalence_um_per_oz"] =         model.capability(
            source, "copper_weight_equivalence_um_per_oz",
            float(found.group(1)), units="um/oz",
            conditions="fabricator-stated definition",
            excerpt=found.group(0)[:160], category="definition")

    # -- available weights per layer position ------------------------------
    for identity, anchor, conditions, applies, availability in (
            ("outer_copper_fr4_standard_oz", "1oz, 2oz (standard)",
             "FR-4 outer layer, standard options",
             {"position": "outer", "min_layers": 1, "max_layers": None},
             None),
            ("outer_copper_fr4_2layer_heavy_oz", "2.5oz, 3.5oz, 4.5oz",
             "FR-4 outer layer, 2-layer boards only, special high-current",
             {"position": "outer", "min_layers": 2, "max_layers": 2},
             None),
            ("inner_copper_fr4_oz", "0.5oz (default), 1oz, 2oz",
             "FR-4 inner layer; availability depends on total layers and "
             "overall thickness (specifics not published)",
             {"position": "inner", "min_layers": 4, "max_layers": None},
             "conditional")):
        for line in lines:
            if line.startswith(anchor):
                values = [_oz_token(v) for v in _OZ_LIST.findall(line)]
                record = model.capability(
                    source, identity, values, units="oz",
                    conditions=conditions, excerpt=line[:160],
                    category="copper", applies=applies)
                if availability is not None:
                    # The page itself says which factors availability
                    # depends on without publishing the mapping; the flag
                    # carries that statement so a selector can refuse to
                    # read the bare list as proof of any particular tuple.
                    record["availability"] = availability
                catalog["capabilities"][identity] = record
                break
        else:
            catalog["not_extracted"].append({
                "source": source, "field": identity,
                "reason": "available-weights row not found where last "
                          "published"})

    # -- the design-rule table (FR-4 rows only) ----------------------------
    marker = next((index for index, line in enumerate(lines)
                   if line.startswith("Design Rules")), None)
    if marker is None:
        raise ParseError(
            "copper-weight guide: the design-rules table marker is gone")
    emitted = 0
    index = marker
    while index + 2 < len(lines):
        weight_cell = lines[index]
        type_cell = lines[index + 1]
        value_cell = lines[index + 2]
        row = _design_rule_row(weight_cell, type_cell, value_cell)
        if row is None:
            index += 1
            continue
        weights, span, label, track = row
        if weights is None:
            # An FPC row or an unrecognised type: present on the page,
            # deliberately not normalized (rigid FR-4 only), and said so.
            catalog["not_extracted"].append({
                "source": source,
                "field": "design rule {!r} / {!r}".format(weight_cell,
                                                          type_cell),
                "reason": "outside this adapter's rigid-FR-4 scope, or an "
                          "unrecognised PCB type; not normalized"})
            index += 3
            continue
        identity = "trace_space {} {} (copper-weight)".format(
            "-".join(str(w) for w in weights) + "oz",
            re.sub(r"[^a-z0-9.>=]+", "-", label.lower()))
        catalog["capabilities"][identity] = model.capability(
            source, identity, {"track": track, "space": track},
            units="mm",
            conditions="{} oz finished copper, {} (track and space "
                       "stated as one figure)".format(
                           "/".join(str(w) for w in weights), label),
            excerpt="{} | {} | {}".format(weight_cell, type_cell,
                                          value_cell)[:160],
            category="trace",
            applies={"copper_weights_oz": weights,
                     "min_layers": span[0], "max_layers": span[1]})
        emitted += 1
        index += 3
    if emitted < 4:
        raise ParseError(
            "copper-weight guide: only {} FR-4 design-rule rows parsed; "
            "the table has changed shape".format(emitted))


_FR4_TYPES = {
    "1-2 layers fr4": (1, 2, "1-2 layers FR4"),
    "2-layer fr4": (2, 2, "2-layer FR4"),
    "any layer fr4": (1, None, "any layer FR4"),
    "4 layers fr4": (4, None, ">=4 layers FR4"),
}


def _design_rule_row(weight_cell, type_cell, value_cell):
    """One table row -> (weights, layer span, label, track_mm) or None.

    Returns None when the three lines are not a rule row at all; returns
    (None, ...) when they are a row this adapter deliberately does not
    normalize (FPC), so the caller can record that honestly.
    """
    weights = [_oz_token(v) for v in _OZ_LIST.findall(weight_cell)]
    if not weights or _OZ_LIST.sub("", weight_cell).strip(" or,") != "":
        return None
    value = re.match(r"([\d.]+) ?mm", value_cell)
    if not value:
        return None
    normalized_type = type_cell.lower()
    normalized_type = normalized_type.replace("\u2013", "-")
    normalized_type = re.sub(r"[\u2265]", "", normalized_type)
    normalized_type = re.sub(r"\s+", " ", normalized_type).strip()
    span = _FR4_TYPES.get(normalized_type)
    if span is None:
        return (None, None, type_cell, None)
    return weights, (span[0], span[1]), span[2], float(value.group(1))


def _oz_token(token):
    if "/" in token:
        parts = token.split("/")
        return round(float(parts[0]) / float(parts[1]), 4)
    return float(token)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def parse(raw_sources):
    """raw bytes per source id -> a validated normalized catalog.

    Fails closed: a missing source, an unparseable structure, or an
    implausibly small result raises rather than producing a catalog that
    quietly says less than the fabricator does.
    """
    for spec in SOURCES:
        if spec["id"] not in raw_sources:
            raise ParseError(
                "source {!r} was not supplied; a catalog parsed from part of "
                "the evidence would silently shrink the offer".format(
                    spec["id"]))
    catalog = model.empty_catalog(FABRICATOR)
    # The copper-weight guide first: it carries the oz equivalence the
    # stackup applicability derivation cites. Then the impedance-calculator
    # guide (finished-thickness statements the applicability basis cites),
    # then the capabilities page (section option lists), then the
    # impedance page whose stackups consume all of the above.
    _parse_copper_weight(
        raw_sources["copper-weight"].decode("utf-8", "ignore"), catalog)
    _parse_impedance_calculator(
        raw_sources["impedance-calculator"].decode("utf-8", "ignore"),
        catalog)
    _parse_capabilities(
        raw_sources["capabilities"].decode("utf-8", "ignore"), catalog)
    _parse_thickness_options(
        raw_sources["thickness-options"].decode("utf-8", "ignore"), catalog)
    _parse_impedance(
        raw_sources["impedance"].decode("utf-8", "ignore"), catalog)
    return model.validate_catalog(catalog)
