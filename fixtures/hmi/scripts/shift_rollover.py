# Ignition gateway timer script (original sample for Rungstone tests)
# At shift change: record the batch count, then pulse the PLC reset bit.
count = system.tag.readBlocking(["[default]Fill/BatchCount"])[0].value
system.db.runPrepUpdate("INSERT INTO shift_counts (shift, batches) VALUES (?, ?)",
                        [system.tag.readBlocking(["[default]Fill/Shift"])[0].value, count])
system.tag.writeBlocking(["[default]Fill/ResetCount"], [True])
