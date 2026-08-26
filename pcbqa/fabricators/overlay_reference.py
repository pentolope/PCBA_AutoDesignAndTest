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
the printed figures cannot both be right - an internal erratum of the
paper. Both variants are implemented below, named for what they are;
neither is adopted as authority, and the w/d < 1 cover branch
(k11..k13) has no validation figure in the paper at all, so it is
entirely unvalidated in either sign.

Known deltas from the Hammerstad forms pinned in ``propagation``: the
paper's air limit (its equation (9)) carries k1 = 0.52 where the
classic Hammerstad coefficient is exactly one half, and its s(w/d)
shape term uses 0.04*(1 - w/d) LINEAR where Hammerstad prints the
square. Both are transcribed here as printed, which is why this
reference and the production bare model are different families.
"""

from __future__ import annotations

import math

from .. import propagation

#: The paper's fit constants, as printed.
K1, K2, K3, K4 = 0.52, 0.241, 0.715, 0.446
K5, K6, K7 = 1.814, 1.798, 12.52
K8, K9, K10 = 1.877, 0.904, 0.367
K11, K12, K13 = 1.782, 0.782, 0.214

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


def _validate(epsilon_r, epsilon_rc, w_over_d):
    for label, value in (("epsilon_r", epsilon_r),
                         ("epsilon_rc", epsilon_rc)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not value >= 1.0:
            raise propagation.Unsupported(
                "{} = {!r} is outside the reference's physical domain "
                "(a relative permittivity of at least 1)".format(
                    label, value))
    if isinstance(w_over_d, bool) or not isinstance(w_over_d,
                                                    (int, float)) \
            or not 0.0 < w_over_d <= 10.0:
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
    everywhere above. Validated in the tests against the paper's own
    Figure 4 and Figure 7 asymptotes directly and, through the
    figure-consistent cover composition, Figures 5, 6, 8 and 10 -
    which together exercise both Corr branches.
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
    if isinstance(epsilon_rc, bool) or not isinstance(epsilon_rc,
                                                      (int, float)) \
            or not epsilon_rc >= 1.0:
        raise propagation.Unsupported(
            "epsilon_rc = {!r} is outside the reference's physical "
            "domain (a relative permittivity of at least 1)".format(
                epsilon_rc))
    if isinstance(w_over_d, bool) or not isinstance(w_over_d,
                                                    (int, float)) \
            or not 0.0 < w_over_d <= 10.0:
        raise propagation.Unsupported(
            "w/d = {!r} is outside the paper's stated fit range "
            "0 < w/d <= 10 (its equation (11))".format(w_over_d))
    if isinstance(dc_over_d, bool) or not isinstance(dc_over_d,
                                                     (int, float)) \
            or dc_over_d < 0.0:
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
    erratum, not a transcription choice.
    """
    return _cover_epsilon(epsilon_rc, dc_over_d, w_over_d, +1.0)


def cover_epsilon_figure_consistent(epsilon_rc, dc_over_d, w_over_d):
    """Equation (10) with the sign of the w/d exponent reversed.

    NOT what the paper prints. It is the variant that reproduces the
    paper's own validation figures to plot-reading precision, recorded
    as evidence characterizing the erratum; adopting it as authority
    would require the authors' erratum notice or an independent
    primary source.
    """
    return _cover_epsilon(epsilon_rc, dc_over_d, w_over_d, -1.0)


def covered_epsilon_as_printed(epsilon_r, epsilon_rc, w_over_d,
                               dc_over_d):
    """Equation (12) composed with the cover equation as printed."""
    return immersed_epsilon(
        epsilon_r,
        cover_epsilon_as_printed(epsilon_rc, dc_over_d, w_over_d),
        w_over_d)


def covered_epsilon_figure_consistent(epsilon_r, epsilon_rc, w_over_d,
                                      dc_over_d):
    """Equation (12) composed with the figure-consistent cover variant."""
    return immersed_epsilon(
        epsilon_r,
        cover_epsilon_figure_consistent(epsilon_rc, dc_over_d,
                                        w_over_d),
        w_over_d)
