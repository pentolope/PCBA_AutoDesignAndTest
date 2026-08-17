"""Placement angles: normalisation, and the reviewed library-zero registry.

Two different things happen to a rotation on its way from a layout into a
placement file, and keeping them apart is the whole job.

*Normalisation* moves an angle into the range the assembly house reads. It is
arithmetic, it applies to everything, and it changes no part's orientation.

A *library-zero offset* does change orientation. It exists because the part's
zero orientation in the assembly house's library need not match the zero of the
footprint in the layout, and where they differ every instance of that part is
fitted turned by the difference. That is a property of the part, so it is
looked up by distributor part number and never by footprint name: two parts can
share a footprint name and differ in the house's library.

An offset must never be invented to turn a negative angle positive - that is
what normalisation is for, and an offset would turn a part that was right. And
a part with no reviewed entry is not assumed to need no offset: "we have not
looked at this one" and "we looked and it needs nothing" are different
statements, and only the second is safe to ship.
"""

from __future__ import annotations


class OrientationError(Exception):
    """The registry cannot answer for a part that is about to be shipped."""


def normalise(angle, low=0.0, high=360.0):
    """Put an angle in [low, high). Same orientation, said the expected way.

    Half-open means half-open: 360 is not a placement angle, it is 0 written
    the way the range excludes. Python's float remainder can land exactly on
    the divisor for a very small negative input, so the open end is closed off
    explicitly rather than left to rounding.
    """
    value = low + (float(angle) - low) % (high - low)
    return low if value >= high else value


class Registry:
    """The reviewed offsets, indexed by part number.

    Built from the board manifest. Every lookup either returns a reviewed
    entry or raises: there is no default, because a silent zero is exactly the
    failure this exists to prevent.
    """

    REQUIRED_FIELDS = ("lcsc", "mpn", "package", "kicad_footprint",
                       "offset_deg", "review_status", "evidence_file",
                       "raw_file", "evidence_sha256")
    #: The only value that lets an entry be used. Not a prefix, not a
    #: case-insensitive match, not "anything non-empty": "pending" and
    #: "unreviewed" are entries somebody wrote down and has not finished, and
    #: shipping them would be exactly the silent assumption this prevents.
    USABLE_STATUS = "reviewed"

    @classmethod
    def is_reviewed(cls, value):
        """The one place the review status is decided. Exact JSON string only.

        No strip, no case fold, no coercion. ``" reviewed "`` is a value
        somebody typed by hand into a field that is read by a machine, and
        ``"Reviewed"`` is a different string; treating either as equal to
        ``"reviewed"`` invents a review nobody performed. A non-string - a
        number, a list, ``null`` - is not the value either, and quietly
        stringifying it would let ``True`` become a review.
        """
        return type(value) is str and value == cls.USABLE_STATUS

    @classmethod
    def describe_status(cls, row):
        """How to name what an entry says, in a message a human will read."""
        if "review_status" not in row:
            return "(no review_status field)"
        value = row["review_status"]
        return "(null)" if value is None else repr(value)

    def __init__(self, spec):
        self.spec = spec or {}
        self.part_number_field = self.spec.get("part_number_field", "MPN")
        low, high = self.spec.get("normalize_range_deg", [0, 360])
        self.low, self.high = float(low), float(high)
        self.entries = {}
        self.unusable = {}
        self.duplicates = []
        for row in self.spec.get("registry", []):
            lcsc = str(row.get("lcsc", "")).strip()
            if not lcsc:
                continue
            if not self.is_reviewed(row.get("review_status")):
                self.unusable[lcsc] = row
                continue
            if lcsc in self.entries:
                if float(self.entries[lcsc]["offset_deg"]) != float(
                        row["offset_deg"]):
                    self.duplicates.append(lcsc)
                continue
            self.entries[lcsc] = row

    def _status_of(self, lcsc):
        return self.describe_status(self.unusable.get(lcsc) or {})

    def defects(self):
        """Ways the registry is unusable as written, before any board is read."""
        problems = []
        for lcsc in sorted(set(self.duplicates)):
            problems.append({
                "lcsc": lcsc,
                "issue": "declared twice with different offsets, so which one "
                         "applies depends on read order"})
        for lcsc in sorted(self.unusable):
            problems.append({
                "lcsc": lcsc,
                "review_status": self._status_of(lcsc),
                "issue": "review_status is {} rather than exactly '{}', so no "
                         "reviewed orientation mapping is available for this "
                         "part".format(self._status_of(lcsc),
                                       self.USABLE_STATUS)})
        for lcsc, row in sorted(self.entries.items()):
            for field in self.REQUIRED_FIELDS:
                if str(row.get(field, "")).strip() == "":
                    problems.append({
                        "lcsc": lcsc,
                        "issue": "the entry records no {}; a reviewed offset "
                                 "nobody can re-check is not reviewed".format(
                                     field)})
            try:
                float(row.get("offset_deg"))
            except (TypeError, ValueError):
                problems.append({"lcsc": lcsc,
                                 "issue": "the offset is not a number",
                                 "value": row.get("offset_deg")})
        return problems

    def offset(self, lcsc):
        """The reviewed offset for a part. Raises if there is not one."""
        if not lcsc:
            raise OrientationError(
                "a placement carries no part number, so no reviewed "
                "orientation can be found for it")
        row = self.entries.get(lcsc)
        if row is None:
            if lcsc in self.unusable:
                raise OrientationError(
                    "{}: no reviewed orientation mapping is available - its "
                    "review_status is {} rather than exactly '{}'".format(
                        lcsc, self._status_of(lcsc), self.USABLE_STATUS))
            raise OrientationError(
                "{} has no reviewed orientation entry; a part whose library "
                "zero nobody has checked must not be assumed to need no "
                "correction".format(lcsc))
        return float(row["offset_deg"])

    def angle_for(self, lcsc, board_angle):
        """The placement angle to ship: board angle, offset applied, normalised."""
        return normalise(float(board_angle) + self.offset(lcsc),
                         self.low, self.high)

    def covers(self, lcsc):
        return lcsc in self.entries


def apply_to_rows(rows, registry, part_numbers, designator_field,
                  rotation_field, decimals=4):
    """Rewrite each row's angle in place. Returns (applied, problems).

    Every row must resolve to a reviewed entry. A row that cannot is left
    untouched and reported: shipping it with an unreviewed angle is the
    outcome this refuses to produce.
    """
    applied, problems = {}, []
    for row in rows:
        reference = str(row.get(designator_field, "")).strip()
        raw = row.get(rotation_field, "")
        try:
            board_angle = float(raw)
        except (TypeError, ValueError):
            problems.append({"reference": reference,
                             "issue": "rotation is not a number",
                             "value": raw})
            continue
        lcsc = part_numbers.get(reference)
        try:
            final = registry.angle_for(lcsc, board_angle)
        except OrientationError as exc:
            problems.append({"reference": reference, "lcsc": lcsc or None,
                             "issue": str(exc)})
            continue
        text = "{:.{}f}".format(final, decimals)
        if float(text) >= registry.high:
            # 359.99999 rounds to 360.0000 at four decimals, and what is
            # written is what ships. The range applies to the stored value.
            final = registry.low
            text = "{:.{}f}".format(final, decimals)
        row[rotation_field] = text
        applied[reference] = {
            "lcsc": lcsc,
            "board_deg": board_angle,
            "offset_deg": registry.offset(lcsc),
            "shipped_deg": final,
        }
    return applied, problems
