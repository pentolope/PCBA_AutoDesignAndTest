"""Build the complete component list and netlist for the microphone array.

`build()` returns `(components, nets)` where components carry both their
electrical identity and their board placement, and nets map a net name to the
list of `(reference, pad)` pairs on it. The schematic and PCB generators both
consume this, so there is exactly one description of the design.
"""

import design as d

# Manufacturer / LCSC data captured during component selection. Every entry was
# confirmed present in the JLCPCB assembly catalogue with non-zero stock.
PARTS = {
    "mic": ("MSM261DHP006", "MEMSensing", "C22390138"),
    "ldo": ("LP5907MFX-3.3/NOPB", "Texas Instruments", "C80670"),
    "buffer": ("SN74LVC244APWR", "Texas Instruments", "C7668"),
    "osc": ("OT322524.576MJBA4SL", "YXC", "C2831388"),
    "esd": ("USBLC6-4SC6", "STMicroelectronics", "C111212"),
    "schottky": ("SS14", "MDD", "C2480"),
    "ptc": ("JK-MSMD050-30", "Jinrui", "C369168"),
    "ferrite": ("GZ2012D601TF", "Sunlord", "C1017"),
    "r22": ("0402WGF220JTCE", "UNI-ROYAL", "C25092"),
    "r33": ("0402WGF330JTCE", "UNI-ROYAL", "C25105"),
    "r100": ("0402WGF1000TCE", "UNI-ROYAL", "C25076"),
    "c100n": ("CL05B104KO5NNNC", "Samsung", "C1525"),
    "c1u": ("CC0603KRX7R8BB105", "YAGEO", "C106858"),
    "c10u": ("CL21A106KAYNNNE", "Samsung", "C15850"),
    "c22u": ("C2012X5R1C226KT000E", "TDK", "C76637"),
}


class Design:
    def __init__(self):
        self.components = []
        self.nets = {}

    def add(self, ref, value, symbol, footprint, x, y, rot=0.0, side="top",
            part=None, dnp=False, in_bom=True, description=""):
        mpn, mfr, lcsc = PARTS.get(part, ("", "", "")) if part else ("", "", "")
        self.components.append({
            "ref": ref, "value": value, "symbol": symbol, "footprint": footprint,
            "x": x, "y": y, "rot": rot % 360.0, "side": side,
            "mpn": mpn, "manufacturer": mfr, "lcsc": lcsc,
            "dnp": dnp, "in_bom": in_bom, "description": description,
        })
        return ref

    def connect(self, net, *pins):
        bucket = self.nets.setdefault(net, [])
        for ref, pad in pins:
            bucket.append((ref, str(pad)))


def build():
    b = Design()

    # ------------------------------------------------------------------
    # Microphone ring: 16 channels, 8 L/R-shared pairs
    # ------------------------------------------------------------------
    for k in range(d.MIC_COUNT):
        ref = f"MK{k + 1}"
        angle = d.mic_angle(k)
        pair = k // 2
        mx, my = d.polar(d.MIC_BODY_RADIUS, angle)
        b.add(ref, "MSM261DHP006", "MicArrayV2:MSM261DHP006", d.KI_FP["mic"],
              mx, my, d.radial_rotation(angle), part="mic",
              description=f"PDM microphone channel {k}")

        rv, cm, rd = f"RV{k + 1}", f"CM{k + 1}", f"RD{k + 1}"
        rvx, rvy = d.polar(d.MIC_SUPPLY_RADIUS, angle + d.MIC_RES_OFFSET_DEG)
        cmx, cmy = d.polar(d.MIC_SUPPLY_RADIUS, angle + d.MIC_CAP_OFFSET_DEG)
        rdx, rdy = d.polar(d.MIC_DATA_RADIUS, angle)
        b.add(rv, "100R", "Device:R", d.KI_FP["r0402"], rvx, rvy, angle,
              part="r100", description=f"channel {k} supply isolation")
        # Rotated 180 degrees so the ground pad, and therefore its stitching
        # via, faces radially inward instead of sitting in the microphone's
        # clock escape corridor.
        b.add(cm, "100nF", "Device:C", d.KI_FP["c0402"], cmx, cmy, angle + 180.0,
              part="c100n", description=f"channel {k} local decoupling")
        # Rotated so pad 1 faces the microphone: the damping resistor has to
        # take the microphone's output on its outward pad and hand the shared
        # pair line off inward, otherwise every data escape has to run past the
        # far pad to reach the near one.
        b.add(rd, "22R", "Device:R", d.KI_FP["r0402"], rdx, rdy, angle + 180.0,
              part="r22", description=f"channel {k} PDM data series damping")

        vdd = f"MIC_VDD_{k}"
        b.connect("+3V3A", (rv, 1))
        b.connect(vdd, (rv, 2), (cm, 1), (ref, 1))
        b.connect("GND", (cm, 2), (ref, 5), (ref, 6), (ref, 7), (ref, 8))
        # Even channels strap L/R low and assert data on the falling clock edge;
        # odd channels strap it high and use the rising edge, so a pair shares
        # one data line.
        b.connect("GND" if k % 2 == 0 else vdd, (ref, 2))
        b.connect(f"PDM_CLK_B{pair}", (ref, 3))
        b.connect(f"MIC_DOUT_{k}", (ref, 4), (rd, 1))
        b.connect(f"PDM_D{pair}", (rd, 2))

    # Quadrant bulk capacitors on the microphone ring rail
    for i, angle in enumerate(d.BULK_ANGLES):
        ref = f"CB{i + 1}"
        cx, cy = d.polar(d.BULK_RADIUS, angle)
        b.add(ref, "10uF", "Device:C", d.KI_FP["c0805"], cx, cy, angle,
              part="c10u", description="microphone ring bulk decoupling")
        b.connect("+3V3A", (ref, 1))
        b.connect("GND", (ref, 2))

    # ------------------------------------------------------------------
    # Clock generation and fan-out
    # ------------------------------------------------------------------
    b.add("X1", "24.576MHz", "MicArrayV2:Oscillator_4pin", d.KI_FP["osc"],
          -7.0, 7.0, 0.0, part="osc", description="audio master clock")
    b.add("C8", "100nF", "Device:C", d.KI_FP["c0402"], -7.0, 3.2, 0.0,
          part="c100n", description="X1 decoupling")
    b.add("R1", "33R", "Device:R", d.KI_FP["r0402"], -2.0, 7.0, 0.0,
          part="r33", description="MCLK series damping")
    b.connect("+3V3_CLK", ("X1", 4), ("X1", 1), ("C8", 1))
    b.connect("GND", ("X1", 2), ("C8", 2))
    b.connect("MCLK_OSC", ("X1", 3), ("R1", 1))
    b.connect("AUDIO_MCLK", ("R1", 2))

    b.add("U2", "SN74LVC244A", "MicArrayV2:SN74LVC244A", d.KI_FP["tssop20"],
          0.0, -3.0, 0.0, part="buffer", description="PDM clock fan-out buffer")
    b.add("C6", "100nF", "Device:C", d.KI_FP["c0402"], 0.0, -9.2, 0.0,
          part="c100n", description="U2 decoupling")
    b.add("C7", "1uF", "Device:C", d.KI_FP["c0603"], 4.2, -9.2, 0.0,
          part="c1u", description="U2 decoupling")
    b.add("R2", "33R", "Device:R", d.KI_FP["r0402"], -7.5, -1.0, 0.0,
          part="r33", description="PDM clock input series damping")
    b.connect("+3V3_CLK", ("U2", 20), ("C6", 1), ("C7", 1))
    b.connect("GND", ("U2", 10), ("C6", 2), ("C7", 2), ("U2", 1), ("U2", 19))
    b.connect("PDM_CLK_FPGA", ("R2", 1))
    b.connect("PDM_CLK_IN", ("R2", 2),
              ("U2", 2), ("U2", 4), ("U2", 6), ("U2", 8),
              ("U2", 11), ("U2", 13), ("U2", 15), ("U2", 17))

    # Branch terminators sit in two columns flanking U2 inside the band between
    # the socket rows. Branch n feeds the pair at azimuth 45n..45n+22.5, so the
    # columns are ordered to keep each branch on its own side of the board.
    # Outputs are assigned so each branch leaves on the side of the package its
    # terminating resistor sits on. The natural 1Y1..2Y4 order would send four
    # branches straight back across the buffer.
    # Ordered so each output pin's position down the package matches its
    # resistor's position down the column beside it. The obvious 1Y1..2Y4 order
    # inverts one side and makes all four tracks cross.
    #
    # Within a column the resistors are also ordered by the azimuth their
    # branch has to reach, sweeping from the most downward at the bottom to the
    # most upward at the top. Getting this backwards on the right-hand column
    # forced branch 1 (56 degrees, up) to leave below branch 0 (11 degrees) and
    # cross it on the way out.
    branch_outputs = ["16", "18", "3", "5", "7", "9", "12", "14"]
    branch_positions = [(8.0, 1.5), (8.0, 4.0), (-11.5, 4.0), (-11.5, 1.5),
                        (-11.5, -1.0), (-11.5, -3.5), (8.0, -3.5), (8.0, -1.0)]
    for n, out_pin in enumerate(branch_outputs):
        ref = f"RC{n + 1}"
        rx, ry = branch_positions[n]
        b.add(ref, "33R", "Device:R", d.KI_FP["r0402"], rx, ry, 0.0,
              part="r33", description=f"PDM clock branch {n} series termination")
        b.connect(f"PDM_CLK_Y{n}", ("U2", out_pin), (ref, 1))
        b.connect(f"PDM_CLK_B{n}", (ref, 2))

    # ------------------------------------------------------------------
    # Power input and regulation
    # ------------------------------------------------------------------
    row, row2 = d.POWER_ROW_Y, d.POWER_ROW2_Y
    b.add("F1", "500mA", "Device:Fuse", d.KI_FP["fuse1812"], -21.0, row, 0.0,
          part="ptc", description="resettable fuse on the Pi 5 V feed")
    # Cathode to the right, so the anode faces the fuse it is fed from. The
    # other way round the fuse has to reach across the diode's own output pad
    # to get to its input.
    b.add("D1", "SS14", "Device:D_Schottky", d.KI_FP["sma"], -13.5, row, 180.0,
          part="schottky", description="blocks Tang USB-C back-feed into the Pi")
    b.add("C4", "22uF", "Device:C", d.KI_FP["c0805"], -6.5, row, 0.0,
          part="c22u", description="5 V bulk")
    b.add("C5", "100nF", "Device:C", d.KI_FP["c0402"], -6.5, row2, 0.0,
          part="c100n", description="5 V high frequency bypass")
    b.connect("PI_5V", ("F1", 1))
    b.connect("5V_FUSED", ("F1", 2), ("D1", 2))
    b.connect("+5V", ("D1", 1), ("C4", 1), ("C5", 1))
    b.connect("GND", ("C4", 2), ("C5", 2))

    b.add("U1", "LP5907MFX-3.3", "MicArrayV2:LP5907MFX-3.3", d.KI_FP["sot235"],
          3.0, row, 0.0, part="ldo", description="low noise 3.3 V microphone supply")
    b.add("C1", "1uF", "Device:C", d.KI_FP["c0603"], -1.5, row, 0.0,
          part="c1u", description="LDO input")
    b.add("C2", "1uF", "Device:C", d.KI_FP["c0603"], 7.5, row, 0.0,
          part="c1u", description="LDO output")
    b.add("C3", "10uF", "Device:C", d.KI_FP["c0805"], 7.5, row2, 0.0,
          part="c10u", description="3V3A bulk")
    b.connect("+5V", ("U1", 1), ("U1", 3), ("C1", 1))
    b.connect("GND", ("U1", 2), ("C1", 2), ("C2", 2), ("C3", 2))
    b.connect("+3V3A", ("U1", 5), ("C2", 1), ("C3", 1))

    b.add("FB1", "600R@100MHz", "Device:FerriteBead", d.KI_FP["l0805"],
          12.5, row, 0.0, part="ferrite",
          description="isolates clock circuitry from the microphone rail")
    b.add("C9", "1uF", "Device:C", d.KI_FP["c0603"], 16.5, row, 0.0,
          part="c1u", description="3V3_CLK bulk")
    b.connect("+3V3A", ("FB1", 1))
    b.connect("+3V3_CLK", ("FB1", 2), ("C9", 1))
    b.connect("GND", ("C9", 2))

    # ------------------------------------------------------------------
    # Tang Nano 9K sockets
    # ------------------------------------------------------------------
    # The 1x24 header footprint has its origin on pin 1, not at the package
    # centre, and on the bottom side rotation 90 lays the pins out towards -X.
    # Placing the origin at the +X end therefore puts pin 1 at the module's
    # USB-C end, matching the Sipeed header numbering.
    for ref, row_y in (("J2", d.TANG_ROW_SPACING / 2.0),
                       ("J3", -d.TANG_ROW_SPACING / 2.0)):
        b.add(ref, "Tang_Nano_9K", f"MicArrayV2:TangNano9K_{ref}", d.KI_FP["hdr1x24"],
              d.tang_socket_x(1), row_y, 90.0, side="bottom", dnp=True, in_bom=False,
              description="hand-soldered 1x24 female socket for the Tang Nano 9K")
        for position in range(1, d.TANG_PINS + 1):
            net = d.TANG_NET_MAP.get((ref, position))
            if net:
                b.connect(net, (ref, position))

    # ------------------------------------------------------------------
    # Raspberry Pi host interface
    # ------------------------------------------------------------------
    # Same origin-on-pin-1 convention: on the bottom at rotation 90 the odd row
    # runs towards -X and the even row sits 2.54 mm towards -Y, so this origin
    # centres the 26-way shroud on PI_HEADER_POS.
    b.add("J1", "RPi_P1_26", "MicArrayV2:RPi_P1_26", d.KI_FP["idc2x13"],
          d.PI_HEADER_POS[0] + 15.24, d.PI_HEADER_POS[1] + 1.27, 90.0,
          side="bottom", dnp=True, in_bom=False,
          description="hand-soldered 26-way IDC to the 2012 Raspberry Pi P1 header")
    for pin, net in d.PI_HEADER.items():
        b.connect(net, ("J1", pin))

    # Both arrays sit directly above the Pi header, under the signal fan, so
    # every clamp is a short stub off its line rather than a detour.
    b.add("U3", "USBLC6-4SC6", "MicArrayV2:USBLC6-4SC6", d.KI_FP["sot236"],
          -9.5, d.ESD_ROW_Y, 0.0, part="esd",
          description="ESD protection, four leftmost host lines")
    b.add("U4", "USBLC6-4SC6", "MicArrayV2:USBLC6-4SC6", d.KI_FP["sot236"],
          -1.0, d.ESD_ROW_Y, 0.0, part="esd",
          description="ESD protection, four rightmost host lines")
    b.connect("GND", ("U3", 2), ("U4", 2))
    b.connect("TANG_3V3", ("U3", 5), ("U4", 5))

    for i, (pi_net, board_net, _, res, esd_ref, esd_pin) in enumerate(d.HOST_SIGNALS):
        x = -13.5 + i * 2.6
        b.add(res, "33R", "Device:R", d.KI_FP["r0402"], x, d.HOST_RESISTOR_ROW_Y, 90.0,
              part="r33", description=f"{board_net} series damping")
        b.connect(pi_net, (res, 1), (esd_ref, esd_pin))
        b.connect(board_net, (res, 2))

    # ------------------------------------------------------------------
    # Mechanical and test provisions
    # ------------------------------------------------------------------
    for i, angle in enumerate(d.MOUNT_ANGLES):
        mx, my = d.polar(d.MOUNT_RADIUS, angle)
        b.add(f"H{i + 1}", "M3", "Mechanical:MountingHole", d.KI_FP["mount"],
              mx, my, 0.0, in_bom=False, description="M3 mounting hole")

    # Probe pads. Their positions are not on a ring any more - each one sits on
    # a piece of its own net that is already routed, so it needs no track of
    # its own, and where that could go depends on the routing rather than on
    # any formula. tools/place_testpoints.py chooses them against the board and
    # records the result, which is read back here so the schematic, the BOM and
    # the board all agree.
    for ref, net, footprint, tx, ty in d.TEST_POINTS:
        b.add(ref, net, "Connector:TestPoint", footprint, tx, ty, 0.0,
              in_bom=False, description=f"test point on {net}")
        b.connect(net, (ref, 1))

    return b.components, b.nets


def expected_unconnected():
    """Pins that are deliberately left open, as (reference, pad) pairs.

    Declared independently of the generators so the parity check can assert
    that nothing else drifted into being unconnected.
    """
    open_pins = set()
    for pin in range(1, 27):
        if pin not in d.PI_HEADER:
            open_pins.add(("J1", str(pin)))
    for ref, labels in (("J2", d.TANG_J5), ("J3", d.TANG_J6)):
        for position in range(1, len(labels) + 1):
            if (ref, position) not in d.TANG_NET_MAP:
                open_pins.add((ref, str(position)))
    open_pins.add(("U1", "4"))  # LP5907 pin 4 is a no-connect
    return open_pins


if __name__ == "__main__":
    components, nets = build()
    print(f"deliberately open pins: {len(expected_unconnected())}")
    placed = [c for c in components if c["footprint"]]
    print(f"components: {len(components)}  nets: {len(nets)}")
    print(f"bom lines : {len([c for c in components if c['in_bom']])}")
    pads = sum(len(v) for v in nets.values())
    print(f"pad connections: {pads}")
    singles = [n for n, v in nets.items() if len(v) < 2]
    print(f"single-pad nets: {singles}")
