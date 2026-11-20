# Turrets For All Post-Medieval Tech Levels

The fourth mod in the All-Bamba series for RimWorld: machine gun, EMP, rocket and artillery turrets for the Industrial, Spacer and Ultra tech levels, with Combat Extended, Muzzle Flash and Turret Pipeline support.

- Steam Workshop: https://steamcommunity.com/sharedfiles/filedetails/?id=3800286808
- Site with every turret's stats: https://databobek.github.io/AllBambaTurrets/

By Bambaryla (design), Haderoth (art) and Samael Gray (mod structure, Muzzle Flash and CE integration).

## Layout

| Path | What |
|---|---|
| `1.7/` | Current defs. `Vanilla/` loads without Combat Extended, `CE/` with it; `MuzzleFlash/` and `TurretPipeline/` load with those mods. |
| `1.6/` | The 1.6 version, kept as it was. |
| `Textures/`, `About/` | Art and Workshop metadata. |
| `site/` | The player site (not part of the mod). |

## The site

`site/build.py` reads the 1.7 defs the way RimWorld does (load folders, inheritance) once without and once with Combat Extended, and renders `site/content/index.md` with stat tables for every turret. Change the text in `index.md`; the numbers follow the defs.

```
pip install -r site/requirements.txt
python site/build.py --serve      # http://localhost:8000
```

Some defs inherit from vanilla or Combat Extended (`BaseBullet`, CE ammo sets and rounds). `site/external_defs.xml` holds copies of those. After a game or CE update, refresh it on a machine with both installed and commit it:

```
python site/build.py --refresh-external
```

Pushing to `main` rebuilds and publishes the site through `.github/workflows/pages.yml`. The build runs with `--strict`, so a missing def or texture fails the run and the live site stays as it was. One-time setup on GitHub: **Settings > Pages > Source: GitHub Actions**.

`_PublisherPlus.xml` keeps `site/`, `.github/` and other dev files out of the Workshop upload.
