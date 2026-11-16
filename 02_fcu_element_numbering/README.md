# 02 - Number the elements of each FCU

Writes a number into `DLB_ELEMENT Number` on every element of an FCU's duct
system, for pressure-loss calculation. Numbers start at 1 at the return air
terminal, run to the FCU, then continue on the supply side. The longest route
reaches the highest number. The FCU itself is not numbered; a plenum is.

- Rigid ducts at or below the minimum length (default 100 mm) are not counted
  and are written as 0.
- Branch elements get the number for their distance from the FCU, so two
  branches can share numbers (`branches=zero` writes 0 on branches instead).

## How to set it up (pick one)

**Option A - open the graph.** Open `fcu_element_numbering.dyn` in Dynamo
(File > Open). All blocks are already wired.

**Option B - build it yourself.** The `.py` file is the code for ONE block, the
Python Script node (engine CPython3, 7 inputs). Add the input blocks below,
connect each to its port, add a Watch node on `OUT`, set run mode to Manual.

| Input | Block | Default | Meaning |
|-------|-------|---------|---------|
| IN[0] | String | `FCU-LI-L0-095` | Filter text (contains match; `-` and `.` treated the same) |
| IN[1] | String | `DLB_ELEMENT Number` | Parameter the numbers are written into |
| IN[2] | Integer Slider (0-500, step 10) | 100 | Minimum duct length in mm (0 = count every duct) |
| IN[3] | Integer Slider (10-500, step 10) | 100 | Max hops when walking the network |
| IN[4] | Boolean | `False` | Run. False = preview, nothing written |
| IN[5] | String | `DLB-Designation` | Parameter the filter searches |
| IN[6] | String | see below | Options |

All 7 ports must be wired.

## Options (IN[6])

`branches=depth; through=fcu-pl, plenum; detail=yes`

- `branches`: `depth` (number branches by distance) or `zero` (branches get 0)
- `through`: family-name words of equipment that is part of the route (plenum)
- `detail`: `yes` lists every element with its number in the report

## Running

1. Run with the Boolean on `False`. The report shows the planned route and numbers.
2. Set it to `True`, run once, then set it back to `False`. Ctrl+Z undoes the run.
3. The report is also saved to `%USERPROFILE%\fcu_script02_report.txt`.

Report legend: AT air terminal, D duct, FX flex duct, F fitting, ACC accessory, PL plenum.
