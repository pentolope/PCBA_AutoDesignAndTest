"""The JLCPCB adapter: official sources, and parsers held to what they read.

Two official pages carry the knowledge this adapter extracts:

  * ``impedance`` - https://jlcpcb.com/impedance - the published multilayer
    stackups, layer by layer with material and thickness, one section per
    layer count, each section led by a "No requirement Stackup" (what JLCPCB
    builds when no impedance requirement is stated - their own default,
    captured here as evidence rather than invented as a preference), plus the
    prepreg dielectric-constant table and the core dielectric constant.
  * ``capabilities`` - https://jlcpcb.com/capabilities/pcb-capabilities - the
    manufacturing capability statements: layer counts, FR-4 thickness
    options, copper weights and their stated default, trace/space, drill and
    via limits.

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

  * the published stackup tables describe the 1.6 mm construction; other
    nominal thicknesses are listed as options but their constructions are not
    published on this page, and none is inferred;
  * prepreg and core dielectric constants are stated without a frequency;
  * surcharge/price structure is not published on these pages, so
    "standard" here means what JLCPCB itself labels default or lists as the
    ordinary option set - no cost model is fabricated.
"""

from __future__ import annotations

import hashlib
import re

from . import model

FABRICATOR = "jlcpcb"

#: Bump when extraction logic changes meaning. A changed parser version with
#: unchanged raw sources explains a changed normalized catalog by itself.
PARSER_VERSION = "1"

SOURCES = (
    {"id": "impedance", "kind": "official-stackup-page",
     "url": "https://jlcpcb.com/impedance"},
    {"id": "capabilities", "kind": "official-capabilities-page",
     "url": "https://jlcpcb.com/capabilities/pcb-capabilities"},
)


class ParseError(model.CatalogError):
    """The source could not be read the way it was last known to be written."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

_SPAN = re.compile(r"<span[^>]*>([^<]*)</span>")
_TAG = re.compile(r"<[^>]+>")
_MM = re.compile(r"^([0-9.]+)\s*mm$")


def _text_lines(html):
    text = _TAG.sub("\n", html)
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
            catalog["capabilities"][identity] = model.capability(
                source, identity, values, units=units,
                conditions="{} copper layers, impedance-capable "
                           "constructions".format(layer_count),
                excerpt=excerpt, category="stackup-options")
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
        if identity in catalog["stackups"]:
            raise ParseError(
                "two stackups parse to the identity {}; duplicate process "
                "identifiers cannot be told apart and are refused".format(
                    identity))
        catalog["stackups"][identity] = stackup


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
         None),
        ("fr4_thickness_options_mm", "board",
         r"Thickness for FR4 are: ([\d./]+) ?mm \(([^)]*)\)",
         lambda m: [float(v) for v in m.group(1).split("/")], "mm",
         lambda m: m.group(2)),
        ("board_thickness_range_mm", "board",
         r"Thickness\n0\.4 " + dash + r" ([\d.]+) ?mm",
         lambda m: {"min": 0.4, "max": float(m.group(1))}, "mm", None),
        ("outer_copper_multilayer_oz", "copper",
         r"Multi-layer: ((?:[\d.]+ ?oz(?: / )?)+)",
         lambda m: [float(v) for v in re.findall(r"[\d.]+", m.group(1))],
         "oz", None),
        ("outer_copper_2layer_oz", "copper",
         r"2-layer: ((?:[\d.]+ ?oz(?: / )?)+)\nMulti-layer",
         lambda m: [float(v) for v in re.findall(r"[\d.]+", m.group(1))],
         "oz", None),
        ("inner_copper_oz", "copper",
         r"Finished Inner Layer Copper\n((?:[\d.]+ ?oz(?: / )?)+)",
         lambda m: [float(v) for v in re.findall(r"[\d.]+", m.group(1))],
         "oz", None),
        ("inner_copper_default_oz", "copper",
         r"Finished copper weight of inner layer is ([\d.]+)oz by default",
         lambda m: float(m.group(1)), "oz",
         lambda m: "stated default"),
        ("min_trace_space_masked_1oz_mm", "trace",
         r"Minimum trace width/clearance: ([\d.]+)/([\d.]+) ?mm,\s*when "
         r"traces are covered by solder mask \(1oz\)",
         lambda m: {"track": float(m.group(1)), "space": float(m.group(2))},
         "mm", lambda m: "covered by solder mask, 1 oz copper"),
        ("drill_diameter_multilayer_mm", "drill",
         r"Multilayer: ([\d.]+) " + dash + r" ([\d.]+) ?mm",
         lambda m: {"min": float(m.group(1)), "max": float(m.group(2))},
         "mm", None),
        ("via_multilayer_mm", "via",
         r"Multilayer: ([\d.]+) ?mm hole size / ([\d.]+) ?mm via diameter",
         lambda m: {"hole": float(m.group(1)), "diameter": float(m.group(2))},
         "mm", None),
    )

    matched = 0
    for identity, category, pattern, extract, units, conditions in probes:
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
            excerpt=found.group(0)[:160], category=category)
    if matched < 5:
        raise ParseError(
            "capabilities page: only {} of {} anchored probes matched; the "
            "page has been restructured and this parse cannot be "
            "trusted".format(matched, len(probes)))


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
    _parse_impedance(
        raw_sources["impedance"].decode("utf-8", "ignore"), catalog)
    _parse_capabilities(
        raw_sources["capabilities"].decode("utf-8", "ignore"), catalog)
    return model.validate_catalog(catalog)
