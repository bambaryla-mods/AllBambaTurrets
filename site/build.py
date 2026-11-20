"""Build the All-Bamba Turrets player site.

    python site/build.py                     # writes site/_build
    python site/build.py --serve             # builds, then serves it on http://localhost:8000
    python site/build.py --out DIR --strict  # what CI runs
    python site/build.py --refresh-external  # re-copy the vanilla/CE base defs (needs the game installed)

The page text is site/content/index.md. It can call the helpers in Helpers below,
e.g. {{ turret_tables("industrial") }}, to pull generated tables into the text.
"""
from __future__ import annotations

import argparse
import copy
import functools
import http.server
import re
import shutil
import sys
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from lxml import etree
from markupsafe import Markup

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent
GAME_VERSION = "1.7"
EXTERNAL = SITE / "external_defs.xml"
BUILD_MARKER = ".abt-site-build"  # a rebuild only ever deletes a folder it made itself

CE_PACKAGE_ID = "ceteam.combatextended"
CE_WORKSHOP_ID = "2890901044"
GITHUB = "https://github.com/databobek/AllBambaTurrets"
SITE_URL = "https://databobek.github.io/AllBambaTurrets/"

# Research project -> tier shown on the page, in page order.
TIERS = {
    "Bamba_Turret_Research_Industrial": "industrial",
    "Bamba_Turret_Research_Spacer": "spacer",
    "Bamba_Turret_Research_Ultra": "ultra",
}
TIER_TITLES = {"industrial": "Industrial", "spacer": "Spacer", "ultra": "Ultra"}

PARSER = etree.XMLParser(remove_comments=True, remove_blank_text=True, recover=True)


# ---------------------------------------------------------------- defs

def read_defs(path: Path) -> list:
    try:
        root = etree.parse(str(path), PARSER).getroot()
    except (etree.XMLSyntaxError, OSError):
        return []
    if root is None or root.tag != "Defs":
        return []
    return [el for el in root if isinstance(el.tag, str)]


def load_folders(ce: bool) -> list[Path]:
    """The folders LoadFolders.xml loads for GAME_VERSION, with only CE as an optional mod."""
    tree = etree.parse(str(ROOT / "LoadFolders.xml"), PARSER).getroot()
    out = []
    for li in tree.find(f"v{GAME_VERSION}"):
        active, inactive = li.get("IfModActive"), li.get("IfModNotActive")
        if active and not (ce and active.lower() == CE_PACKAGE_ID):
            continue
        if inactive and ce and inactive.lower() == CE_PACKAGE_ID:
            continue
        out.append(ROOT / li.text.strip().strip("/"))
    return out


def _child(el, tag):
    for c in el:
        if c.tag == tag:
            return c
    return None


def _merge(current, child):
    """RimWorld's XmlInheritance: list items append, Inherit="False" replaces,
    leaves replace text, other elements merge recursively."""
    for ch in child:
        if not isinstance(ch.tag, str):
            continue
        if (ch.get("Inherit") or "").lower() == "false":
            existing = _child(current, ch.tag)
            if existing is not None:
                current.replace(existing, copy.deepcopy(ch))
            else:
                current.append(copy.deepcopy(ch))
            continue
        if ch.tag == "li":
            current.append(copy.deepcopy(ch))
            continue
        existing = _child(current, ch.tag)
        if existing is None:
            current.append(copy.deepcopy(ch))
            continue
        for k, v in ch.attrib.items():
            existing.set(k, v)
        if len(ch) == 0:
            for c in list(existing):
                existing.remove(c)
            existing.text = ch.text
            continue
        _merge(existing, ch)


class DefDB:
    """Every def one set of active mods sees, with inheritance resolved on lookup."""

    def __init__(self, ce: bool, problems: list[str]):
        self.ce = ce
        self.problems = problems
        self.by_name: dict[str, object] = {}   # Name="" -> element, for parents
        self.by_def: dict[tuple[str, str], object] = {}
        self.order: list = []                  # mod defs in load order
        if EXTERNAL.is_file():
            for el in read_defs(EXTERNAL):
                if el.get("source") == "ce" and not ce:
                    continue
                self._add(el, mod=False)
        for folder in load_folders(ce):
            for path in sorted((folder / "Defs").rglob("*.xml")):
                for el in read_defs(path):
                    self._add(el, mod=True)

    def _add(self, el, mod: bool):
        if el.get("Name"):
            self.by_name[el.get("Name")] = el
        dn = el.findtext("defName")
        if dn:
            self.by_def[(_kind(el.tag), dn.strip())] = el
        if mod:
            self.order.append(el)

    @functools.lru_cache(maxsize=None)
    def resolve_el(self, el):
        parent = el.get("ParentName")
        if not parent:
            return el
        base = self.by_name.get(parent)
        if base is None:
            self.problems.append(f"unknown parent {parent} ({'CE' if self.ce else 'vanilla'})")
            return el
        out = copy.deepcopy(self.resolve_el(base))
        for k in ("Name", "Abstract"):
            out.attrib.pop(k, None)
        for k, v in el.attrib.items():
            if k not in ("Name", "Abstract", "ParentName"):
                out.set(k, v)
        _merge(out, el)
        return out

    def get(self, kind: str, def_name: str | None):
        if not def_name:
            return None
        el = self.by_def.get((kind, def_name.strip()))
        return self.resolve_el(el) if el is not None else None

    def label(self, kind: str, def_name: str) -> str:
        el = self.get(kind, def_name)
        return (el.findtext("label") if el is not None else None) or def_name


def _kind(tag: str) -> str:
    return tag.rsplit(".", 1)[-1]


def num(el, path: str, default=None):
    if el is None:
        return default
    t = el.findtext(path)
    try:
        return float(t) if t is not None else default
    except ValueError:
        return default


def comp(el, cls: str):
    for li in el.findall("comps/li"):
        if li.get("Class") == cls:
            return li
    return None


# ---------------------------------------------------------------- turrets

def turret_rows(db: DefDB) -> list[dict]:
    rows = []
    for raw in db.order:
        if raw.tag != "ThingDef" or raw.get("Abstract", "").lower() == "true":
            continue
        t = db.resolve_el(raw)
        gun = db.get("ThingDef", t.findtext("building/turretGunDef"))
        if gun is None:
            continue
        research = [li.text.strip() for li in t.findall("researchPrerequisites/li")]
        tier = next((TIERS[r] for r in research if r in TIERS), "other")
        verb = gun.find("verbs/li")
        power = comp(t, "CompProperties_Power")
        cooldown = num(t, "building/turretBurstCooldownTime")
        row = {
            "def": t.findtext("defName"),
            "label": t.findtext("label"),
            "desc": (t.findtext("description") or "").strip(),
            "gun_desc": (gun.findtext("description") or "").strip(),
            "icon": t.findtext("uiIconPath") or t.findtext("graphicData/texPath"),
            "tier": tier,
            "hp": num(t, "statBases/MaxHitPoints"),
            "work": num(t, "statBases/WorkToBuild"),
            "size": (t.findtext("size") or "(1,1)").strip("() ").replace(",", "×"),
            "skill": num(t, "constructionSkillPrerequisite"),
            "power": num(power, "basePowerConsumption"),
            "cost": [(db.label("ThingDef", c.tag), int(float(c.text))) for c in t.findall("costList/*")],
            "research": [db.label("ResearchProjectDef", r) for r in research],
            "range": num(verb, "range"),
            "min_range": num(verb, "minRange"),
            "warmup": num(verb, "warmupTime"),
            "burst": int(num(verb, "burstShotCount", 1)),
            "burst_ticks": num(verb, "ticksBetweenBurstShots"),
            "cooldown": cooldown if cooldown is not None and cooldown >= 0 else num(gun, "statBases/RangedWeapon_Cooldown"),
            "forced_miss": num(verb, "forcedMissRadius"),
            "accuracy": num(t, "statBases/ShootingAccuracyTurret"),
            "aiming": num(t, "statBases/AimingAccuracy"),
            "anti_air": "CIWS" in (verb.get("Class") or "") if verb is not None else False,
        }
        if db.ce:
            ammo_user = comp(gun, "CombatExtended.CompProperties_AmmoUser")
            modes = comp(gun, "CombatExtended.CompProperties_FireModes")
            row["magazine"] = num(ammo_user, "magazineSize")
            row["reload"] = num(ammo_user, "reloadTime")
            row["aimed_burst"] = num(modes, "aimedBurstShotCount")
            row["spread"] = num(gun, "statBases/ShotSpread")
            row["sway"] = num(gun, "statBases/SwayFactor")
            row["ammo"] = ce_ammo(db, ammo_user.findtext("ammoSet") if ammo_user is not None else None,
                                  verb.findtext("defaultProjectile") if verb is not None else None)
        else:
            fuel = comp(t, "CompProperties_Refuelable")
            row["shots"] = num(fuel, "fuelCapacity")
            row["fuel"] = [db.label("ThingDef", li.text) for li in fuel.findall("fuelFilter/thingDefs/li")] if fuel is not None else []
            row["shot"] = vanilla_projectile(db, verb.findtext("defaultProjectile"))
        rows.append(row)
    return rows


def vanilla_projectile(db: DefDB, def_name: str) -> dict:
    p = db.get("ThingDef", def_name)
    if p is None:
        db.problems.append(f"unknown projectile {def_name}")
        return {}
    proj = p.find("projectile")
    ddef = db.get("DamageDef", proj.findtext("damageDef"))
    dmg = num(proj, "damageAmountBase", num(ddef, "defaultDamage"))
    ap = num(proj, "armorPenetrationBase", -1)
    if ap < 0:  # ProjectileProperties.GetArmorPenetration
        ap = 0 if ddef is None or ddef.findtext("armorCategory") is None else (dmg or 0) * 0.015
    return {"damage": dmg, "type": ddef.findtext("label") if ddef is not None else None, "ap": ap,
            "blast": num(proj, "explosionRadius"), "stopping": num(proj, "stoppingPower")}


def ce_ammo(db: DefDB, ammo_set: str | None, fallback: str | None) -> dict:
    s = db.get("AmmoSetDef", ammo_set)
    pairs = [(a.tag, a.text) for a in s.find("ammoTypes")] if s is not None else [(None, fallback)]
    out = []
    for ammo, proj_name in pairs:
        p = db.get("ThingDef", proj_name)
        if p is None:
            db.problems.append(f"unknown CE projectile {proj_name}")
            continue
        proj = p.find("projectile")
        ddef = db.get("DamageDef", proj.findtext("damageDef"))
        second = [f"{num(li, 'amount', 0):g} {db.label('DamageDef', li.findtext('def'))}"
                  for li in proj.findall("secondaryDamage/li")]
        out.append({
            "label": db.label("ThingDef", ammo) if ammo else p.findtext("label"),
            "damage": num(proj, "damageAmountBase", num(ddef, "defaultDamage")),
            "type": ddef.findtext("label") if ddef is not None else None,
            "sharp": num(proj, "armorPenetrationSharp"),
            "blunt": num(proj, "armorPenetrationBlunt"),
            "blast": num(proj, "explosionRadius"),
            "secondary": second,
            "frags": sum(float(f.text) for f in p.findall("comps/li/fragments/*") if f.text) or None,
        })
    return {"set": s.findtext("label") if s is not None else None, "rounds": out}


def research_rows(db: DefDB) -> list[dict]:
    rows = []
    for raw in db.order:
        if raw.tag != "ResearchProjectDef":
            continue
        r = db.resolve_el(raw)
        bench = r.findtext("requiredResearchBuilding")
        rows.append({
            "label": r.findtext("label"),
            "desc": r.findtext("description"),
            "cost": num(r, "baseCost"),
            "tech": r.findtext("techLevel"),
            "prereqs": [db.label("ResearchProjectDef", li.text) for li in r.findall("prerequisites/li")],
            "bench": db.label("ThingDef", bench) if bench else None,
            "facilities": [db.label("ThingDef", li.text) for li in r.findall("requiredResearchFacilities/li")],
        })
    return rows


# ---------------------------------------------------------------- external base defs

def refresh_external() -> None:
    """Copy the vanilla, DLC and Combat Extended defs the mod's defs point at
    (parents, projectiles, ammo, damage types, research, cost items) into EXTERNAL,
    so CI can resolve them without the game."""
    game = ROOT.parents[1]
    steamapps = ROOT.parents[3]
    sources = [("core", d) for d in sorted((game / "Data").glob("*/Defs"))]
    ce_dir = find_ce(steamapps)
    if ce_dir is None:
        sys.exit("Combat Extended isn't installed; can't refresh the CE defs.")
    sources.append(("ce", ce_dir / "Defs"))
    names, defs = {}, {}
    for src, d in sources:
        for path in d.rglob("*.xml"):
            for el in read_defs(path):
                el.set("source", src)
                if el.get("Name"):
                    names.setdefault(el.get("Name"), el)
                dn = el.findtext("defName")
                if dn:
                    defs.setdefault(dn.strip(), el)

    # A def the mod has in only one of vanilla/CE may still be needed from outside in the other.
    mod, mod_names, mod_defs = [], None, None
    for ce in (False, True):
        els = [el for folder in load_folders(ce) for path in (folder / "Defs").rglob("*.xml") for el in read_defs(path)]
        mod += els
        n = {el.get("Name") for el in els if el.get("Name")}
        d = {el.findtext("defName") for el in els if el.findtext("defName")}
        mod_names = n if mod_names is None else mod_names & n
        mod_defs = d if mod_defs is None else mod_defs & d

    wanted, seen, out = [], set(), []

    def refs(el):
        if el.get("ParentName"):
            yield ("name", el.get("ParentName"))
        for tag in ("defaultProjectile", "ammoSet", "damageDef", "def", "turretGunDef", "requiredResearchBuilding"):
            for x in el.iter(tag):
                if x.text:
                    yield ("def", x.text.strip())
        for path in ("researchPrerequisites/li", "prerequisites/li", "requiredResearchFacilities/li",
                     "fuelFilter/thingDefs/li", "comps/li/fuelFilter/thingDefs/li"):
            for x in el.findall(path):
                if x.text:
                    yield ("def", x.text.strip())
        for c in el.findall("costList/*") + el.findall("ammoTypes/*"):
            yield ("def", c.tag)
            if c.text and c.text.strip() and not c.text.strip()[0].isdigit():
                yield ("def", c.text.strip())

    for el in mod:
        wanted += refs(el)
    while wanted:
        key = wanted.pop()
        if key in seen:
            continue
        seen.add(key)
        kind, n = key
        if (kind == "name" and n in mod_names) or (kind == "def" and n in mod_defs):
            continue
        el = (names if kind == "name" else defs).get(n)
        if el is None:
            continue
        out.append(el)
        if el.get("ParentName"):
            wanted.append(("name", el.get("ParentName")))
        # Only follow what a projectile, ammo set or parent needs, not whole buildings' worth of refs.
        if el.tag.endswith("AmmoSetDef") or el.find("projectile") is not None or el.get("Abstract"):
            wanted += refs(el)

    root = etree.Element("Defs")
    root.append(etree.Comment(" Copied from RimWorld and Combat Extended by: python site/build.py (refresh-external). Don't edit. "))
    for el in sorted({id(e): e for e in out}.values(), key=lambda e: (e.get("source"), e.tag, e.get("Name") or "", e.findtext("defName") or "")):
        root.append(copy.deepcopy(el))
    etree.ElementTree(root).write(str(EXTERNAL), encoding="utf-8", xml_declaration=True, pretty_print=True)
    print(f"wrote {len(out)} defs to {EXTERNAL.relative_to(ROOT)}")


def find_ce(steamapps: Path) -> Path | None:
    candidates = []
    for about in list((steamapps / "common" / "RimWorld" / "Mods").glob("*/About/About.xml")) + \
            list((steamapps / "workshop" / "content" / "294100").glob("*/About/About.xml")):
        try:
            text = about.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        m = re.search(r"<packageId>\s*([^<\s]+)\s*</packageId>", text, re.I)
        if m and m.group(1).lower() == CE_PACKAGE_ID:
            candidates.append(about.parent.parent)
    candidates.sort(key=lambda p: p.name != CE_WORKSHOP_ID)
    return candidates[0] if candidates else None


# ---------------------------------------------------------------- page

def fmt_num(v, digits: int = 2) -> str:
    if v is None:
        return "—"
    s = f"{v:,.{digits}f}".rstrip("0").rstrip(".") if digits else f"{v:,.0f}"
    return s.replace("-", "−")


def fmt_pct(v) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


class Helpers:
    """Functions content/index.md can call. Each returns a placeholder that is
    swapped for its HTML after Markdown runs, so Markdown never touches tables."""

    def __init__(self, env, vanilla: list[dict], ce: list[dict], research: list[dict], problems: list[str]):
        self.env, self.vanilla, self.ce, self.research, self.problems = env, vanilla, ce, research, problems
        self.blocks: list[str] = []

    def _block(self, html: str) -> str:
        self.blocks.append(html)
        return f"\n\nABTBLOCK{len(self.blocks) - 1}\n\n"

    def _macro(self, name, **kw):
        return self._block(str(getattr(self.env.get_template("macros.html").module, name)(**kw)))

    def turret_tables(self, tier: str):
        v = [r for r in self.vanilla if r["tier"] == tier]
        c = [r for r in self.ce if r["tier"] == tier]
        if not v and not c:
            self.problems.append(f"no turrets in tier {tier}")
        ce_only = [r["label"] for r in c if r["def"] not in {x["def"] for x in v}]
        return self._macro("turret_tables", vanilla=v, ce=c, ce_only=ce_only)

    def research_table(self):
        return self._macro("research_table", rows=self.research)


def build(out: Path, strict: bool) -> int:
    problems: list[str] = []
    vdb, cdb = DefDB(False, problems), DefDB(True, problems)
    vanilla, ce = turret_rows(vdb), turret_rows(cdb)
    research = research_rows(vdb)

    env = Environment(loader=FileSystemLoader(SITE / "templates"), autoescape=select_autoescape(["html"]),
                      undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    env.filters["num"] = fmt_num
    env.filters["pct"] = fmt_pct
    env.filters["cap"] = lambda t: t[:1].upper() + t[1:] if t else t
    env.globals["tier_titles"] = TIER_TITLES

    helpers = Helpers(env, vanilla, ce, research, problems)
    text = (SITE / "content" / "index.md").read_text(encoding="utf-8")
    text = env.from_string(text).render(**{n: getattr(helpers, n) for n in dir(helpers) if not n.startswith("_")})
    md = markdown.Markdown(extensions=["tables", "toc", "attr_list", "md_in_html"])
    html = md.convert(text)
    for i, block in enumerate(helpers.blocks):
        html = html.replace(f"<p>ABTBLOCK{i}</p>", block)
    toc = [{"id": t["id"], "name": t["name"]} for t in md.toc_tokens if t["level"] == 2]

    # Textures: every icon the tables use, plus the Workshop preview.
    icons = {r["icon"] for r in vanilla + ce if r["icon"]}
    if out.exists():
        if not (out / BUILD_MARKER).is_file():
            sys.exit(f"{out} exists and wasn't made by this script; not deleting it.")
        shutil.rmtree(out)
    shutil.copytree(SITE / "static", out / "assets")
    (out / BUILD_MARKER).write_text("")
    for icon in sorted(icons):
        src = ROOT / "Textures" / f"{icon}.png"
        if not src.is_file():
            problems.append(f"missing texture Textures/{icon}.png")
            continue
        dst = out / "assets" / "tex" / f"{icon}.png"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    shutil.copy2(ROOT / "About" / "Preview.png", out / "assets" / "preview.png")

    about = etree.parse(str(ROOT / "About" / "About.xml"), PARSER).getroot()
    workshop_id = (ROOT / "About" / "PublishedFileId.txt").read_text(encoding="utf-8").strip()
    page = env.get_template("page.html").render(
        content=Markup(html), toc=toc, title=about.findtext("name"),
        authors=about.findtext("author"), favicon=f"assets/tex/{sorted(icons)[0]}.png" if icons else None,
        workshop=f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}",
        github=GITHUB, site_url=SITE_URL,
    )
    (out / "index.html").write_text(page, encoding="utf-8")

    print(f"built {out} ({len(vanilla)} vanilla turrets, {len(ce)} CE turrets)")
    for p in sorted(set(problems)):
        print(f"  problem: {p}")
    return 1 if strict and problems else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=SITE / "_build")
    ap.add_argument("--strict", action="store_true", help="exit non-zero on any problem")
    ap.add_argument("--serve", action="store_true", help="serve the result on http://localhost:8000")
    ap.add_argument("--refresh-external", action="store_true", help="re-copy vanilla/CE base defs from the installed game")
    args = ap.parse_args()
    if args.refresh_external:
        refresh_external()
        return
    code = build(args.out.resolve(), args.strict)
    if args.serve:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(args.out.resolve()))
        print("serving on http://localhost:8000 (Ctrl+C to stop)")
        http.server.ThreadingHTTPServer(("127.0.0.1", 8000), handler).serve_forever()
    sys.exit(code)


if __name__ == "__main__":
    main()
