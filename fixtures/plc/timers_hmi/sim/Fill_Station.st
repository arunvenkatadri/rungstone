PROGRAM Fill_Station
VAR
    Start_PB AT %IX0.0 : BOOL;     (* was Start_PB -> Local:1:I.Data.0 *)
    Stop_PB AT %IX0.1 : BOOL;      (* was Stop_PB, normally closed *)
    Tank_Full AT %IX0.2 : BOOL;    (* was Tank_Full, LSH-301 *)
    Fill_Valve AT %QX0.0 : BOOL;   (* was Fill_Valve, XV-301 *)
    HMI_Reset_Count : BOOL;
    Fill_Active : BOOL;            (* NOT declared RETAIN: deliberate divergence from Logix retentive tags *)
    Cycle_Done : BOOL;
    Start_Trig : R_TRIG;           (* replaces ONS(Start_OS) *)
    Fill_Timer : TON;              (* was TIMER; ACC (DINT ms) -> ET (TIME) *)
    Batch_Count : CTU;             (* was COUNTER; ACC -> CV *)
    Fill_Time_PT : TIME := T#30S;  (* was Fill_Timer.PRE = 30000 *)
END_VAR

(* rung 0 *)
Start_Trig(CLK := Start_PB);
IF Start_Trig.Q AND NOT Fill_Active THEN
    Fill_Active := TRUE;
END_IF;
(* rung 1 *)
Fill_Timer(IN := Fill_Active, PT := Fill_Time_PT);
(* rung 2 *)
Fill_Valve := Fill_Active AND NOT Fill_Timer.Q AND NOT Tank_Full;
(* rung 3 *)
Cycle_Done := FALSE;
IF Fill_Active AND (Fill_Timer.Q OR Tank_Full) THEN
    Fill_Active := FALSE;
    Cycle_Done := TRUE;
END_IF;
(* rung 4 *)
IF NOT Stop_PB THEN
    Fill_Active := FALSE;
END_IF;
(* rungs 5-6 *)
Batch_Count(CU := Cycle_Done, R := HMI_Reset_Count, PV := 100);
END_PROGRAM
