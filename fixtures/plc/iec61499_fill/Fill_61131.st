(* Scan-style source with TON, R_TRIG and CTU, for the IEC 61499 translation fixture. *)
PROGRAM Fill
  VAR
    Start_PB AT %IX0.0 : BOOL;
    Level_High AT %IX0.1 : BOOL;
    Fill_Valve AT %QX0.0 : BOOL;
    Done_Lamp AT %QX0.1 : BOOL;
  END_VAR
  VAR
    Fill_Timer : TON;
    Start_Edge : R_TRIG;
    Batches : CTU;
    Filling : BOOL;
  END_VAR
  Start_Edge(CLK := Start_PB);
  IF Start_Edge.Q THEN Filling := TRUE; END_IF;
  Fill_Timer(IN := Filling, PT := T#3s);
  IF Level_High OR Fill_Timer.Q THEN Filling := FALSE; END_IF;
  Fill_Valve := Filling;
  Batches(CU := Fill_Timer.Q, RESET := FALSE, PV := 5);
  Done_Lamp := Batches.Q;
END_PROGRAM
