"""Transmission-line closed forms. Physics, not any manufacturer's policy.

Hammerstad's microstrip synthesis, the coated-microstrip effective permittivity
chord, and the symmetric-stripline form. Each takes geometry and materials and
returns an impedance or a permittivity; none of them knows what a catalog is,
which fabricator published it, or whether the geometry is manufacturable.

The JLCPCB catalog code supplies the numbers these are evaluated at and decides
whether the answer may be built. That decision belongs to a manufacturer. These
equations do not.
"""

from __future__ import annotations

import math

from . import propagation

#: Free-space wave impedance, the constant every microstrip form is scaled by.
FREE_SPACE_ETA_OHM = 376.730313668


def _hammerstad_geometry(epsilon_r, width_mm, height_mm, conductor_mm,
                         _force_branch):
    """z_air and the bare effective permittivity of the Hammerstad model.

    Shared verbatim by the bare and coated external-microstrip forms so
    the two can never drift in transcription: the effective width, the
    validity window, the two-branch geometry factor and the bare
    permittivity are computed exactly once, here.
    """
    effective_width = propagation.thickness_corrected_width(
        width_mm, height_mm, conductor_mm)
    epsilon_bare = propagation.hammerstad_effective_permittivity(
        epsilon_r, effective_width, height_mm)
    u = effective_width / height_mm
    if not 0.1 <= u <= 20.0:
        raise propagation.Unsupported(
            "microstrip w_eff/h = {:.4f} lies outside the 0.1-20 validity "
            "window of the Hammerstad impedance form".format(u))
    narrow = u <= 1.0 if _force_branch is None \
        else _force_branch == "narrow"
    if narrow:
        z_air = 60.0 * math.log(8.0 / u + u / 4.0)
    else:
        z_air = (FREE_SPACE_ETA_OHM
                 / (u + 1.393 + 0.667 * math.log(u + 1.444)))
    return z_air, epsilon_bare


def microstrip_z0(epsilon_r, width_mm, height_mm, conductor_mm,
                  _force_branch=None):
    """Hammerstad Z0 for a bare microstrip of finite conductor thickness.

    The effective width and effective permittivity come from the same
    helpers the timing propagation model uses, so impedance and delay can
    never quietly disagree about the geometry. The two-branch Hammerstad
    Z0 is used with its usual validity window on u = w_eff/h; a geometry
    outside it refuses rather than extrapolating. The geometry factor is
    computed by ``_hammerstad_geometry``, shared with the coated model.
    """
    z_air, epsilon_effective = _hammerstad_geometry(
        epsilon_r, width_mm, height_mm, conductor_mm, _force_branch)
    return z_air / math.sqrt(epsilon_effective), epsilon_effective


def coated_microstrip_epsilon(epsilon_r, epsilon_mask, epsilon_bare):
    """The declared linear-chord two-media permittivity reading.

    What is algebra and what is assumption, separated exactly:
    q = (eps_bare - 1)/(eps_r - 1) is merely the DEFINITION of q by
    rearranging the pinned Hammerstad bare result; it proves nothing
    beyond eps_bare itself. REUSING that q with the upper half-space
    at eps_mask,

        eps = q*eps_r + (1 - q)*eps_mask,

    is a modeling assumption of THIS toolkit: a straight chord in the
    upper permittivity between two exact anchors (eps_mask = 1
    reproduces the bare form by construction; eps_mask = eps_r is the
    homogeneous medium, exact by physics). No pinned source licenses
    the interior. For the exact electrostatic problem the true
    effective permittivity is concave in the upper permittivity at
    fixed geometry (capacitance is an infimum of energy functionals
    each linear in the permittivity field), so a chord drawn between
    the TRUE endpoints would read low in effective permittivity -
    equivalently high in impedance - against the true curve between
    the anchors. That comparison does NOT transfer to this chord,
    whose bare endpoint is the Hammerstad approximation rather than
    the true value: no ordering between this loaded edge and the
    true infinite-superstrate response is established in either
    direction, and none is claimed. It is a model edge, not a proven
    physical bound, and the result says so.

    The Barbuto reference is now pinned: an author-provided copy of
    Barbuto, Alu, Bilotti, Toscano, Vegni, "Characteristic impedance
    of a microstrip line with a dielectric overlay" (COMPEL 32(6),
    2013) has been transcribed verbatim into
    ``pcbqa.overlay_reference`` - the immersed two-media
    equation (8) validated against the paper's own figures on both of
    its branches, the finite-cover equation (10) carrying a
    documented printed-vs-figure inconsistency (its printed w/d
    exponent sign contradicts the paper's own validation figures; the
    sign reversal is a candidate correction, not a confirmed
    erratum), and the w/d < 1 cover branch unvalidated by any
    figure. The reference is
    EVIDENCE; nothing here dispatches it. What still stands between
    it and a production loaded edge, exactly: (a) the paper models a
    zero-thickness strip, so a mapping from this solver's
    finite-thickness trapezoid geometry onto its w/d must be declared
    and defended; (b) the paper's air limit uses k1 = 0.52 and a
    linear 0.04 shape term where the Hammerstad form pinned by this
    solver uses one half and the square, so mixing the two families
    inside one enclosure would put the edges on inconsistent bare
    baselines; (c) a finite-cover point model would additionally need
    the printed-vs-figure inconsistency resolved by an
    authoritative text and a defensible mapping of the fabricator's
    three stated mask thicknesses onto the paper's single uniform
    overlay; no erratum is established, and the printed-vs-figure
    inconsistency remains unresolved.
    Svacina (IEEE MTT 40(4), 1992) and Bahl and Stuchly (IEEE MTT,
    1980) remain unobtained.

    Promotion decision (model version 12): equation (8) was NOT
    promoted into this dispatched edge. Measured across this
    fabricator's constructions, this chord and the pinned equation
    (8) agree in effective permittivity within 0.4 percent at matched
    widths - the same functional form up to k1 = 0.52 versus one half
    and the sub-unity shape term - so promotion buys no material
    accuracy. The zero-thickness width convention the reference
    requires would instead shift the loaded width by roughly 7 to 50
    percent across representative targets (measured, asserted in the
    tests): that shift is dominated by the thickness/width-convention
    leg of the decomposition (the model-family leg stays under half a
    percent on widths - see _CHORD_CALIBRATION), a sensitivity no
    source licenses a mapping across, and dispatching the reference
    would trade
    a sub-percent sourced refinement for a double-digit unmodeled
    geometry change. The chord stays, characterized instead of
    floating; the reference stays evidence.
    """
    if epsilon_r <= 1.0:
        raise propagation.Unsupported(
            "the filling-fraction decomposition needs epsilon_r > 1 "
            "(got {})".format(epsilon_r))
    q = (epsilon_bare - 1.0) / (epsilon_r - 1.0)
    return q * epsilon_r + (1.0 - q) * epsilon_mask


def coated_microstrip_z0(epsilon_r, epsilon_mask, width_mm, height_mm,
                         conductor_mm, _force_branch=None):
    """The LOADED model edge of the coated-microstrip enclosure.

    This is deliberately not a point model of a thin soldermask, and -
    since model version 8 - it is not presented as a physical bound
    either: it is the declared linear-chord two-media reading at the
    fabricator's stated mask permittivity (see
    ``coated_microstrip_epsilon`` for exactly what is algebra, what is
    assumption, and which primary sources would license more). The
    fabricator's stated mask thicknesses are deliberately NOT
    consumed: no pinned form maps a thickness to a permittivity
    weight, and an invented weighting would be fabricated precision.

    Monotonicity of the inverse is established for
    1 <= epsilon_mask <= epsilon_r (the chord permittivity is then
    non-decreasing in width while z_air strictly decreases); outside
    that window the form refuses. The geometry factor, validity window
    and branch structure are shared verbatim with ``microstrip_z0``,
    so the loaded edge has exactly the bare model's u = 1 seam and no
    other.
    """
    z_air, epsilon_bare = _hammerstad_geometry(
        epsilon_r, width_mm, height_mm, conductor_mm, _force_branch)
    if not 1.0 <= epsilon_mask <= epsilon_r:
        raise propagation.Unsupported(
            "the coated enclosure is established for 1 <= mask Dk <= "
            "substrate Dk; got mask {} against substrate {}".format(
                epsilon_mask, epsilon_r))
    epsilon = coated_microstrip_epsilon(epsilon_r, epsilon_mask,
                                        epsilon_bare)
    return z_air / math.sqrt(epsilon), epsilon


def stripline_z0(epsilon_r, width_mm, plate_gap_mm, conductor_mm,
                 _force_branch=None):
    """Symmetric-stripline Z0 with finite conductor thickness.

    `plate_gap_mm` is b: the span between the two reference planes.

    Narrow strips - w/(b-t) < 0.35 - implement, exactly:

        Z0 = 60/sqrt(er) * ln( 4*b / (0.67*pi*(0.8*w + t)) )

    which is the IPC-2141 / Wadell closed form. Analog Devices tutorial
    MT-094 prints the algebraically near-equivalent Z0 = 60/sqrt(er) *
    ln(1.9*b/(0.8*w + t)) - "near" because 4/(0.67*pi) = 1.9004, so the
    two prints differ by 0.02% in the constant; what runs here is the
    0.67*pi form, with no other transformation.

    Wide strips implement Cohn's fringing-capacitance characterisation
    (Cohn, "Characteristic Impedance of the Shielded-Strip Transmission
    Line", IRE MTT 1954, as reproduced by Wadell), untransformed.

    Stated validity, enforced here: w/(b-t) < 0.35 selects the narrow
    branch, t/b <= 0.25 throughout, w < b for the wide branch's
    derivation. Accuracy, measured not assumed: at t -> 0 against Cohn's
    exact elliptic solution the narrow form reads 2-4.5% low (worst near
    the branch seam) and the wide form tracks within 0.6%. At finite
    conductor thickness NO independent exact reference is available
    here, so the finite-thickness model error is NOT quantitatively
    bounded; the zero-thickness characterisation is the only measured
    anchor, and nothing here converts it into a finite-thickness claim.

    The two branches meet at w = 0.35*(b-t) with a thickness-dependent
    step: upward in width at small t/b, closing toward zero near t/b ~
    0.03. The inverse solver treats that seam as a first-class model
    boundary - see ``_solve_width``.

    Version note: the model-1 narrow branch used an equivalent-diameter
    expansion whose transcription could not be pinned to a printed
    source; it was replaced, not repaired.
    """
    propagation._positive("relative permittivity", epsilon_r)
    propagation._positive("trace width", width_mm)
    propagation._positive("plate gap", plate_gap_mm)
    propagation._positive("conductor thickness", conductor_mm)
    b = plate_gap_mm
    t = conductor_mm
    w = width_mm
    if t / b > 0.25:
        raise propagation.Unsupported(
            "stripline t/b = {:.3f} exceeds the 0.25 validity limit of "
            "the implemented forms".format(t / b))
    if w >= b:
        # Far outside the narrow/wide characterisations' derivations.
        raise propagation.Unsupported(
            "stripline w/b = {:.3f} is outside the implemented validity "
            "window".format(w / b))
    narrow = (w / (b - t) < 0.35) if _force_branch is None \
        else _force_branch == "narrow"
    if narrow:
        z0 = (60.0 / math.sqrt(epsilon_r)) * math.log(
            4.0 * b / (0.67 * math.pi * (0.8 * w + t)))
        return z0, epsilon_r
    ratio = 1.0 / (1.0 - t / b)
    fringing = (0.0885 * epsilon_r / math.pi) * (
        2.0 * ratio * math.log(ratio + 1.0)
        - (ratio - 1.0) * math.log(ratio * ratio - 1.0))
    z0 = (94.15 / math.sqrt(epsilon_r)) / (
        (w / b) * ratio + fringing / (0.0885 * epsilon_r))
    return z0, epsilon_r


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------

