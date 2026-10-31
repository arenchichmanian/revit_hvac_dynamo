# 01 - Isolated 3D view per FCU

Creates one 3D view per matching FCU containing only its duct system
(ducts, fittings, accessories, air terminals, insulation and pass-through
equipment such as an FCU plenum). The view is section-boxed, set to 1:50,
fine detail, pipe categories off, and the camera is locked.

## How to set it up (pick one)

**Option A - easiest: open the graph.**
Open `fcu_isolated_3d_views.dyn` in Dynamo (File > Open). All blocks are already
wired. If the Python node is empty or outdated, paste the contents of
`fcu_isolated_3d_views.py` into it (see below).

**Option B - build it yourself.**
The `.py` file is only the code for ONE block, the Python Script node. The other
blocks are plain input nodes that feed it.

1. Add a **Python Script** node (Dynamo: Core > Script > Python Script) and set
   its engine to **CPython3**. Click `+` until it has 8 inputs (IN[0] to IN[7]).
2. Double-click the node, delete the default code, paste the contents of
   `fcu_isolated_3d_views.py`.
3. Add the 8 input blocks and connect each to the matching port:

   | Block to add | Value | Connect to |
   |--------------|-------|------------|
   | String | `FCU-L0-095` | IN[0] |
   | String | `3D` | IN[1] |
   | Integer Slider (0-1000, step 50) | 300 | IN[2] |
   | Integer Slider (10-500, step 10) | 100 | IN[3] |
   | Boolean | `False` | IN[4] |
   | String | `Mark` | IN[5] |
   | String | `Mark` | IN[6] |
   | String | options text below | IN[7] |

4. Add a **Watch** node and connect the Python node's `OUT` to it.
5. Set the run mode (bottom left) to **Manual**.

## What each input means

| Input | Node | Default | Meaning |
|-------|------|---------|---------|
| IN[0] | String | `FCU-L0-095` | Filter text (contains match; `-` and `.` treated the same, case ignored) |
| IN[1] | String | `3D` | View name prefix |
| IN[2] | Integer Slider (0-1000, step 50) | 300 | Section box margin in mm |
| IN[3] | Integer Slider (10-500, step 10) | 100 | Max hops when walking the network |
| IN[4] | Boolean | `False` | Run. False = preview, nothing changed |
| IN[5] | String | `Mark` | Parameter the filter searches |
| IN[6] | String | `Mark` | Parameter used to name the view |
| IN[7] | String | see below | Options |

All 8 ports must be wired. Run mode: Manual.

## Options (IN[7])

`style=edges; transparency=0; scale=50; direction=SE; existing=skip; through=fcu-pl, plenum`

- `style`: display style of the view
- `transparency`: 0-90, applied to duct categories
- `scale`: view scale denominator
- `direction`: iso camera direction
- `existing`: `skip` or `replace` an existing view of the same name
- `through`: family-name words of equipment the walk passes through

An invalid option stops the run before anything is changed.

## Running

1. Run with the Boolean on `False` to preview.
2. Set it to `True`, run once, then set it back to `False`.
3. Ctrl+Z in Revit undoes the whole run.
4. A report is saved to `%USERPROFILE%\fcu_script01_report.txt`.
5. In Manual mode, Dynamo only re-runs when an input changes: flip the Boolean off and on.
6. If `existing=replace` cannot delete the old view, it renames it `..._OLD`.
