# 03 - Tag the elements in each FCU view

Places tags in the locked 3D views made by script 01. Each element gets its
normal tag and its Element Number tag. Run script 02 first: an element with no
number gets no number tag.

| Element | Normal tag | Element Number tag |
|---------|-----------|--------------------|
| Duct, flex duct, accessory, air terminal | yes | yes |
| Duct fitting | no | yes |
| FCU | yes | no |
| Plenum | no | yes |
| Rigid duct at or below the minimum length | no | no |

The tag family and type names are in `TAG_RULES` at the top of the `.py` file.
Change them there for another project. A tag that is not loaded is reported
as `MISSING TAG`, with the loaded tags of that category listed.

## How to set it up (pick one)

**Option A - open the graph.** Open `fcu_tagging.dyn` in Dynamo (File > Open).

**Option B - build it yourself.** The `.py` file is the code for ONE block, the
Python Script node (engine CPython3, 6 inputs). Add the input blocks below,
connect each to its port, add a Watch node on `OUT`, set run mode to Manual.

| Input | Block | Default | Meaning |
|-------|-------|---------|---------|
| IN[0] | String | `3D_FCU-LI-L0-095` | View name filter (contains match) |
| IN[1] | String | `DLB_ELEMENT Number` | Parameter holding the numbers |
| IN[2] | Integer Slider (0-30, step 1) | 8 | Tag offset from the element, in paper mm |
| IN[3] | Integer Slider (0-500, step 10) | 100 | Minimum duct length in mm (same value as script 02) |
| IN[4] | Boolean | `False` | Run. False = preview, nothing created |
| IN[5] | String | see below | Options |

All 6 ports must be wired.

## Options (IN[5])

`tags=both; leader=free; replace=no; through=fcu-pl, plenum`

- `tags`: `both`, `normal` or `number`
- `leader`: `free` (free end on the element), `attached` or `none`
- `replace`: `yes` deletes the existing tags of these types in the view first
- `through`: family-name words that mark a plenum

## Running

1. Run with the Boolean on `False` to see how many tags would be created.
2. Set it to `True`, run once, then set it back to `False`. Ctrl+Z undoes the run.
3. Running again does not duplicate tags: existing ones are skipped.
4. The report is also saved to `%USERPROFILE%\fcu_script03_report.txt`.
