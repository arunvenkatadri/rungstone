# Two-tank OpenPLC fixture

Hand-written OpenPLC Runtime translation of `../two_tank_slc.txt`, used to exercise
`openplc_check.py`, `st_to_plcopen.py` and `simulate.py compare`.

- `two_tank_openplc.st` passes `openplc_check.py` with no errors or warnings.
- `mapping.csv` assumes a 0..65535 analog input board (`raw:3277..16384->0..65535` on I:3.0).
- Against `../sim/scenarios.json`, S1-S3 **match**. S4 (transmitter wire break, raw 0) **differs** on
  purpose: the SLC scales a broken 4-20 mA loop to -250 (below zero, so the fault is visible), while
  the OpenPLC input reads 0 and the level shows 0 %, the same as an empty tank. The valves still close
  in both, but wire-break detection is lost. That is a review item for the engineer, not a bug in
  the simulator.
- Results are simulated, not hardware-tested.
