# revit-hvac-dynamo

Dynamo (Python) scripts for Revit HVAC shop drawings and duct pressure-loss work.
Tested on Revit 2022/2023 with the Dynamo CPython3 engine.

## Planned workflow

| # | Script | Status |
|---|--------|--------|
| 01 | Isolated, locked 3D view per FCU | Done |
| 02 | Number elements along the longest route | In progress |
| 03 | Tag elements with their number tags | In progress |
| 04 | Classify fittings to ASHRAE codes | Planned |
| 05 | Export the route to Excel | Planned |

Each script has its own folder with a README (inputs, wiring, options).

## How to use a script

1. Open Dynamo from Revit, create a Python Script node (CPython3).
2. Paste the contents of the `.py` file into it, or open the provided `.dyn`.
3. Wire the inputs as described in the script's README.

## Notes

Scripts change the Revit model. Try them on a copy of the model first.
Each script is a single file, so shared code (the network walk) is copied into each one.
