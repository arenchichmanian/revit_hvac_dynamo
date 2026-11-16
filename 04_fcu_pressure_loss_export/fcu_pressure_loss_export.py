# fcu_pressure_loss_export.py  (v1.1)
# Dynamo for Revit - Python Script node, CPython3 engine
# Author: Aren  |  License: MIT (see LICENSE in the repo root)
# Writes an Excel pressure-loss sheet for each matched FCU: one row per element
# of the longest route (return air terminal -> FCU -> supply air terminal),
# with flow, size, length and a suggested ASHRAE fitting code, plus the
# formulas for velocity, Reynolds number, friction and pressure loss.
# It reads the model only: nothing in Revit is changed.
#
# The workbook also gets a "Noise inputs" sheet: for the return and the supply
# side of each FCU, the duct inputs of the noise calculation (smallest duct
# dimension, duct length, elbows, critical outlet area and flow), taken along
# the route from the FCU to the nearest air terminal.
#
# Version history
#   v1.1  "Noise inputs" sheet added
#   v1.0  first version
#
# Cells left YELLOW in the workbook must be filled in by hand:
#   - the loss coefficient of each fitting (from the ASHRAE Duct Fitting
#     Database, using the suggested code and the sizes in the notes column)
#   - the fixed pressure loss of grilles, diffusers, dampers and attenuators
#     (manufacturer data)
#   - the FCU flow, when the air terminals carry no flow in Revit
#
# Flow in each element = sum of the flows of the air terminals beyond it.
# If the air terminals have no flow in Revit, the FCU flow is shared equally
# between the terminals of that side.
#
# Inputs:
#   IN[0] filter text ("contains" match)               e.g. FCU-LI-L0-095
#   IN[1] output folder (empty = Documents\FCU pressure loss)
#   IN[2] minimum duct length in mm (0 = rule off)     e.g. 100
#         use the SAME value as in the numbering script
#   IN[3] max hops (safety limit for the duct walk)    e.g. 100
#   IN[4] run (False = preview only, True = write the Excel file)
#   IN[5] parameter to filter on                       default DLB-Designation
#   IN[6] options: "key=value; key=value"
#           through=word, word      (default fcu-pl, plenum)
#           safety=number           (default 10) safety margin in percent
#           fcuflow=parameter name  (default empty) FCU parameter holding the
#                                   design airflow; empty = type it in Excel
#           numparam=parameter name (default DLB_ELEMENT Number) only used to
#                                   check that the numbering script was run
# Special filter parameter values: @type, @family, @familytype

import os
import math
import time
import zipfile
import traceback
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter,
    Element, FamilyInstance, MEPCurve, Domain, StorageType, LocationCurve
)
from RevitServices.Persistence import DocumentManager

doc = DocumentManager.Instance.CurrentDBDocument

VERSION = "v1.1"

# ==================================================================
# EDIT HERE: names and suggested ASHRAE codes
# ==================================================================
# Suggested ASHRAE fitting code by (kind of fitting, "rect"/"round" or side).
# These are starting points taken from past calculations: always check them.
ASHRAE_CODES = {
    ("elbow", "rect"): "CR3-5",
    ("elbow", "round"): "CD3-1",
    ("transition", "return"): "ER4-2",
    ("transition", "supply"): "SR4-1",
    ("tee", "supply"): "SR5-13",
    ("tap", "supply"): "SD1-1",
    ("plenum", "supply"): "SD1-1",
}

# Duct accessory names: (word in the family or type name, name on the sheet).
# The first match wins. No match = the family name is used.
ACCESSORY_NAMES = [
    ("mfsd", "MFSD"),
    ("mfd", "MFD"),
    ("fsd", "FSD"),
    ("vcd", "VCD"),
    ("attenuator", "Attenuator"),
    ("dac-at", "Attenuator"),
    ("silencer", "Attenuator"),
]

# Fixed pressure loss in Pa by sheet name (manufacturer data). Empty = the
# cell stays yellow. Example: {"Attenuator": 15, "MFD": 4}
FIXED_LOSS = {}


# ---- COMMON BLOCK v2 (begin) -------------------------------------------
# Identical in scripts 01, 02, 04 and 05. Change it in common/network_walk.py
# first, then paste it into every script. It needs only the imports above
# (BuiltInCategory, BuiltInParameter, MEPCurve, FamilyInstance, Domain,
# LocationCurve) and no global variables.
# v2: equipment whose family name contains one of `through_names` (for example
# a plenum) is walked through as part of the network.

# Categories that count as part of an equipment's duct network
ALLOWED_CATS = {
    int(BuiltInCategory.OST_DuctCurves),
    int(BuiltInCategory.OST_FlexDuctCurves),
    int(BuiltInCategory.OST_DuctFitting),
    int(BuiltInCategory.OST_DuctAccessory),
    int(BuiltInCategory.OST_DuctTerminal),
}

DUCT_CAT = int(BuiltInCategory.OST_DuctCurves)
EQUIPMENT_CAT = int(BuiltInCategory.OST_MechanicalEquipment)


def eid_int(eid):
    """ElementId -> int. Works on old and new Revit versions."""
    try:
        return eid.Value            # Revit 2024+
    except AttributeError:
        return eid.IntegerValue     # older versions


def family_name_lower(el):
    """Lowercase family name of a placed family, or '' if unavailable."""
    try:
        return str(el.Symbol.FamilyName).lower()
    except Exception:
        return ""


def is_pass_through(el, through_names):
    """True for mechanical equipment whose family name contains a through word."""
    if not through_names:
        return False
    cat = el.Category
    if cat is None or eid_int(cat.Id) != EQUIPMENT_CAT:
        return False
    fam = family_name_lower(el)
    for word in through_names:
        if word in fam:
            return True
    return False


def is_allowed(el, through_names=()):
    cat = el.Category
    if cat is None:
        return False
    if eid_int(cat.Id) in ALLOWED_CATS:
        return True
    return is_pass_through(el, through_names)


def duct_length_ft(el):
    """Length of a duct in feet, or None if it cannot be read."""
    try:
        p = el.get_Parameter(BuiltInParameter.CURVE_ELEM_LENGTH)
        if p is not None and p.HasValue:
            return p.AsDouble()
    except Exception:
        pass
    try:
        loc = el.Location
        if isinstance(loc, LocationCurve):
            return loc.Curve.Length
    except Exception:
        pass
    return None


def is_small_duct(el, min_len_ft):
    """True for a rigid duct at or below the minimum length (no pressure drop)."""
    if min_len_ft <= 0:
        return False
    cat = el.Category
    if cat is None or eid_int(cat.Id) != DUCT_CAT:
        return False
    length = duct_length_ft(el)
    return length is not None and length <= min_len_ft + 1e-6


def get_connectors(el):
    """Return all connectors of a duct curve or a family instance."""
    try:
        if isinstance(el, MEPCurve):
            return list(el.ConnectorManager.Connectors)
        if isinstance(el, FamilyInstance):
            mep = el.MEPModel
            if mep is not None and mep.ConnectorManager is not None:
                return list(mep.ConnectorManager.Connectors)
    except Exception:
        pass
    return []


def neighbors_of_connector(c, self_id, through_names):
    """Allowed elements joined to connector c (never the owner itself)."""
    out = []
    try:
        if not c.IsConnected:
            return out
        for ref in c.AllRefs:
            o = ref.Owner
            if o is None:
                continue
            if eid_int(o.Id) == self_id:
                continue
            if not isinstance(o, (MEPCurve, FamilyInstance)):
                continue            # skips the logical system element
            if not is_allowed(o, through_names):
                continue            # skips other equipment
            out.append(o)
    except Exception:
        pass
    return out


def element_neighbors(el, through_names):
    """Allowed elements reachable from el through its duct (HVAC) connectors."""
    sid = eid_int(el.Id)
    out = []
    for c in get_connectors(el):
        try:
            if c.Domain != Domain.DomainHvac:
                continue
        except Exception:
            continue
        out.extend(neighbors_of_connector(c, sid, through_names))
    return out


def walk_side(roots, blocked, max_hops, min_len_ft, through_names):
    """
    Breadth-first walk outward from the elements touching an equipment connector.
    roots:         elements touching the connector(s) of the side being walked.
    blocked:       element ids that belong to another side (never entered).
    max_hops:      safety limit on how far the walk goes.
    min_len_ft:    short-duct threshold in feet (0 = rule off).
    through_names: lowercase words; equipment whose family name contains one
                   of them is walked through (for example a plenum).
    Returns (order, depth, parent, skipped, truncated, overlaps).
      order:   elements in discovery order (short ducts included)
      depth:   id -> distance in COUNTED elements; a short duct has the same
               depth as the element it was reached from
      parent:  id -> id of the element it was reached from (None for roots)
      skipped: ids of short ducts (walked through, never counted)
    """
    depth, parent, order = {}, {}, []
    skipped = set()
    overlaps = 0
    frontier = []
    for r in roots:
        rid = eid_int(r.Id)
        if rid in depth:
            continue
        if rid in blocked:
            overlaps += 1
            continue
        small = is_small_duct(r, min_len_ft)
        if small:
            skipped.add(rid)
        depth[rid] = 0 if small else 1
        parent[rid] = None
        order.append(r)
        frontier.append(r)

    hops = 1
    while frontier and hops < max_hops:
        nxt = []
        for el in frontier:
            eid = eid_int(el.Id)
            for o in element_neighbors(el, through_names):
                oid = eid_int(o.Id)
                if oid in depth:
                    continue
                if oid in blocked:
                    overlaps += 1
                    continue
                small = is_small_duct(o, min_len_ft)
                if small:
                    skipped.add(oid)
                depth[oid] = depth[eid] + (0 if small else 1)
                parent[oid] = eid
                order.append(o)
                nxt.append(o)
        frontier = nxt
        hops += 1
    return order, depth, parent, skipped, len(frontier) > 0, overlaps

# ---- COMMON BLOCK v2 (end) ---------------------------------------------


# ------------------------------------------------------------------
# Inputs
# ------------------------------------------------------------------
DEFAULT_FILTER_PARAM = "DLB-Designation"

DEFAULT_OPTIONS = {
    "through": "fcu-pl, plenum",
    "safety": "10",
    "fcuflow": "",
    "numparam": "DLB_ELEMENT Number",
}

FT_TO_MM = 304.8
CFS_TO_LPS = 28.316846592           # cubic feet per second -> litres per second

SYS_SUPPLY, SYS_RETURN, SYS_EXHAUST = 1, 2, 3
DIR_IN, DIR_OUT = 1, 2
CONNECTOR_TYPE_CURVE = 2            # a connector on the side of a duct (tap)

TERMINAL_CAT = int(BuiltInCategory.OST_DuctTerminal)
FLEX_CAT = int(BuiltInCategory.OST_FlexDuctCurves)
FITTING_CAT = int(BuiltInCategory.OST_DuctFitting)
ACCESSORY_CAT = int(BuiltInCategory.OST_DuctAccessory)


def inp(i, default):
    """Read IN[i] as trimmed text; use default if missing, None or empty."""
    try:
        v = IN[i]
    except Exception:
        return default
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def norm(text):
    """Lowercase and treat '.' like '-' so FCU.LI.L0.016 == FCU-LI-L0-016."""
    return str(text).strip().lower().replace('.', '-')


def parse_options(text):
    """'a=1; b=2' -> ({'a': '1', 'b': '2'} merged over defaults, [unknown parts])."""
    opts = dict(DEFAULT_OPTIONS)
    unknown = []
    for part in text.replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            unknown.append(part)
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k in opts:
            opts[k] = v
        else:
            unknown.append(part)
    return opts, unknown


filter_text  = norm(inp(0, ""))
OUT_FOLDER   = inp(1, "")
min_len_mm   = float(inp(2, "100"))
min_len_ft   = min_len_mm / FT_TO_MM
max_hops     = int(float(inp(3, "100")))
run          = bool(IN[4])
FILTER_PARAM = inp(5, DEFAULT_FILTER_PARAM)

OPTS, UNKNOWN_OPTS = parse_options(inp(6, ""))
THROUGH_NAMES = tuple(t.strip().lower() for t in OPTS["through"].split(",")
                      if t.strip())
FCU_FLOW_PARAM = OPTS["fcuflow"]
NUM_PARAM = OPTS["numparam"]
try:
    SAFETY = float(OPTS["safety"])
except Exception:
    SAFETY = None


def option_errors():
    """Problems with the inputs. Empty list means all good."""
    errs = []
    for u in UNKNOWN_OPTS:
        errs.append("Unknown option '{}'. Allowed keys: through, safety, fcuflow, "
                    "numparam.".format(u))
    if SAFETY is None or SAFETY < 0:
        errs.append("safety must be a number of 0 or more (got '{}')."
                    .format(OPTS["safety"]))
    if min_len_mm < 0:
        errs.append("minimum duct length cannot be negative.")
    if max_hops < 1:
        errs.append("max hops must be 1 or more.")
    return errs


# ------------------------------------------------------------------
# Revit helpers
# ------------------------------------------------------------------
def get_name(el):
    """
    Read an element's name through the base class (CPython3 workaround for
    'TypeError: property cannot be read'), falling back to built-in parameters.
    """
    if el is None:
        return ""
    try:
        return str(Element.Name.__get__(el))
    except Exception:
        pass
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.VIEW_NAME):
        try:
            p = el.get_Parameter(bip)
            if p is not None:
                s = p.AsString()
                if s:
                    return str(s)
        except Exception:
            pass
    return ""


def get_family_name(etype):
    """Family name of a family type (empty string if unavailable)."""
    if etype is None:
        return ""
    try:
        return str(etype.FamilyName)
    except Exception:
        pass
    try:
        p = etype.get_Parameter(BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM)
        if p is not None:
            s = p.AsString()
            if s:
                return str(s)
    except Exception:
        pass
    return ""


def type_names(el):
    """(family name, type name) of an element; empty strings if unavailable."""
    try:
        etype = doc.GetElement(el.GetTypeId())
    except Exception:
        etype = None
    if etype is None:
        return "", ""
    return get_family_name(etype), get_name(etype)


def get_param_value(el, spec):
    """
    Text value of the parameter called `spec`, or None if missing/empty.
    Looks at the placed element first, then at its type.
    Special specs: @type, @family, @familytype.
    """
    key = spec.strip().lower()
    etype = doc.GetElement(el.GetTypeId())

    if key in ("@type", "@family", "@familytype"):
        if etype is None:
            return None
        fam = get_family_name(etype)
        typ = get_name(etype)
        if key == "@type":
            val = typ
        elif key == "@family":
            val = fam
        else:
            val = fam + " - " + typ
        val = val.strip()
        return val if val else None

    p = el.LookupParameter(spec.strip())
    if p is None and etype is not None:
        p = etype.LookupParameter(spec.strip())
    if p is None:
        return None
    val = p.AsString()
    if not val:
        val = p.AsValueString()
    return val.strip() if val else None


def find_targets():
    """(element, filter value) for mechanical equipment matching the filter."""
    matched = []
    col = (FilteredElementCollector(doc)
           .OfCategory(BuiltInCategory.OST_MechanicalEquipment)
           .WhereElementIsNotElementType())
    for el in col:
        if not isinstance(el, FamilyInstance):
            continue
        if is_pass_through(el, THROUGH_NAMES):
            continue                # a plenum is part of a route, never a target
        val = get_param_value(el, FILTER_PARAM)
        if val and filter_text in norm(val):
            matched.append((el, val))
    matched.sort(key=lambda t: t[1])
    return matched


def as_number(value):
    """Enum or number -> int, or None. Enums are plain ints in CPython3."""
    try:
        return int(value)
    except Exception:
        pass
    text = str(value).lower()
    if "supply" in text:
        return SYS_SUPPLY
    if "return" in text:
        return SYS_RETURN
    if "exhaust" in text:
        return SYS_EXHAUST
    if "curve" in text:
        return CONNECTOR_TYPE_CURVE
    return None


def hvac_connectors(el):
    out = []
    for c in get_connectors(el):
        try:
            if c.Domain != Domain.DomainHvac:
                continue
        except Exception:
            continue
        out.append(c)
    return out


def find_fcu_connectors(fcu):
    """
    Returns (return_connectors, supply_connectors, note). Same rules as the
    numbering script: system type first (2 = Return Air, 1 = Supply Air),
    Exhaust Air as return, then the flow direction as a last resort.
    """
    hvac = hvac_connectors(fcu)
    ret, sup, exh, seen = [], [], [], []
    for c in hvac:
        try:
            st = as_number(c.DuctSystemType)
        except Exception:
            st = None
        seen.append("?" if st is None else str(st))
        if st == SYS_RETURN:
            ret.append(c)
        elif st == SYS_SUPPLY:
            sup.append(c)
        elif st == SYS_EXHAUST:
            exh.append(c)

    note = ""
    if not ret and exh:
        ret = exh
        note = "no Return Air connector, used the Exhaust Air connector as return"

    if not ret or not sup:
        ret2, sup2 = [], []
        for c in hvac:
            try:
                d = as_number(c.Direction)
            except Exception:
                d = None
            if d == DIR_IN:
                ret2.append(c)
            elif d == DIR_OUT:
                sup2.append(c)
        if ret2 and sup2:
            ret, sup = ret2, sup2
            note = ("system types not usable ({}), sides taken from the flow "
                    "direction (In = return, Out = supply)".format(", ".join(seen)))
        else:
            note = "duct connectors found: {} (system types: {})".format(
                len(hvac), ", ".join(seen) or "none")
    return ret, sup, note


def longest_route(order, depth, parent, skipped):
    """Counted elements along the deepest path, listed from the FCU outward."""
    counted = [e for e in order if eid_int(e.Id) not in skipped]
    if not counted:
        return []
    by_id = {}
    for e in order:
        by_id[eid_int(e.Id)] = e
    leaf = max(counted, key=lambda e: (depth[eid_int(e.Id)], -eid_int(e.Id)))
    route = []
    cur = eid_int(leaf.Id)
    while cur is not None:
        if cur not in skipped:
            route.append(by_id[cur])
        cur = parent[cur]
    route.reverse()
    return route


def cat_int(el):
    cat = el.Category
    return eid_int(cat.Id) if cat is not None else None


def conn_size(c):
    """('rect', width mm, height mm) or ('round', diameter mm) or None."""
    try:
        w, h = c.Width, c.Height
        if w > 0 and h > 0:
            return ("rect", w * FT_TO_MM, h * FT_TO_MM)
    except Exception:
        pass
    try:
        r = c.Radius
        if r > 0:
            return ("round", 2.0 * r * FT_TO_MM)
    except Exception:
        pass
    return None


def size_text(size):
    if size is None:
        return "?"
    if size[0] == "rect":
        return "{:.0f}x{:.0f}".format(size[1], size[2])
    return "dia {:.0f}".format(size[1])


def connector_to(el, other_id):
    """The duct connector of el that is joined to the element with id other_id."""
    for c in hvac_connectors(el):
        try:
            if not c.IsConnected:
                continue
            for ref in c.AllRefs:
                o = ref.Owner
                if o is not None and eid_int(o.Id) == other_id:
                    return c
        except Exception:
            pass
    return None


def is_tap(el):
    """True if one connector of the fitting sits on the side of a duct."""
    sid = eid_int(el.Id)
    for c in hvac_connectors(el):
        try:
            for ref in c.AllRefs:
                o = ref.Owner
                if o is None or eid_int(o.Id) == sid:
                    continue
                if as_number(ref.ConnectorType) == CONNECTOR_TYPE_CURVE:
                    return True
        except Exception:
            pass
    return False


def bend_angle(c1, c2):
    """Bend between two connectors in degrees (0 = straight through), or None."""
    try:
        z1 = c1.CoordinateSystem.BasisZ
        z2 = c2.CoordinateSystem.BasisZ
        return abs(180.0 - math.degrees(z1.AngleTo(z2)))
    except Exception:
        return None


def connector_distance_m(c1, c2):
    try:
        return c1.Origin.DistanceTo(c2.Origin) * FT_TO_MM / 1000.0
    except Exception:
        return None


def terminal_flow_lps(el):
    """Design flow of an air terminal in L/s, or None if not set in Revit."""
    try:
        p = el.get_Parameter(BuiltInParameter.RBS_DUCT_FLOW_PARAM)
        if p is not None and p.HasValue:
            v = p.AsDouble() * CFS_TO_LPS
            if v > 0.01:
                return v
    except Exception:
        pass
    return None


def read_fcu_flow(fcu):
    """FCU design flow in L/s from the 'fcuflow' parameter, or None."""
    if not FCU_FLOW_PARAM:
        return None
    try:
        p = fcu.LookupParameter(FCU_FLOW_PARAM)
        if p is None:
            etype = doc.GetElement(fcu.GetTypeId())
            if etype is not None:
                p = etype.LookupParameter(FCU_FLOW_PARAM)
        if p is None:
            return None
        if p.StorageType == StorageType.Double:
            try:                    # the displayed value, e.g. "74.0 L/s"
                text = (p.AsValueString() or "").replace(",", ".").split()
                return float(text[0])
            except Exception:
                return p.AsDouble() * CFS_TO_LPS    # internal unit is ft3/s
        if p.StorageType == StorageType.Integer:
            return float(p.AsInteger())
        s = (p.AsString() or "").lower().replace("l/s", "").strip()
        return float(s) if s else None
    except Exception:
        return None


def read_number(el):
    """The element's number as an int, or None if the parameter is missing."""
    try:
        p = el.LookupParameter(NUM_PARAM)
        if p is None:
            return None
        if p.StorageType == StorageType.Integer:
            return p.AsInteger()
        s = p.AsString()
        return int(s) if s and s.strip().isdigit() else 0
    except Exception:
        return None


# ------------------------------------------------------------------
# Flow and classification
# ------------------------------------------------------------------
def side_flows(order, parent):
    """
    For one side: (flow, share, terminals, complete)
      flow[id]  = sum of the Revit flows of the air terminals beyond the element
      share[id] = fraction of the side's terminals beyond the element
      complete  = True if every terminal of the side has a flow in Revit
    'Beyond' = further from the FCU, following the walk.
    """
    flow, count = {}, {}
    terminals, complete = 0, True
    for e in order:
        eid = eid_int(e.Id)
        flow[eid] = 0.0
        count[eid] = 0
        if cat_int(e) == TERMINAL_CAT:
            terminals += 1
            count[eid] = 1
            q = terminal_flow_lps(e)
            if q is None:
                complete = False
            else:
                flow[eid] = q
    for e in reversed(order):               # children are found after parents
        eid = eid_int(e.Id)
        pid = parent[eid]
        if pid is not None:
            flow[pid] += flow[eid]
            count[pid] += count[eid]
    share = {}
    for eid in count:
        share[eid] = (float(count[eid]) / terminals) if terminals else 1.0
    if terminals == 0:
        complete = False
    return flow, share, terminals, complete


def direct_child_toward(target_id, el_id, parent):
    """Id of the neighbour of el that lies on the way to target (via the walk)."""
    cur = target_id
    guard = 0
    while cur is not None and guard < 10000:
        if parent.get(cur) == el_id:
            return cur
        cur = parent.get(cur)
        guard += 1
    return None


def accessory_name(el):
    fam, typ = type_names(el)
    text = (fam + " " + typ).lower()
    for word, name in ACCESSORY_NAMES:
        if word in text:
            return name
    return fam or "Accessory"


def describe(el, side, c_in, c_out):
    """
    (sheet name, kind, suggested code, size used, length m, note) for a row.
    c_in / c_out = connectors where the air enters / leaves (may be None).
    kind: straight, fitting or fixed.
    """
    cat = cat_int(el)
    fam, typ = type_names(el)
    s_in = conn_size(c_in) if c_in is not None else None
    s_out = conn_size(c_out) if c_out is not None else None
    if s_in is None:
        for c in hvac_connectors(el):
            s_in = conn_size(c)
            if s_in is not None:
                break
    size = s_in
    shape = size[0] if size else "rect"
    note_parts = []

    if cat == DUCT_CAT or cat == FLEX_CAT:
        length = duct_length_ft(el)
        name = "Straight Run" if cat == DUCT_CAT else "Flex Duct"
        return (name, "straight", "", size,
                (length * FT_TO_MM / 1000.0) if length is not None else None, "")

    if cat == TERMINAL_CAT:
        low = (fam + " " + typ).lower()
        if side == "return":
            name = "Extract Grill"
        elif "grill" in low:
            name = "Supply Grill"
        else:
            name = "Supply Diffuser"
        return (name, "fixed", typ, size, None, "")

    if cat == ACCESSORY_CAT:
        length = None
        if c_in is not None and c_out is not None:
            length = connector_distance_m(c_in, c_out)
        return (accessory_name(el), "fixed", "", size, length, "")

    if cat == EQUIPMENT_CAT:
        outs = [size_text(conn_size(c)) for c in hvac_connectors(el)
                if c is not c_in]
        note = "in {} -> out {}".format(size_text(s_in), ", ".join(outs) or "?")
        return ("Plenum", "fitting", ASHRAE_CODES.get(("plenum", side), ""),
                size, None, note)

    # duct fitting
    conns = hvac_connectors(el)
    n = len(conns)
    length = None
    if n >= 3:
        name = "Tee" if n == 3 else "Cross"
        others = [size_text(conn_size(c)) for c in conns
                  if c is not c_in and c is not c_out]
        note_parts.append("in {} -> route {}, branch {}".format(
            size_text(s_in), size_text(s_out), ", ".join(others) or "?"))
        code = ASHRAE_CODES.get(("tee", side), "")
    elif n == 2:
        a, b = conns[0], conns[1]
        if c_in is None or c_out is None:
            c_in, c_out = a, b
            s_out = conn_size(b)
        angle = bend_angle(a, b)
        length = connector_distance_m(a, b)
        if is_tap(el):
            name = "Tap"
            code = ASHRAE_CODES.get(("tap", side), "")
            note_parts.append("{} -> {}".format(size_text(s_in), size_text(s_out)))
        elif angle is not None and angle >= 3.0:
            name = "Elbow {:.0f}".format(angle)
            code = ASHRAE_CODES.get(("elbow", shape), "")
            length = None
            if s_out is not None and s_in is not None and s_out != s_in:
                note_parts.append("{} -> {}".format(size_text(s_in),
                                                    size_text(s_out)))
        elif s_in is not None and s_out is not None and (
                s_in[0] != s_out[0]
                or any(abs(x - y) > 0.5 for x, y in zip(s_in[1:], s_out[1:]))):
            name = "Transition"
            code = ASHRAE_CODES.get(("transition", side), "")
            note_parts.append("{} -> {}".format(size_text(s_in), size_text(s_out)))
        else:
            name = fam or "Fitting"
            code = ""
            length = None
    else:
        name = fam or "Fitting"
        code = ""
        note_parts.append("{} duct connector(s)".format(n))
    return (name, "fitting", code, size, length, "; ".join(note_parts))


def plane_width(c, other):
    """
    Width of a rectangular connector measured in the plane of the bend
    (the plane that also holds the other connector's direction), in mm.
    Falls back to the smaller side. Round: the diameter.
    """
    size = conn_size(c)
    if size is None:
        return None
    if size[0] == "round":
        return size[1]
    try:
        z = other.CoordinateSystem.BasisZ
        cs = c.CoordinateSystem
        along_w = abs(cs.BasisX.DotProduct(z))
        along_h = abs(cs.BasisY.DotProduct(z))
        if abs(along_w - along_h) > 0.2:
            return size[1] if along_w > along_h else size[2]
    except Exception:
        pass
    return min(size[1], size[2])


def room_of(el):
    """(name, volume m3) of the Space or Room an air terminal serves, or ('', None)."""
    for attr in ("Space", "Room"):
        try:
            r = getattr(el, attr)
            if r is None:
                continue
            vol = None
            try:
                v = r.Volume
                if v and v > 0:
                    vol = v * 0.028316846592        # ft3 -> m3
            except Exception:
                pass
            return get_name(r), vol
        except Exception:
            pass
    return "", None


def noise_inputs(side, order, depth, parent, skipped, flow, share, complete):
    """
    Inputs of the noise calculation for one side, along the route from the FCU
    to the NEAREST air terminal (least attenuation = the critical outlet).
    """
    by_id = {}
    for e in order:
        by_id[eid_int(e.Id)] = e
    terms = [e for e in order if cat_int(e) == TERMINAL_CAT]
    if not terms:
        return None
    outlet = min(terms, key=lambda e: (depth[eid_int(e.Id)], eid_int(e.Id)))
    chain = []
    cur = eid_int(outlet.Id)
    while cur is not None:
        chain.append(by_id[cur])
        cur = parent[cur]

    smallest, duct_len, flex_len = None, 0.0, 0.0
    short, longr, other_elbows, accessories = {}, {}, 0, []
    for e in chain:
        cat = cat_int(e)
        if cat == DUCT_CAT or cat == FLEX_CAT:
            L = duct_length_ft(e)
            L = (L * FT_TO_MM / 1000.0) if L is not None else 0.0
            if cat == FLEX_CAT:
                flex_len += L
                continue
            duct_len += L
            for c in hvac_connectors(e):
                size = conn_size(c)
                if size is not None:
                    m = min(size[1:])
                    if smallest is None or m < smallest:
                        smallest = m
                    break
        elif cat == ACCESSORY_CAT:
            accessories.append(accessory_name(e))
        elif cat == FITTING_CAT:
            conns = hvac_connectors(e)
            if len(conns) != 2 or is_tap(e):
                continue
            angle = bend_angle(conns[0], conns[1])
            if angle is None or angle < 60.0:
                if angle is not None and angle >= 3.0:
                    other_elbows += 1
                continue
            w = plane_width(conns[0], conns[1])
            if w is None:
                continue
            w = int(round(w))
            radius = None
            d = connector_distance_m(conns[0], conns[1])
            try:
                radius = d * 1000.0 / (2.0 * math.sin(math.radians(angle) / 2.0))
            except Exception:
                pass
            # centreline radius / width: below 1.25 = short radius
            if radius is not None and radius / w >= 1.25:
                longr[w] = longr.get(w, 0) + 1
            else:
                short[w] = short.get(w, 0) + 1

    oid = eid_int(outlet.Id)
    area = None
    for c in hvac_connectors(outlet):
        size = conn_size(c)
        if size is not None:
            if size[0] == "rect":
                area = size[1] * size[2] / 100.0            # mm2 -> cm2
            else:
                area = math.pi * size[1] ** 2 / 400.0
            break
    fam, typ = type_names(outlet)
    room_name, room_vol = room_of(outlet)
    notes = []
    if flex_len > 0:
        notes.append("flex duct on the route: {:.2f} m (not in the length)"
                     .format(flex_len))
    if accessories:
        notes.append("on the route: " + ", ".join(accessories))
    if other_elbows:
        notes.append("{} bend(s) under 60 deg not counted".format(other_elbows))
    for label, group in (("short", short), ("long", longr)):
        if len(group) > 3:
            notes.append("more than 3 {} radius elbow widths: {}".format(
                label, ", ".join("{}x{}".format(q, w)
                                 for w, q in sorted(group.items()))))
    return {
        "side": side.upper(), "smallest": smallest, "length": duct_len,
        "short": sorted(short.items())[:3], "long": sorted(longr.items())[:3],
        "area": area, "flow": flow[oid] if complete else None,
        "share": share[oid], "room": room_name, "volume": room_vol,
        "outlet": "{} (id {})".format(typ or fam, oid), "notes": "; ".join(notes),
        "terminals": len(terms),
    }


def plan_fcu(fcu, label):
    """
    Read-only: build the rows for one FCU.
    Returns (report_lines, sheet) where sheet is None on failure, else a dict
    with 'label', 'rows', 'fcu_flow', 'need_flow'.
    """
    ret_c, sup_c, note = find_fcu_connectors(fcu)
    if not ret_c or not sup_c:
        return (["FAIL | {} | needs a Return Air and a Supply Air duct connector | {}"
                 .format(label, note)], None)

    fid = eid_int(fcu.Id)
    ret_roots, sup_roots = [], []
    for c in ret_c:
        ret_roots.extend(neighbors_of_connector(c, fid, THROUGH_NAMES))
    for c in sup_c:
        sup_roots.extend(neighbors_of_connector(c, fid, THROUGH_NAMES))
    if not ret_roots or not sup_roots:
        side = "Return Air" if not ret_roots else "Supply Air"
        return (["FAIL | {} | the {} connector is not connected to any duct, fitting, "
                 "accessory, air terminal or plenum".format(label, side)], None)

    r_order, r_depth, r_parent, r_skip, r_trunc, _ = walk_side(
        ret_roots, set(), max_hops, min_len_ft, THROUGH_NAMES)
    s_order, s_depth, s_parent, s_skip, s_trunc, _ = walk_side(
        sup_roots, set(r_depth.keys()), max_hops, min_len_ft, THROUGH_NAMES)

    r_route = longest_route(r_order, r_depth, r_parent, r_skip)   # FCU -> far end
    s_route = longest_route(s_order, s_depth, s_parent, s_skip)   # FCU -> far end
    R = len(r_route)

    r_flow, r_share, r_terms, r_complete = side_flows(r_order, r_parent)
    s_flow, s_share, s_terms, s_complete = side_flows(s_order, s_parent)

    fcu_flow = read_fcu_flow(fcu)
    if fcu_flow is None and s_complete:
        fcu_flow = sum(s_flow[eid_int(e.Id)] for e in s_order
                       if s_parent[eid_int(e.Id)] is None)
    if fcu_flow is None and r_complete:
        fcu_flow = sum(r_flow[eid_int(e.Id)] for e in r_order
                       if r_parent[eid_int(e.Id)] is None)

    rows = []
    mismatches = 0
    plan = (("return", list(reversed(r_route)), r_route, r_parent, r_depth,
             r_flow, r_share, r_complete),
            ("supply", s_route, s_route, s_parent, s_depth,
             s_flow, s_share, s_complete))
    for side, ordered, route, parent, depth, flow, share, complete in plan:
        pos = {}
        for i, e in enumerate(route):               # route is FCU -> far end
            pos[eid_int(e.Id)] = i
        for e in ordered:
            eid = eid_int(e.Id)
            i = pos[eid]
            # connector on the FCU side, and the one toward the far end
            pid = parent[eid]
            c_near = connector_to(e, pid if pid is not None else fid)
            c_far = None
            if i + 1 < len(route):
                child = direct_child_toward(eid_int(route[i + 1].Id), eid, parent)
                if child is not None:
                    c_far = connector_to(e, child)
            if c_far is None:
                rest = [c for c in hvac_connectors(e) if c is not c_near]
                if len(rest) == 1:
                    c_far = rest[0]
            if side == "supply":
                c_in, c_out = c_near, c_far
                number = R + depth[eid]
            else:
                c_in, c_out = c_far, c_near
                number = R - depth[eid] + 1

            name, kind, code, size, length, note = describe(e, side, c_in, c_out)
            fam, typ = type_names(e)
            stored = read_number(e)
            if stored is not None and stored != number:
                mismatches += 1
            rows.append({
                "number": number, "name": name, "kind": kind, "code": code,
                "size": size, "length": length, "note": note,
                "flow": flow[eid] if complete else None,
                "share": share[eid], "id": eid,
                "type": (fam + " : " + typ) if fam else typ,
                "fixed": FIXED_LOSS.get(name),
            })

    need_flow = (not r_complete) or (not s_complete)
    noise = []
    for args in (("return", r_order, r_depth, r_parent, r_skip, r_flow, r_share,
                  r_complete),
                 ("supply", s_order, s_depth, s_parent, s_skip, s_flow, s_share,
                  s_complete)):
        try:
            n = noise_inputs(*args)
        except Exception:
            n = None
        if n is not None:
            noise.append(n)
    lines = ["PLAN | {} | {} rows (return {} + supply {}) | air terminals: return {}, "
             "supply {} | flows from {}".format(
                 label, len(rows), len(r_route), len(s_route), r_terms, s_terms,
                 "the Revit air terminals" if not need_flow else
                 "the FCU flow shared equally between terminals"
                 + ("" if fcu_flow is not None else " (type the FCU flow in the "
                    "yellow cell)"))]
    if note:
        lines.append("NOTE | {} | {}".format(label, note))
    if r_trunc or s_trunc:
        lines.append("WARNING | {} | hop limit reached, network may be incomplete"
                     .format(label))
    if mismatches:
        lines.append("WARNING | {} | {} element(s) carry a different number in '{}' "
                     "than this route gives: run the numbering script again"
                     .format(label, mismatches, NUM_PARAM))
    if not r_route or not s_route:
        lines.append("WARNING | {} | the {} side has no countable element".format(
            label, "return" if not r_route else "supply"))
    for r in rows:
        lines.append("  {} | {:>3} | {} | {} | {} | L {} | {}".format(
            label, r["number"], r["name"], r["code"] or "-", size_text(r["size"]),
            "-" if r["length"] is None else "{:.3f} m".format(r["length"]),
            ("{:.1f} L/s".format(r["flow"]) if r["flow"] is not None
             else "{:.0f}% of FCU flow".format(100 * r["share"]))))
    for n in noise:
        lines.append("  {} | NOISE {} | smallest duct {} mm, length {:.2f} m | "
                     "elbows short {} long {} | outlet {} cm2".format(
                         label, n["side"],
                         "?" if n["smallest"] is None else int(round(n["smallest"])),
                         n["length"],
                         ", ".join("{}x{}".format(q, w) for w, q in n["short"])
                         or "0",
                         ", ".join("{}x{}".format(q, w) for w, q in n["long"])
                         or "0",
                         "?" if n["area"] is None else int(round(n["area"]))))
    return lines, {"label": label, "rows": rows, "fcu_flow": fcu_flow,
                   "need_flow": need_flow, "noise": noise}


# ------------------------------------------------------------------
# Excel writer (standard library only, no extra packages needed)
# ------------------------------------------------------------------
S_NORMAL, S_BOLD, S_YELLOW, S_HEAD, S_NUM2, S_TITLE, S_BOLDNUM, S_INT, S_NUM3 = \
    0, 1, 2, 3, 4, 5, 6, 7, 8

HEADERS = ["Element number on figure", "Duct Element", "ASHRAE Fitting number",
           "Flow rate /L.s-1", "Width /mm", "Depth /mm", "Length /m",
           "Fitting loss coefficient", "EQ DIA /mm", "Velocity /m.s-1",
           "Reynold Number", "f' factor", "Friction factor F",
           "Velocity Pressure /Pa", "Friction /Pa.m-1", "Velocity /fpm",
           "Total Pressure Loss /Pa", "", "Revit id", "Family : Type", "Notes"]
COL_WIDTHS = [10, 20, 14, 10, 9, 9, 9, 11, 10, 10, 11, 9, 9, 10, 10, 10, 12, 3,
              11, 42, 40]
FIRST_ROW = 6
FLOW_CELL = "$D$3"


def xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def col_letter(index):
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        s = chr(65 + rem) + s
    return s


def cell_xml(ref, value, style=S_NORMAL):
    """value: None, number, text, or ('f', formula)."""
    if value is None:
        return '<c r="{}" s="{}"/>'.format(ref, style)
    if isinstance(value, tuple):
        return '<c r="{}" s="{}"><f>{}</f></c>'.format(ref, style,
                                                      xml_escape(value[1]))
    if isinstance(value, (int, float)):
        return '<c r="{}" s="{}"><v>{}</v></c>'.format(ref, style, repr(value))
    return ('<c r="{}" s="{}" t="inlineStr"><is><t xml:space="preserve">{}</t></is>'
            '</c>'.format(ref, style, xml_escape(value)))


def sheet_xml(cells, widths, freeze_row=None):
    """cells: {(row, col): (value, style)} with 1-based row, 0-based col."""
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
           '2006/main">']
    if freeze_row:
        out.append('<sheetViews><sheetView workbookViewId="0"><pane ySplit="{0}" '
                   'topLeftCell="A{1}" activePane="bottomLeft" state="frozen"/>'
                   '</sheetView></sheetViews>'.format(freeze_row, freeze_row + 1))
    out.append("<cols>")
    for i, w in enumerate(widths):
        out.append('<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'
                   .format(i + 1, w))
    out.append("</cols><sheetData>")
    rows = {}
    for (r, c), v in cells.items():
        rows.setdefault(r, {})[c] = v
    for r in sorted(rows):
        out.append('<row r="{}">'.format(r))
        for c in sorted(rows[r]):
            value, style = rows[r][c]
            out.append(cell_xml("{}{}".format(col_letter(c), r), value, style))
        out.append("</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="2"><numFmt numFmtId="164" formatCode="0.00"/>'
    '<numFmt numFmtId="165" formatCode="0.000"/></numFmts>'
    '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="14"/><name val="Calibri"/></font></fonts>'
    '<fills count="4"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFF00"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/>'
    '<bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
    '<border><left style="thin"/><right style="thin"/><top style="thin"/>'
    '<bottom style="thin"/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
    '</cellStyleXfs>'
    '<cellXfs count="9">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="2" borderId="1" xfId="0" applyFill="1" '
    'applyBorder="1" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="3" borderId="1" xfId="0" applyFont="1" '
    'applyFill="1" applyBorder="1" applyAlignment="1"><alignment wrapText="1" '
    'horizontal="center" vertical="center"/></xf>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" '
    'applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" '
    'applyNumberFormat="1"/>'
    '<xf numFmtId="1" fontId="0" fillId="0" borderId="0" xfId="0" '
    'applyNumberFormat="1"/>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" '
    'applyNumberFormat="1"/>'
    '</cellXfs></styleSheet>')


def fcu_sheet_cells(sheet):
    """Cells of one FCU sheet. Returns (cells, total row, safety total row)."""
    cells = {}
    cells[(1, 0)] = ("PRESSURE DROP CALCULATION", S_TITLE)
    cells[(2, 0)] = (sheet["label"], S_TITLE)
    cells[(3, 0)] = ("FCU flow /L.s-1", S_BOLD)
    if sheet["fcu_flow"] is not None:
        cells[(3, 3)] = (round(sheet["fcu_flow"], 1), S_NUM2)
    else:
        cells[(3, 3)] = (None, S_YELLOW)
    cells[(3, 5)] = ("Yellow cells: fill in by hand (loss coefficient from the "
                     "ASHRAE Duct Fitting Database, fixed losses from the "
                     "manufacturer).", S_NORMAL)
    cells[(4, 0)] = ("INPUT", S_BOLD)
    cells[(4, 8)] = ("OUTPUT", S_BOLD)
    for c, h in enumerate(HEADERS):
        if h:
            cells[(5, c)] = (h, S_HEAD)

    r = FIRST_ROW
    for row in sheet["rows"]:
        n = str(r)
        cells[(r, 0)] = (row["number"], S_INT)
        cells[(r, 1)] = (row["name"], S_NORMAL)
        if row["code"]:
            cells[(r, 2)] = (row["code"], S_NORMAL)
        if row["flow"] is not None:
            cells[(r, 3)] = (round(row["flow"], 2), S_NUM2)
        else:
            cells[(r, 3)] = (("f", "{}*{}".format(FLOW_CELL,
                                                  round(row["share"], 6))), S_NUM2)
        size = row["size"]
        if size is not None and size[0] == "rect":
            cells[(r, 4)] = (round(size[1]), S_INT)
            cells[(r, 5)] = (round(size[2]), S_INT)
            cells[(r, 8)] = (("f", "1.3*(E{0}*F{0})^0.625/(E{0}+F{0})^0.25"
                              .format(n)), S_NUM2)
        elif size is not None:
            cells[(r, 8)] = (round(size[1]), S_NUM2)
        else:
            cells[(r, 8)] = (None, S_YELLOW)
        if row["length"] is not None:
            cells[(r, 6)] = (round(row["length"], 3), S_NUM3)
        if row["kind"] == "fitting":
            cells[(r, 7)] = (None, S_YELLOW)
        cells[(r, 9)] = (("f", "(D{0}/1000)/((3.14/4)*((I{0}/1000)^2))".format(n)),
                         S_NUM2)
        cells[(r, 10)] = (("f", "66.4*J{0}*(I{0})".format(n)), S_INT)
        cells[(r, 11)] = (("f", "0.11*(((0.09/I{0})+(68/K{0}))^0.25)".format(n)),
                          S_NUM3)
        cells[(r, 12)] = (("f", "(0.85*L{0})+0.0028".format(n)), S_NUM3)
        cells[(r, 13)] = (("f", "J{0}^2*1.21/2".format(n)), S_NUM2)
        cells[(r, 14)] = (("f", "((J{0}^2)/2)*1.2*M{0}*(1000/I{0})".format(n)),
                          S_NUM3)
        cells[(r, 15)] = (("f", "J{0}*196.85".format(n)), S_NUM2)
        if row["kind"] == "straight":
            cells[(r, 16)] = (("f", "O{0}*G{0}".format(n)), S_NUM2)
        elif row["kind"] == "fitting":
            cells[(r, 16)] = (("f", "(H{0}*POWER(P{0}/4005,2))*254".format(n)),
                              S_NUM2)
        elif row["fixed"] is not None:
            cells[(r, 16)] = (row["fixed"], S_NUM2)
        else:
            cells[(r, 16)] = (None, S_YELLOW)
        cells[(r, 18)] = (row["id"], S_INT)
        cells[(r, 19)] = (row["type"], S_NORMAL)
        if row["note"]:
            cells[(r, 20)] = (row["note"], S_NORMAL)
        r += 1

    last = r - 1
    cells[(r, 7)] = ("TOTAL Pressure Drop (Pa)", S_BOLD)
    cells[(r, 16)] = (("f", "SUM(Q{}:Q{})".format(FIRST_ROW, max(last, FIRST_ROW))),
                      S_BOLDNUM)
    cells[(r + 1, 7)] = ("+% Safety", S_BOLD)
    cells[(r + 1, 16)] = (SAFETY, S_NUM2)
    cells[(r + 2, 7)] = ("TOTAL (Pa)", S_BOLD)
    cells[(r + 2, 16)] = (("f", "Q{}*(1+Q{}/100)".format(r, r + 1)), S_BOLDNUM)
    return cells, r, r + 2


NOISE_HEADERS = [
    ("FCU", ""), ("Side", "A1"), ("Total flow /L.s-1", "F8"),
    ("Smallest duct dimension /mm", "C13"), ("Duct length /m", "F13"),
    ("Short radius elbow 1 width /mm", "C15"), ("Qty", "F15"),
    ("Short radius elbow 2 width /mm", "C16"), ("Qty", "F16"),
    ("Short radius elbow 3 width /mm", "C17"), ("Qty", "F17"),
    ("Long radius elbow 1 width /mm", "C19"), ("Qty", "F19"),
    ("Long radius elbow 2 width /mm", "C20"), ("Qty", "F20"),
    ("Long radius elbow 3 width /mm", "C21"), ("Qty", "F21"),
    ("Critical outlet area /cm2", "C23"), ("Outlet flow /L.s-1", "F23"),
    ("Percentage leaving outlet", "C27"), ("Room volume /m3", "C34"),
    ("Room", ""), ("Critical outlet", ""), ("Air terminals on this side", ""),
    ("Notes", ""),
]
NOISE_WIDTHS = [22, 9, 10, 11, 9, 11, 5, 11, 5, 11, 5, 11, 5, 11, 5, 11, 5, 11, 10,
                11, 10, 24, 30, 10, 60]


def noise_sheet_cells(sheets, names):
    """One row per FCU side with the inputs of the noise calculation sheet."""
    cells = {
        (1, 0): ("NOISE CALCULATION INPUTS", S_TITLE),
        (2, 0): ("Route from the FCU to the nearest air terminal of each side. "
                 "Row 4 gives the cell of the noise calculation sheet each value "
                 "goes into. Source sound power, distance to listener, "
                 "directivity, reverberation time and NC are not in the model.",
                 S_NORMAL),
    }
    for c, (text, ref) in enumerate(NOISE_HEADERS):
        cells[(3, c)] = (text, S_HEAD)
        if ref:
            cells[(4, c)] = (ref, S_BOLD)
    r = 5
    for sheet, name in zip(sheets, names):
        flow_ref = "'" + name.replace("'", "''") + "'!D3"
        for n in sheet.get("noise", []):
            cells[(r, 0)] = (sheet["label"], S_NORMAL)
            cells[(r, 1)] = (n["side"], S_NORMAL)
            cells[(r, 2)] = (("f", flow_ref), S_NUM2)
            if n["smallest"] is not None:
                cells[(r, 3)] = (int(round(n["smallest"])), S_INT)
            cells[(r, 4)] = (round(n["length"], 2), S_NUM2)
            for base, group in ((5, n["short"]), (11, n["long"])):
                for k, (w, q) in enumerate(group):
                    cells[(r, base + 2 * k)] = (w, S_INT)
                    cells[(r, base + 2 * k + 1)] = (q, S_INT)
            if n["area"] is not None:
                cells[(r, 17)] = (round(n["area"], 1), S_NUM2)
            if n["flow"] is not None:
                cells[(r, 18)] = (round(n["flow"], 2), S_NUM2)
            else:
                cells[(r, 18)] = (("f", "{}*{}".format(flow_ref,
                                                       round(n["share"], 6))), S_NUM2)
            cells[(r, 19)] = (("f", "IF(C{0}=0,0,S{0}/C{0})".format(r)), S_NUM2)
            if n["volume"] is not None:
                cells[(r, 20)] = (round(n["volume"], 1), S_NUM2)
            if n["room"]:
                cells[(r, 21)] = (n["room"], S_NORMAL)
            cells[(r, 22)] = (n["outlet"], S_NORMAL)
            cells[(r, 23)] = (n["terminals"], S_INT)
            if n["notes"]:
                cells[(r, 24)] = (n["notes"], S_NORMAL)
            r += 1
    return cells


def safe_sheet_names(labels):
    """Excel sheet names: max 31 characters, no []:*?/\\ and unique."""
    names, used = [], set()
    for label in labels:
        base = "".join("-" if ch in '[]:*?/\\' else ch for ch in label)[:31] or "FCU"
        name, k = base, 2
        while name.lower() in used:
            suffix = "_{}".format(k)
            name = base[:31 - len(suffix)] + suffix
            k += 1
        used.add(name.lower())
        names.append(name)
    return names


def write_workbook(path, sheets):
    """Write one workbook: a Summary sheet, then one sheet per FCU."""
    names = safe_sheet_names(["Summary", "Noise inputs"]
                             + [s["label"] for s in sheets])[2:]
    parts = []
    summary = {
        (1, 0): ("FCU PRESSURE DROP SUMMARY", S_TITLE),
        (3, 0): ("FCU", S_HEAD), (3, 1): ("Flow /L.s-1", S_HEAD),
        (3, 2): ("Elements", S_HEAD), (3, 3): ("Total /Pa", S_HEAD),
        (3, 4): ("Total + safety /Pa", S_HEAD),
    }
    for i, (sheet, name) in enumerate(zip(sheets, names)):
        cells, total_row, safe_row = fcu_sheet_cells(sheet)
        parts.append(sheet_xml(cells, COL_WIDTHS, freeze_row=5))
        quoted = "'" + name.replace("'", "''") + "'"
        r = 4 + i
        summary[(r, 0)] = (sheet["label"], S_NORMAL)
        summary[(r, 1)] = (("f", "{}!D3".format(quoted)), S_NUM2)
        summary[(r, 2)] = (len(sheet["rows"]), S_INT)
        summary[(r, 3)] = (("f", "{}!Q{}".format(quoted, total_row)), S_NUM2)
        summary[(r, 4)] = (("f", "{}!Q{}".format(quoted, safe_row)), S_NUM2)
    all_names = ["Summary", "Noise inputs"] + names
    all_parts = [sheet_xml(summary, [34, 12, 10, 12, 18]),
                 sheet_xml(noise_sheet_cells(sheets, names), NOISE_WIDTHS,
                           freeze_row=4)] + parts

    ns = "http://schemas.openxmlformats.org/"
    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="{}package/2006/content-types">'.format(ns),
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        'package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<workbook xmlns="{0}spreadsheetml/2006/main" xmlns:r="{0}officeDocument/'
          '2006/relationships"><sheets>'.format(ns)]
    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="{}package/2006/relationships">'.format(ns)]
    for i, name in enumerate(all_names):
        k = i + 1
        content_types.append(
            '<Override PartName="/xl/worksheets/sheet{}.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.'
            'worksheet+xml"/>'.format(k))
        wb.append('<sheet name="{}" sheetId="{}" r:id="rId{}"/>'.format(
            xml_escape(name), k, k))
        rels.append('<Relationship Id="rId{0}" Type="{1}officeDocument/2006/'
                    'relationships/worksheet" Target="worksheets/sheet{0}.xml"/>'
                    .format(k, ns))
    content_types.append("</Types>")
    wb.append('</sheets><calcPr calcId="0" fullCalcOnLoad="1"/></workbook>')
    rels.append('<Relationship Id="rId{}" Type="{}officeDocument/2006/relationships/'
                'styles" Target="styles.xml"/></Relationships>'.format(
                    len(all_names) + 1, ns))
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="{0}package/2006/relationships">'
                 '<Relationship Id="rId1" Type="{0}officeDocument/2006/relationships/'
                 'officeDocument" Target="xl/workbook.xml"/></Relationships>'
                 .format(ns))

    try:
        zf = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    except Exception:
        zf = zipfile.ZipFile(path, "w", zipfile.ZIP_STORED)
    try:
        zf.writestr("[Content_Types].xml", "".join(content_types))
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", "".join(wb))
        zf.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
        zf.writestr("xl/styles.xml", STYLES_XML)
        for i, part in enumerate(all_parts):
            zf.writestr("xl/worksheets/sheet{}.xml".format(i + 1), part)
    finally:
        zf.close()


def output_path():
    folder = OUT_FOLDER or os.path.join(os.path.expanduser("~"), "Documents",
                                        "FCU pressure loss")
    if not os.path.isdir(folder):
        os.makedirs(folder)
    tag = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in filter_text)
    name = "FCU_pressure_loss_{}_{}.xlsx".format(
        tag.upper(), time.strftime("%Y%m%d_%H%M%S"))
    return os.path.join(folder, name)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    errs = option_errors()
    if errs:
        return ["INPUT ERROR | " + e for e in errs]

    if not filter_text:
        return ["Filter text is empty - refusing to run on all equipment."]

    fcus = find_targets()
    if not fcus:
        return ["No equipment with '{}' containing '{}'."
                .format(FILTER_PARAM, filter_text)]

    report = ["Settings ({}): ducts <= {:.0f} mm not counted, through={}, safety "
              "{:g}%, FCU flow parameter: {}".format(
                  VERSION, min_len_mm,
                  ", ".join(THROUGH_NAMES) if THROUGH_NAMES else "(none)", SAFETY,
                  FCU_FLOW_PARAM or "(none)"),
              "Rows: FCU | number | element | suggested ASHRAE code | size mm | "
              "length | flow"]
    sheets = []
    for fcu, label in fcus:
        try:
            lines, sheet = plan_fcu(fcu, label)
        except Exception as ex:
            lines, sheet = ["FAIL | {} | {}".format(label, ex)], None
        report.extend(lines)
        if sheet is not None and sheet["rows"]:
            sheets.append(sheet)

    failed = len(fcus) - len(sheets)
    if not run:
        report.insert(0, "Summary ({}): PREVIEW ONLY - no file was written. FCUs "
                         "matched: {}, failed: {}. Set Run to True to write the "
                         "Excel file.".format(VERSION, len(fcus), failed))
        return report

    if not sheets:
        report.insert(0, "Summary ({}): nothing to write. FCUs matched: {}, "
                         "failed: {}".format(VERSION, len(fcus), failed))
        return report

    try:
        path = output_path()
        write_workbook(path, sheets)
    except Exception as ex:
        report.insert(0, "Summary ({}): the Excel file could not be written: {}"
                      .format(VERSION, ex))
        return report

    report.insert(0, "Summary ({}): Excel file written with {} FCU sheet(s), "
                     "failed: {} | {}".format(VERSION, len(sheets), failed, path))
    return report


# ------------------------------------------------------------------
# Run, and also save the report to %USERPROFILE%\fcu_script04_report.txt
# ------------------------------------------------------------------
try:
    _result = main()
except Exception:
    _result = ["SCRIPT ERROR:", traceback.format_exc()]

try:
    _path = os.path.join(os.path.expanduser("~"), "fcu_script04_report.txt")
    with open(_path, "w", encoding="utf-8") as _f:
        _f.write("\n".join(str(x) for x in _result))
    _result.append("Report saved to: " + _path)
except Exception:
    pass

OUT = _result
