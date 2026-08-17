# Written by tools/place_testpoints.py. Board positions of the
# probe pads, chosen against the routed copper, so that
# tools/netlist.py can reproduce them.
PLACED_TEST_POINTS = (
    ("TP1", "+5V", "MicArrayV2:TestPoint_Pad_D1.0mm_NoSilk", -3.140, -14.602),
    ("TP2", "+3V3A", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", 24.145, 19.169),
    ("TP3", "+3V3_CLK", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", 16.877, -14.723),
    ("TP4", "GND", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", 4.000, 24.000),
    ("TP5", "GND", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", 7.000, 22.000),
    ("TP6", "PDM_D0", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", 39.130, 16.208),
    ("TP7", "PDM_D1", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", 29.949, 29.949),
    ("TP8", "PDM_D2", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", 0.000, 42.354),
    ("TP9", "PDM_D3", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", -29.949, 29.949),
    ("TP10", "PDM_D4", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", -39.130, -16.208),
    ("TP11", "PDM_D5", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", -29.949, -29.949),
    ("TP12", "PDM_D6", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", 16.208, -39.130),
    ("TP13", "PDM_D7", "MicArrayV2:TestPoint_Pad_D1.5mm_NoSilk", 29.949, -29.949),
    ("TP14", "HOST_STATUS", "MicArrayV2:TestPoint_Pad_D1.0mm_NoSilk", 2.600, -19.900),
)
