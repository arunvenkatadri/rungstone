"""Generate fixtures/hmi/alarms/alarm_history.csv (seeded, reproducible).

Planted patterns (for evals):
  FILL_TANK_HI_LVL   chattering: bursts of activate/clear every 6-15 s
  PUMP2_SEAL_LEAK    standing: activates on day 1, never clears (> 24 h)
  COMP1_TRIP -> AIR_PRESS_LO   consequential pair: B follows A within 1 s every time
  DOOR3_OPEN_SW      fleeting: active for 0.2-0.6 s
  flood: 2024-05-14 14:00-14:08, ~30 alarms from a compressor trip cascade
"""
import csv
import datetime as dt
import os
import random

random.seed(18)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "alarms", "alarm_history.csv")
T0 = dt.datetime(2024, 5, 13, 6, 0, 0)
END = T0 + dt.timedelta(days=3)
ALARMS = {
    "FILL_TANK_HI_LVL": ("Fill/TankFull", "High", "Fill tank high level", "FILL"),
    "CONV_JAM": ("Conveyor/JamFault", "High", "Conveyor jam at transfer", "PACK"),
    "PUMP2_SEAL_LEAK": ("Pumps/P2/SealLeak", "Medium", "Pump 2 seal leak detected", "UTIL"),
    "COMP1_TRIP": ("Air/Comp1/Trip", "High", "Compressor 1 tripped", "UTIL"),
    "AIR_PRESS_LO": ("Air/Header/PressLo", "High", "Instrument air header pressure low", "UTIL"),
    "DOOR3_OPEN_SW": ("Pack/Door3/Open", "Low", "Door 3 open", "PACK"),
    "MIXER_OVERLOAD": ("Mix/Agitator/OL", "High", "Agitator motor overload", "MIX"),
    "VALVE_XV301_FAIL": ("Fill/XV301/Fail", "Medium", "XV-301 failed to open", "FILL"),
    "VALVE_XV302_FAIL": ("Fill/XV302/Fail", "Medium", "XV-302 failed to open", "FILL"),
    "BOILER_TEMP_HI": ("Util/Boiler/TempHi", "Urgent", "Boiler temperature high", "UTIL"),
    "CIP_FLOW_LO": ("CIP/FlowLo", "Low", "CIP return flow low", "CIP"),
    "LABELER_STARVED": ("Pack/Labeler/Starved", "Low", "Labeler starved of labels", "PACK"),
}
rows = []


def ev(t, name, state):
    tag, pri, msg, area = ALARMS[name]
    rows.append((t, name, tag, state, pri, msg, area))


def episode(t, name, dur_s, ack_after=None):
    ev(t, name, "ACTIVE")
    if ack_after is not None and ack_after < dur_s:
        ev(t + dt.timedelta(seconds=ack_after), name, "ACK")
    ev(t + dt.timedelta(seconds=dur_s), name, "CLEAR")


# chattering bursts: 10 bursts over 3 days, 20-40 cycles each
for b in range(10):
    t = T0 + dt.timedelta(hours=random.uniform(1, 70))
    for _ in range(random.randint(20, 40)):
        on = random.uniform(1.5, 5)
        episode(t, "FILL_TANK_HI_LVL", on)
        t += dt.timedelta(seconds=on + random.uniform(4, 10))
# standing alarm
ev(T0 + dt.timedelta(hours=3, minutes=12), "PUMP2_SEAL_LEAK", "ACTIVE")
ev(T0 + dt.timedelta(hours=3, minutes=20), "PUMP2_SEAL_LEAK", "ACK")
# consequential pair, 7 occurrences (one inside the flood)
for h in (5, 17, 26, 33, 41, 55):
    t = T0 + dt.timedelta(hours=h, minutes=random.randint(0, 59), seconds=random.randint(0, 59))
    episode(t, "COMP1_TRIP", random.uniform(120, 600), ack_after=30)
    episode(t + dt.timedelta(seconds=random.uniform(0.2, 0.9)), "AIR_PRESS_LO", random.uniform(60, 300), ack_after=35)
# flood 2024-05-14 14:00
f0 = dt.datetime(2024, 5, 14, 14, 0, 0)
episode(f0, "COMP1_TRIP", 900, ack_after=60)
episode(f0 + dt.timedelta(seconds=0.6), "AIR_PRESS_LO", 800, ack_after=62)
cascade = ["VALVE_XV301_FAIL", "VALVE_XV302_FAIL", "MIXER_OVERLOAD", "CONV_JAM", "CIP_FLOW_LO", "LABELER_STARVED",
           "BOILER_TEMP_HI"]
t = f0 + dt.timedelta(seconds=5)
for i in range(28):
    episode(t, cascade[i % len(cascade)], random.uniform(20, 240), ack_after=random.uniform(10, 19))
    t += dt.timedelta(seconds=random.uniform(4, 16))
# fleeting door switch
for _ in range(60):
    episode(T0 + dt.timedelta(hours=random.uniform(0, 71)), "DOOR3_OPEN_SW", random.uniform(0.2, 0.6))
# background noise
for _ in range(110):
    name = random.choice(["CONV_JAM", "MIXER_OVERLOAD", "CIP_FLOW_LO", "LABELER_STARVED", "VALVE_XV301_FAIL"])
    episode(T0 + dt.timedelta(hours=random.uniform(0, 71.5)), name, random.uniform(30, 1800), ack_after=random.uniform(5, 120))

rows.sort(key=lambda r: r[0])
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["EventTime", "AlarmName", "TagPath", "State", "Priority", "Message", "Area"])
    for r in rows:
        if r[0] < END:
            w.writerow([r[0].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]] + list(r[1:]))
print("wrote", OUT, len(rows), "rows")
