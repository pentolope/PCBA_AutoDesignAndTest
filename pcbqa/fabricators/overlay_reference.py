"""Pinned reference: Barbuto et al., covered-microstrip permittivity.

Source, held to the letter:

    M. Barbuto, A. Alu, F. Bilotti, A. Toscano, L. Vegni,
    "Characteristic impedance of a microstrip line with a dielectric
    overlay", COMPEL - The international journal for computation and
    mathematics in electrical and electronic engineering,
    Vol. 32 No. 6 (2013), pp. 1855-1867, DOI 10.1108/COMPEL-10-2012-0283.

Transcribed visually from an author-provided copy of the published
paper, with the equation regions re-rendered at up to 560 dpi, after
every automated access route had been exhausted (publisher paywalled,
Roma Tre repository record metadata-only, open-access aggregators
reporting no fulltext). The equation numbers below are the paper's own.
This module is EVIDENCE, not a production model: nothing in the
impedance solver dispatches it, and the exact obstacles between this
reference and a production coated edge are recorded at
``impedance.coated_microstrip_epsilon``.

What the paper provides:

  * equation (8): a closed fit for the effective relative permittivity
    of a zero-thickness microstrip of width w on a substrate of
    thickness d and permittivity eps_r, IMMERSED in a homogeneous
    medium eps_rc (its Figure 3(a)) - the two-media problem - fitted
    to FEM results with constants k1..k7 via Levenberg-Marquardt;
  * equation (10): a fitted equivalent "cover permittivity" for a
    single uniform overlay of normalized thickness dc/d, constants
    k8..k13, built to hit 1 at dc = 0 and eps_rc as dc -> infinity;
  * equation (11): the fits' stated validity, 0 < w/d <= 10 and
    eps_rc/eps_r <= 10;
  * equation (12): the covered-microstrip permittivity as equation (8)
    with eps_rc replaced by the cover permittivity;
  * Figures 4-10: comparisons against full-wave simulations.

Measured against the paper's own figures (the tests carry the
numbers): equation (8) reproduces every readable anchor, on both of
its Corr branches. Equation (10) AS PRINTED does not: with the printed
positive exponent on w/d the composed equation (12) misses the paper's
own plotted curves by +0.18 to +0.23 in effective permittivity at
w/d in {4, 10}, while the same equation with that exponent's SIGN
reversed lands within plot-reading precision of every anchor,
including the eps_rc/eps_r = 10 validity edge. The printed formula and
the printed figures cannot both be right - an internal
printed-vs-figure inconsistency of the paper. The sign reversal is a
figure-consistent CANDIDATE correction (a probable sign typo, stated
here as this toolkit's inference), not an author- or
publisher-confirmed erratum. Both variants are implemented below,
named for what they are; neither is adopted as authority, and the
w/d < 1 cover branch (k11..k13) has no validation figure in the paper
at all, so it is entirely unvalidated in either sign.

Known deltas from the Hammerstad forms pinned in ``propagation``: the
paper's air limit (its equation (9)) carries k1 = 0.52 where the
classic Hammerstad coefficient is exactly one half, and its s(w/d)
shape term uses 0.04*(1 - w/d) LINEAR where Hammerstad prints the
square. Both are transcribed here as printed, which is why this
reference and the production bare model are different families, and
why a future enclosure must not mix a Barbuto edge with a Hammerstad
edge without resolving that mismatch explicitly.

No finite conductor thickness: the paper's design chain is
parameterized by the zero-thickness strip width w, the substrate
thickness d, the cover thickness dc and the permittivities - no
finished copper thickness and no trapezoidal conductor geometry
appear anywhere in it. Feeding this toolkit's Wheeler
thickness-corrected effective width into these functions would NOT be
source-faithful; any mapping from finite-thickness trapezoid geometry
onto the paper's w/d must be separately declared, justified and
tested. This is a recorded blocker to production promotion.

No direct JLCPCB mask mapping: equation (10) assumes ONE homogeneous
uniform overlay of thickness dc, while the fabricator publishes
distinct mask thicknesses over copper, on the substrate and between
traces - a conformal profile, not a slab. Collapsing those onto a
single dc by convenience is not a defensible mapping, so no real
JLCPCB coated width is derived from equation (10) and no physical
bound is claimed from it.

The exact artifact transcribed is fingerprinted in
``SOURCE_ARTIFACT`` below (the PDF itself is copyrighted and is not
committed). The decisive equation-region renders that adjudicated the
transcription are fingerprinted in ``TRANSCRIPTION_RENDERS``: these
are RECORDED FINGERPRINTS of the exact images judged, together with
the rendering recipe in ``RENDER_RECIPE`` - they are not claimed to be
byte-reproducible render artifacts, because raster output is not
stable across renderer versions. The recipe permits best-effort
regeneration and visual re-adjudication from the fingerprinted PDF;
the hashes identify what was actually looked at.
"""

from __future__ import annotations

import math

from .. import propagation

#: The paper's fit constants, as printed.
K1, K2, K3, K4 = 0.52, 0.241, 0.715, 0.446
K5, K6, K7 = 1.814, 1.798, 12.52
K8, K9, K10 = 1.877, 0.904, 0.367
K11, K12, K13 = 1.782, 0.782, 0.214

#: Identity of this transcription AS EVIDENCE, independent of the
#: production impedance MODEL_VERSION: reference-only changes move this
#: marker and leave production versioning untouched. Revision 1 was the
#: initial pin (immersed equation, cover variants, figure anchors);
#: revision 2 bound equation (11) to the original material triple,
#: added the finite-input discipline and the artifact fingerprints;
#: revision 3 recorded the render recipe, split this identity from the
#: production version, and scoped the asymptote and inconsistency
#: wording to the evidence. The transcribed artifact itself is
#: anchored by SOURCE_ARTIFACT["sha256"].
REFERENCE_VERSION = "3"

PAPER = {
    "authors": "Barbuto, Alu, Bilotti, Toscano, Vegni",
    "title": "Characteristic impedance of a microstrip line with a "
             "dielectric overlay",
    "journal": "COMPEL",
    "volume": "32(6)",
    "pages": "1855-1867",
    "year": 2013,
    "doi": "10.1108/COMPEL-10-2012-0283",
}

#: The exact supplied PDF the transcription was made from. The file is
#: copyrighted and deliberately not committed; these fingerprints let a
#: future reviewer verify they hold the same artifact.
SOURCE_ARTIFACT = {
    "kind": "author-provided PDF of the published paper",
    "sha256": "618bce2839878e7725f14ec7264d70a666116e505744290d0b"
              "4d953714e4cad4",
    "bytes": 131045,
    "page_count": 13,
    "doi": "10.1108/COMPEL-10-2012-0283",
    "published_pages": "1855-1867",
}

#: How the adjudicating renders were produced. Recorded so the images
#: can be regenerated for visual re-adjudication from the fingerprinted
#: PDF; NOT a byte-reproduction contract - raster antialiasing and PNG
#: encoding vary across renderer versions, so the hashes below identify
#: the exact images judged rather than promising to be recomputable.
RENDER_RECIPE = {
    "renderer": "pymupdf 1.28.2 (MuPDF 1.28.2)",
    "call": "page.get_pixmap(dpi=<dpi>, clip=<clip>) followed by "
            "Pixmap.save(<path>.png)",
    "clip_convention": "clip fractions (left, top, right, bottom) of "
                       "page.rect, applied as x0 + left*width, "
                       "y0 + top*height, x0 + right*width, "
                       "y0 + bottom*height, exact float arithmetic, "
                       "no rounding",
    "colorspace": "RGB",
    "alpha": False,
    "annotations": "renderer default (annotations included)",
    "sha256_of": "the PNG file bytes as written by Pixmap.save",
}

#: The equation-region renders that materially adjudicated the
#: transcription. Page indices are zero-based; clips follow
#: RENDER_RECIPE["clip_convention"]; hashes follow
#: RENDER_RECIPE["sha256_of"].
TRANSCRIPTION_RENDERS = (
    {"role": "s(w/d), Corr, ExpCorr and k1..k7", "page_index": 5,
     "dpi": 340, "clip": (0.18, 0.50, 1.00, 0.95),
     "sha256": "ed4cbe12d945f7900178a003a4bf574375c741c8453b7ef448"
               "ddce15d4afa263"},
    {"role": "equation (10), k8..k13 and equation (11)",
     "page_index": 6, "dpi": 300, "clip": (0.05, 0.35, 1.00, 0.80),
     "sha256": "8d225a915d03f40a0d5452658ca8abe6883a5dc17dbfeaa74d"
               "54590cb322e47e"},
    {"role": "equation (10) exponent signs at 560 dpi",
     "page_index": 6, "dpi": 560, "clip": (0.25, 0.375, 0.92, 0.50),
     "sha256": "9374e64fa00eb88aa7395c78b4e7248fed03d3ec78dfe06455"
               "1376a7c708be29"},
    {"role": "Figure 6 anchor readings", "page_index": 8, "dpi": 400,
     "clip": (0.20, 0.55, 0.80, 0.97),
     "sha256": "cfb6c83ca82882a0e76b680b4afdd8b3a7845cc25b09a9ee5d"
               "022fabb3893afa"},
)


def _finite(label, value):
    """The toolkit's finite-number discipline: bools, NaN and the
    infinities refuse instead of propagating into reference output."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or value != value \
            or value in (float("inf"), float("-inf")):
        raise propagation.Unsupported(
            "{} = {!r} is not a finite number".format(label, value))


def _validate(epsilon_r, epsilon_rc, w_over_d):
    _finite("epsilon_r", epsilon_r)
    _finite("epsilon_rc", epsilon_rc)
    _finite("w/d", w_over_d)
    for label, value in (("epsilon_r", epsilon_r),
                         ("epsilon_rc", epsilon_rc)):
        if not value >= 1.0:
            raise propagation.Unsupported(
                "{} = {!r} is outside the reference's physical domain "
                "(a relative permittivity of at least 1)".format(
                    label, value))
    if not 0.0 < w_over_d <= 10.0:
        raise propagation.Unsupported(
            "w/d = {!r} is outside the paper's stated fit range "
            "0 < w/d <= 10 (its equation (11))".format(w_over_d))
    if epsilon_rc / epsilon_r > 10.0:
        raise propagation.Unsupported(
            "eps_rc/eps_r = {:.3f} is outside the paper's stated fit "
            "range <= 10 (its equation (11))".format(
                epsilon_rc / epsilon_r))


def _shape(w_over_d):
    """The paper's s(w/d): Hammerstad-shaped, 0.04 term linear as printed."""
    base = (1.0 + 12.0 / w_over_d) ** -0.5
    if w_over_d >= 1.0:
        return base
    return base + 0.04 * (1.0 - w_over_d)


def _exponent_correction(w_over_d):
    """The paper's ExpCorr(w/d), constants k2..k7 as printed."""
    if w_over_d <= 2.0:
        return K2 / (w_over_d ** K3 + K4)
    return K5 / (w_over_d ** K6 + K7)


def immersed_epsilon(epsilon_r, epsilon_rc, w_over_d):
    """Equation (8): the microstrip immersed in eps_rc, as printed.

    Zero-thickness strip, substrate eps_r below, homogeneous eps_rc
    everywhere above. Checked in the tests for asymptotic consistency
    with the paper's own largest-thickness plotted readings (Figures
    4, 5 and 7 - the plotted curves are large-but-finite covers, so
    these are consistency checks against the paper's stated limiting
    behavior, not direct infinite-superstrate data) and, through the
    figure-consistent cover composition, Figures 6, 8 and 10 - which
    together exercise both Corr branches.
    """
    _validate(epsilon_r, epsilon_rc, w_over_d)
    if epsilon_r >= epsilon_rc:
        correction = 1.0
    else:
        correction = (epsilon_rc / epsilon_r) \
            ** _exponent_correction(w_over_d)
    return (epsilon_r + epsilon_rc) / 2.0 \
        + K1 * (epsilon_r - epsilon_rc) * _shape(w_over_d) * correction


def _cover_epsilon(epsilon_rc, dc_over_d, w_over_d, sign):
    # The eps_rc/eps_r ratio window is the immersion equation's to
    # enforce against the actual substrate; here only the cover's own
    # inputs are checked.
    _finite("epsilon_rc", epsilon_rc)
    _finite("w/d", w_over_d)
    _finite("dc/d", dc_over_d)
    if not epsilon_rc >= 1.0:
        raise propagation.Unsupported(
            "epsilon_rc = {!r} is outside the reference's physical "
            "domain (a relative permittivity of at least 1)".format(
                epsilon_rc))
    if not 0.0 < w_over_d <= 10.0:
        raise propagation.Unsupported(
            "w/d = {!r} is outside the paper's stated fit range "
            "0 < w/d <= 10 (its equation (11))".format(w_over_d))
    if dc_over_d < 0.0:
        raise propagation.Unsupported(
            "dc/d = {!r} is not a usable normalized cover "
            "thickness".format(dc_over_d))
    if w_over_d >= 1.0:
        argument = K8 * dc_over_d ** K9 * w_over_d ** (sign * K10)
    else:
        argument = K11 * dc_over_d ** K12 * w_over_d ** (sign * K13)
    return 1.0 + (2.0 / math.pi) * (epsilon_rc - 1.0) \
        * math.atan(argument)


def cover_epsilon_as_printed(epsilon_rc, dc_over_d, w_over_d):
    """Equation (10) verbatim: positive w/d exponents, as printed.

    Kept because it is what the paper prints; measured against the
    paper's own Figures 5, 6, 8 and 10 the composition built on it
    misses the plotted curves by +0.18 to +0.23 - the documented
    printed-vs-figure inconsistency, not a transcription choice.
    """
    return _cover_epsilon(epsilon_rc, dc_over_d, w_over_d, +1.0)


def cover_epsilon_figure_consistent(epsilon_rc, dc_over_d, w_over_d):
    """Equation (10) with the sign of the w/d exponent reversed.

    NOT what the paper prints. It is the figure-consistent CANDIDATE
    correction - a probable sign typo, stated as this toolkit's
    inference - that reproduces the paper's own validation figures to
    plot-reading precision; adopting it as authority would require an
    author- or publisher-confirmed erratum or an independent primary
    source.
    """
    return _cover_epsilon(epsilon_rc, dc_over_d, w_over_d, -1.0)


def covered_epsilon_as_printed(epsilon_r, epsilon_rc, w_over_d,
                               dc_over_d):
    """Equation (12) composed with the cover equation as printed.

    Equation (11) is enforced on the ORIGINAL material triple
    (epsilon_r, epsilon_rc, w/d) before the cover transform: a cover
    material outside the paper's stated eps_rc/eps_r <= 10 fit range
    refuses at any thickness, because the fit's validity is stated in
    the materials, not in the thickness-reduced equivalent.
    """
    _validate(epsilon_r, epsilon_rc, w_over_d)
    return immersed_epsilon(
        epsilon_r,
        cover_epsilon_as_printed(epsilon_rc, dc_over_d, w_over_d),
        w_over_d)


def covered_epsilon_figure_consistent(epsilon_r, epsilon_rc, w_over_d,
                                      dc_over_d):
    """Equation (12) composed with the figure-consistent cover variant.

    Equation (11) is enforced on the ORIGINAL material triple before
    the cover transform, exactly as in the as-printed composition.
    """
    _validate(epsilon_r, epsilon_rc, w_over_d)
    return immersed_epsilon(
        epsilon_r,
        cover_epsilon_figure_consistent(epsilon_rc, dc_over_d,
                                        w_over_d),
        w_over_d)
