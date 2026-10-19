# Two-tank simulation fixture

Hand-written S7-1500 SCL translation of `../two_tank_slc.txt`, used to exercise `simulate.py compare`.

- `mapping.csv` passes the analog raw value through unchanged. The SLC 1746-NI4 4-20 mA range is 3277..16384, and the S7 range is 0..27648, so the scaled level (N7:0 -> Level_Pct) **differs**. This is the scaling error that the review queue must catch.
- `mapping_scaled.csv` adds `raw:3277..16384->0..27648` to the I:3.0 row. The simulator converts the input before writing it to the target, so the scenarios **match**. Output raw ranges (NO4I vs S7 AQ) are not converted and stay open review items.
- Results are simulated, not hardware-tested.
