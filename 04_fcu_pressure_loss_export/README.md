# 04 - Export the FCU route to a pressure-loss Excel sheet

For each matched FCU, walks the longest route (return air terminal -> FCU ->
supply air terminal) and writes an Excel workbook with one sheet per FCU: one
row per duct/fitting/accessory/terminal on the route, with size, length, flow
and the same velocity/Reynolds/friction/pressure-drop formulas as a hand
calculation. A "Noise inputs" sheet is also written, with the duct inputs
(smallest duct dimension, length, elbow widths/quantities, critical outlet
area and flow, room volume) that a separate noise calculation needs, for the
run from the FCU to the nearest air terminal on each side.

The script only reads the model. Nothing in Revit is changed.

## What's filled in vs. left for you

The sheet is generated with the geometry, flow and layout already in it.
Three kinds of cell are left **yellow** for manual entry:

- the loss coefficient of each fitting (look up the suggested ASHRAE code
  and the sizes in the notes column, in the ASHRAE Duct Fitting Database)
- the fixed pressure loss of grilles, diffusers, dampers and attenuators
  (from the manufacturer's data)
- the FCU's design flow, if the air terminals carry no flow in Revit

`ASHRAE_CODES`, `ACCESSORY_NAMES` and `FIXED_LOSS` near the top of the `.py`
file are the lookup tables behind the suggested codes and names - edit them
for your own project's fittings.

## How to set it up (pick one)

**Option A - open the graph.** Open `fcu_pressure_loss_export.dyn` in Dynamo
(File > Open).

**Option B - build it yourself.** The `.py` file is the code for ONE block,
the Python Script node (engine CPython3, 7 inputs). Add the input blocks
below, connect each to its port, add a Watch node on `OUT`, set run mode to
Manual.

| Input | Block | Default | Meaning |
|-------|-------|---------|---------|
| IN[0] | String | `FCU-L0-095` | Filter text (contains match) |
| IN[1] | String | *(empty)* | Output folder. Empty = `Documents\FCU pressure loss` |
| IN[2] | Integer Slider (0-500, step 10) | 100 | Minimum duct length in mm (same value as script 02) |
| IN[3] | Integer Slider (10-500, step 10) | 100 | Max hops (safety limit on the duct walk) |
| IN[4] | Boolean | `False` | Run. False = preview only, no file written |
| IN[5] | String | `Mark` | Parameter to filter on |
| IN[6] | String | see below | Options |

All 7 ports must be wired.

## Options (IN[6])

`through=fcu-pl, plenum; safety=10; fcuflow=; numparam=DLB_ELEMENT Number`

- `through`: family-name words that mark a plenum (walked through, not blocking)
- `safety`: safety margin added to the total pressure drop, in percent
- `fcuflow`: FCU parameter holding the design airflow, if you have one;
  empty means the flow is typed into Excel by hand
- `numparam`: the parameter script 02 writes numbers to - only checked to
  warn you if script 02 hasn't been run yet

## Running

1. Run with the Boolean on `False` first: the preview lists which FCUs were
   matched and how many rows each sheet will have, without writing anything.
2. Set it to `True` and run once to write the `.xlsx` file. The file name
   includes the filter text and a timestamp, so running again never
   overwrites an earlier export.
3. The report is also saved to `%USERPROFILE%\fcu_script04_report.txt`.

## Known manual steps

- Loss coefficients and fixed losses (grilles, diffusers, dampers,
  attenuators) are not looked up automatically yet - every yellow cell needs
  a number from the ASHRAE database or the manufacturer's datasheet.
- The "Noise inputs" sheet gives duct geometry only. Source sound power,
  distance to the listener, directivity, reverberation time and the NC
  criterion are not modeled and still need to come from your own data.
