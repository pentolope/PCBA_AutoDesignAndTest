"""Single source of truth for the 16-channel PDM microphone array carrier.

Everything downstream - the KiCad symbol library, the schematic and the board -
is generated from the structures in this module, so the schematic and the PCB
cannot drift apart. KiCad's own `--schematic-parity` DRC is the gate that proves
the two generated artefacts still agree.

Geometry convention used throughout: board centre is the origin, +X is to the
right and +Y is up (ordinary mathematical convention). `to_kicad()` converts to
KiCad page coordinates, which put the origin at the top left with +Y downwards.
"""

import math

# --------------------------------------------------------------------------
# Board geometry
# --------------------------------------------------------------------------

BOARD_DIAMETER = 120.0
BOARD_RADIUS = BOARD_DIAMETER / 2.0
PAGE_CX = 150.0
PAGE_CY = 150.0

MIC_COUNT = 16
MIC_PORT_RADIUS = 54.0
# The acoustic port sits 0.78 mm from the package centre, towards the pad-7 end.
# Placing the package centre this much further in puts every port on R = 54.0.
MIC_PORT_OFFSET = 0.78
MIC_BODY_RADIUS = MIC_PORT_RADIUS - MIC_PORT_OFFSET
MIC_PITCH_DEG = 360.0 / MIC_COUNT

# Mounting holes and ring bulk capacitors sit on angles halfway between
# microphone channels so they clear the per-channel component clusters.
MOUNT_RADIUS = 38.0
# Mounting holes sit on the gaps *between* microphone pairs. Each pair's clock
# branch fans out along its own bisector at 11.25 + 45n degrees and its two
# arms sweep the 22.5 degrees between its microphones, which leaves the sectors
# centred on 33.75 + 45n free. Two of the old angles were pair bisectors, so
# branch 1 and branch 7 ran straight into a screw hole.
MOUNT_ANGLES = (33.75, 123.75, 213.75, 303.75)
MOUNT_DRILL = 3.2
BULK_RADIUS = 45.5
BULK_ANGLES = (33.75, 123.75, 213.75, 303.75)

# Per-channel cluster radii, chosen so no courtyard touches the microphone.
#
# The decoupling capacitor and the isolation resistor both sit on the same
# tangential side of the channel - the side the supply escape leaves from - so
# the whole MIC_VDD net is one short chain that never has to cross the data or
# clock escapes. A negative angular offset lands on that side.
MIC_SUPPLY_RADIUS = 48.8
MIC_CAP_OFFSET_DEG = -2.6
MIC_RES_OFFSET_DEG = -6.2
MIC_DATA_RADIUS = 45.0

# Tang Nano 9K module: 70 x 26 mm, two 24-way 2.54 mm rows 0.900 in apart.
# Shifted along +X so the USB-C end overhangs only ~15 mm of carrier, leaving
# room to insert a cable in the socket standoff gap.
TANG_CX = 10.0
TANG_ROW_SPACING = 22.86
TANG_PITCH = 2.54
TANG_PINS = 24
TANG_SPAN = (TANG_PINS - 1) * TANG_PITCH  # 58.42 mm

# The host block is stacked in signal order, outermost first: the Pi header,
# then the ESD arrays, then the series resistors, then the module socket. Every
# host signal therefore travels inward the whole way. With the header in the
# middle of the stack, each signal ran out to the resistors and back past the
# connector, and those doubling-back paths crossed each other.
#
# Note the header's second row sits 2.54 mm *inward* of its origin row, not
# outward, so the stack below has to clear -29.19 mm rather than the nominal
# header centre.
PI_HEADER_POS = (0.0, -33.0)
POWER_ROW_Y = -17.0
# The second power row was at -20.2, which left only 0.57 mm between the bulk
# capacitors and the host series resistors - too narrow for the 5 V input feed
# to pass along on the top layer. Raising it opens that lane to 0.87 mm.
POWER_ROW2_Y = -19.8
ESD_ROW_Y = -25.8
HOST_RESISTOR_ROW_Y = -22.3


def to_kicad(x, y):
    """Convert centre-origin, Y-up design coordinates to KiCad page coordinates."""
    return (PAGE_CX + x, PAGE_CY - y)


def polar(radius, degrees):
    """Design-frame cartesian coordinates for a polar position."""
    angle = math.radians(degrees)
    return (radius * math.cos(angle), radius * math.sin(angle))


def radial_rotation(degrees):
    """Footprint rotation that points a part's local +Y outward along `degrees`."""
    return (degrees - 90.0) % 360.0


def mic_angle(index):
    """Azimuth of microphone channel `index`, counter-clockwise from +X."""
    return index * MIC_PITCH_DEG


# --------------------------------------------------------------------------
# Library part definitions
# --------------------------------------------------------------------------

DATASHEETS = {
    "MicArrayV2:MSM261DHP006":
        "https://datasheet.lcsc.com/datasheet/pdf/55899cff9747905d6fde524be8eff620.pdf",
}

KI_FP = {
    "r0402": "Resistor_SMD:R_0402_1005Metric",
    "c0402": "Capacitor_SMD:C_0402_1005Metric",
    "c0603": "Capacitor_SMD:C_0603_1608Metric",
    "c0805": "Capacitor_SMD:C_0805_2012Metric",
    "l0805": "Inductor_SMD:L_0805_2012Metric",
    "sot235": "Package_TO_SOT_SMD:SOT-23-5",
    "sot236": "Package_TO_SOT_SMD:SOT-23-6",
    "tssop20": "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm",
    "sma": "Diode_SMD:D_SMA",
    "fuse1812": "Fuse:Fuse_1812_4532Metric",
    "hdr1x24": "Connector_PinHeader_2.54mm:PinHeader_1x24_P2.54mm_Vertical",
    # Plain 2x13 pin header rather than a shrouded IDC. The board plugs
    # straight onto the Pi's P1 with the header soldered from the bottom, and
    # the smaller courtyard (6.2 x 34.1 mm against 10.0 x 41.8 mm) is what lets
    # the host block stack in signal order instead of doubling back.
    "idc2x13": "Connector_PinHeader_2.54mm:PinHeader_2x13_P2.54mm_Vertical",
    "mount": "MountingHole:MountingHole_3.2mm_M3",
    "testpoint": "TestPoint:TestPoint_Pad_D1.5mm",
    "mic": "MicArrayV2:MSM261DHP006_LGA-8_3x4mm_TopPort",
    "osc": "MicArrayV2:Oscillator_SMD_YXC_3.2x2.5mm_4Pin",
}

# Ordered pin lists for the custom schematic symbols.
SYMBOL_PINS = {
    "MSM261DHP006": [
        # VDD is declared passive rather than power_in: each microphone is fed
        # through its own 100 ohm isolation resistor, so its supply net has no
        # power_out driver and power_in would raise a spurious ERC error.
        ("1", "VDD", "passive"),
        ("2", "L/R", "input"),
        ("3", "CLK", "input"),
        ("4", "DATA", "tri_state"),
        ("5", "GND", "passive"),
        ("6", "GND", "passive"),
        ("7", "GND", "passive"),
        ("8", "GND", "passive"),
    ],
    "LP5907MFX-3.3": [
        ("1", "IN", "power_in"),
        ("2", "GND", "power_in"),
        ("3", "EN", "input"),
        ("4", "NC", "no_connect"),
        ("5", "OUT", "power_out"),
    ],
    "SN74LVC244A": [
        ("1", "~{1OE}", "input"),
        ("2", "1A1", "input"),
        ("3", "2Y4", "tri_state"),
        ("4", "1A2", "input"),
        ("5", "2Y3", "tri_state"),
        ("6", "1A3", "input"),
        ("7", "2Y2", "tri_state"),
        ("8", "1A4", "input"),
        ("9", "2Y1", "tri_state"),
        ("10", "GND", "power_in"),
        ("11", "2A1", "input"),
        ("12", "1Y4", "tri_state"),
        ("13", "2A2", "input"),
        ("14", "1Y3", "tri_state"),
        ("15", "2A3", "input"),
        ("16", "1Y2", "tri_state"),
        ("17", "2A4", "input"),
        ("18", "1Y1", "tri_state"),
        ("19", "~{2OE}", "input"),
        ("20", "VCC", "power_in"),
    ],
    "USBLC6-4SC6": [
        ("1", "IO1", "passive"),
        ("2", "GND", "passive"),
        ("3", "IO2", "passive"),
        ("4", "IO3", "passive"),
        ("5", "VBUS", "passive"),
        ("6", "IO4", "passive"),
    ],
    "Oscillator_4pin": [
        ("1", "EN", "input"),
        ("2", "GND", "power_in"),
        ("3", "OUT", "output"),
        ("4", "VCC", "power_in"),
    ],
}

# Tang Nano 9K header maps, verified against the official Sipeed pin diagram.
# Position -> (FPGA pin label, carrier net or None when deliberately unused).
TANG_J5 = [
    "38", "37", "36", "39", "25", "26", "27", "28", "29", "30", "33", "34",
    "40", "35", "41", "42", "51", "53", "54", "55", "56", "57", "68", "69",
]
TANG_J6 = [
    "63", "86", "85", "84", "83", "82", "81", "80", "79", "77", "76", "75",
    "74", "73", "72", "71", "70", "5V", "48", "49", "31", "32", "GND", "3V3",
]
# J6 positions 2..9 are FPGA bank 3 at 1.8 V and are left unconnected.
TANG_J6_1V8_POSITIONS = tuple(range(2, 10))


def tang_socket_x(position):
    """Design-frame X of a module header position (position 1 is the USB-C end)."""
    return TANG_CX + TANG_SPAN / 2.0 - (position - 1) * TANG_PITCH


# Raspberry Pi Model B 26-pin P1. Only pins that are identical on revision 1.0
# and revision 2.0 boards are used, so the carrier works with either.
PI_HEADER = {
    2: "PI_5V", 4: "PI_5V",
    6: "GND", 9: "GND", 14: "GND", 20: "GND", 25: "GND",
    16: "PI_SYNC",     # GPIO23
    18: "PI_STATUS",   # GPIO24
    19: "PI_MOSI",     # GPIO10
    21: "PI_MISO",     # GPIO9
    22: "PI_IRQ",      # GPIO25
    23: "PI_SCLK",     # GPIO11
    24: "PI_CS_N",     # GPIO8  / CE0
    26: "PI_RESET_N",  # GPIO7  / CE1
}

# Ordered left to right by where each signal leaves the Pi header, so the
# resistor row, the ESD arrays and the socket pins all run in the same order
# and the eight lines never have to swap places. U3 takes the leftmost four
# signals, U4 the rest.
HOST_SIGNALS = [
    # (pi net, board net, unused, series resistor ref, esd part, esd pin)
    ("PI_RESET_N", "HOST_RESET_N", None, "RH1", "U3", "1"),
    ("PI_SCLK", "SPI_SCLK", None, "RH2", "U3", "3"),
    ("PI_CS_N", "SPI_CS_N", None, "RH3", "U3", "4"),
    ("PI_MISO", "SPI_MISO", None, "RH4", "U3", "6"),
    ("PI_IRQ", "HOST_IRQ", None, "RH5", "U4", "1"),
    ("PI_MOSI", "SPI_MOSI", None, "RH6", "U4", "3"),
    ("PI_STATUS", "HOST_STATUS", None, "RH7", "U4", "4"),
    ("PI_SYNC", "HOST_SYNC", None, "RH8", "U4", "6"),
]

# Carrier net -> Tang module header position, chosen from 3.3 V banks only.
# Data pairs are spread across BOTH socket rows, and ordered along each row to
# follow the azimuth of the channels they serve: the four upper pairs land on
# J2 and the four lower pairs on J3. Putting all eight on adjacent pins of one
# row forces every line from the lower rim to travel the length of the module
# and cross its neighbours; this assignment roughly halves each run and removes
# the convergence entirely.
#
# Several of the pins chosen here are shared with the module's HDMI connector
# (FPGA 68, 70, 71, 72, 75). Driven as ordinary 3.3 V GPIO they carry an
# unterminated stub to that connector, which is immaterial at the 3.072 MHz PDM
# rate and for the DC-ish control lines. The 25 MHz SPI is deliberately kept
# off them, as are both clock inputs.
# Every host signal is on J6, the row facing the Pi header, and every clock is
# on J5. Leaving three SPI lines on J5 made them cross the whole board - out
# from the connector at the bottom, past the header and both socket rows, to
# the top row - for no reason.
#
# The 25 MHz SPI is kept on non-HDMI pins wherever possible. Only SPI_CS_N
# lands on an HDMI-shared pin, and it is static for the duration of a burst.
TANG_NET_MAP = {
    ("J2", 5): "PDM_D0",          # FPGA 25, pair 0 at azimuth 11 deg
    ("J2", 9): "PDM_D1",          # FPGA 29, pair 1 at 56 deg
    ("J2", 14): "PDM_CLK_FPGA",   # FPGA 35, GCLKT_4
    ("J2", 17): "AUDIO_MCLK",     # FPGA 51, GCLKC_3
    ("J2", 19): "PDM_D2",         # FPGA 54, pair 2 at 101 deg
    ("J2", 23): "PDM_D3",         # FPGA 68, pair 3 at 146 deg
    ("J3", 1): "PDM_D7",          # FPGA 63, pair 7 at 326 deg
    ("J3", 10): "PDM_D6",         # FPGA 77, pair 6 at 281 deg
    # Host pins are in the same left-to-right order as the resistor row that
    # feeds them. SPI_SCLK and SPI_MISO are placed on the two non-HDMI pins,
    # being the pair whose edges the 25 MHz link actually depends on.
    ("J3", 11): "HOST_SYNC",      # FPGA 76, not HDMI-shared
    ("J3", 13): "HOST_STATUS",    # FPGA 74
    ("J3", 14): "SPI_MOSI",       # FPGA 73
    ("J3", 15): "HOST_IRQ",       # FPGA 72
    ("J3", 16): "SPI_CS_N",       # FPGA 71
    ("J3", 17): "HOST_RESET_N",   # FPGA 70
    ("J3", 18): "+5V",
    ("J3", 19): "SPI_SCLK",       # FPGA 48, not HDMI-shared
    ("J3", 20): "SPI_MISO",       # FPGA 49, not HDMI-shared
    ("J3", 21): "PDM_D5",         # FPGA 31, pair 5 at 236 deg
    ("J3", 22): "PDM_D4",         # FPGA 32, pair 4 at 191 deg
    ("J3", 23): "GND",
    ("J3", 24): "TANG_3V3",
}

# Test points live on two arcs in the upper half of the board, which is the
# only large area clear of the Tang sockets, the Pi header and the power block.
# (net, short silkscreen label). The label is kept short so neighbouring
# legends on the same arc do not collide; the full net name stays on the
# fabrication layer and in the schematic.
_TEST_POINT_NETS_INNER = [
    ("+5V", "5V"), ("+3V3A", "3V3A"), ("+3V3_CLK", "3V3C"),
    ("TANG_3V3", "TANG"), ("GND", "GND"), ("AUDIO_MCLK", "MCLK"),
    ("PDM_CLK_IN", "PCLK"), ("GND", "GND"), ("SPI_SCLK", "SCK"),
    ("SPI_MOSI", "MOSI"), ("SPI_MISO", "MISO"), ("SPI_CS_N", "CS"),
]
_TEST_POINT_NETS_OUTER = [
    ("PDM_D0", "D0"), ("PDM_D1", "D1"), ("PDM_D2", "D2"), ("PDM_D3", "D3"),
    ("PDM_D4", "D4"), ("PDM_D5", "D5"), ("PDM_D6", "D6"), ("PDM_D7", "D7"),
    ("HOST_IRQ", "IRQ"), ("HOST_SYNC", "SYNC"), ("HOST_RESET_N", "RST"),
    ("HOST_STATUS", "STAT"),
]


# Test points sit in groups on the sector bisectors rather than spread evenly
# round the upper half. The eight PDM clock branches fan out from the centre on
# the pair bisectors at 11.25 + 45n degrees, and an evenly spaced ring put a
# 1.5 mm probe pad squarely in three of those corridors. Clustering the pads at
# 33.75 + 45n leaves a clear 15-degree lane either side of every branch.
TEST_POINT_SECTORS = (33.75, 78.75, 123.75, 168.75)
TEST_POINT_SPREAD = 7.5


def _test_point_table():
    entries = []
    index = 1
    for radius, nets in ((26.0, _TEST_POINT_NETS_INNER),
                         (32.0, _TEST_POINT_NETS_OUTER)):
        for position, (net, label) in enumerate(nets):
            sector = TEST_POINT_SECTORS[position // 3]
            angle = sector + (position % 3 - 1) * TEST_POINT_SPREAD
            entries.append((f"TP{index}", net, radius, angle, label))
            index += 1
    return entries


# Which nets are worth probing, and where the pads would go if they were
# populated. They are not: the 1.5 mm pads sat on the R = 26 and 32 mm rings,
# which is the annulus the clock branches fan out through, and TP1 landed close
# enough to a module socket pin to short it. They were taken off the board
# after routing, and this keeps the schematic in step so the BOM and any
# "update PCB from schematic" match what was actually built.
#
# To reinstate them they need somewhere to live outside the fan-out annulus -
# the four sector gaps at 33.75 + 90n degrees beyond R = 50 mm are clear, or
# the underside of the board.
TEST_POINT_TABLE = _test_point_table()


def _placed_test_points():
    """Probe pads as actually placed against the routed board.

    tools/place_testpoints.py works out where each one can go - on top of a
    piece of its own net, so it needs no track of its own - and writes the
    result to generated/test_points.py. Reading it back here keeps the
    schematic and the BOM in step with the board. Nets it could not place are
    simply absent; there is nowhere on the top layer to probe them.
    """
    import ast
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "generated", "test_points.py")
    if not os.path.exists(path):
        return ()
    # Parsed rather than imported: importing would leave a __pycache__ in the
    # generated directory, and the file is data, not code.
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == "PLACED_TEST_POINTS":
            return tuple(tuple(entry) for entry in ast.literal_eval(node.value))
    return ()


TEST_POINTS = _placed_test_points()
