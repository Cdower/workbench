"""
QGIS Processing tool: Redraw LA council districts.

A form for redistrict.py. Runs it with a Python that has geopandas (your
project's .venv by default), streams its log into the Processing dialog and
adds the result to the map as a layer group.

Install: Processing Toolbox > Python icon > Add Script to Toolbox... and pick
this file. It appears under Scripts > LA equal councils.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputFile,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsSettings,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

SETTINGS = "la_equal_councils"
BALANCE = [("", "From plan file"), ("pop", "Residents"), ("vap", "Adults (18+)"),
           ("households", "Households"), ("housing_units", "Housing units")]
COVERAGE = [("", "From plan file"), ("all", "The whole city"), ("councils", "Certified councils only")]
KEEP = [("", "From plan file"), ("none", "Nothing (default)"), ("councils", "Today's councils"),
        ("neighborhoods", "LA Times neighborhoods")]
UNITS = [("", "From plan file"), ("blocks", "Census blocks (precise, needs fetch)"), ("tracts", "Census tracts (quick, rough)")]
BARRIERS = [("freeways", "Freeways"), ("rail", "Railroads and rail transit"), ("la_river", "Los Angeles River"),
            ("mulholland", "Mulholland ridge (Santa Monica Mountains)")]
TRISTATE = ["From plan file", "Yes", "No"]
# QGIS sets these for its own Python; they break an outside Python's GDAL/PROJ.
SCRUB_ENV = ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "GDAL_DATA", "GDAL_DRIVER_PATH",
             "PROJ_LIB", "PROJ_DATA", "GDAL_FILENAME_IS_UTF8", "QT_PLUGIN_PATH")


def find_python(project: Path, given: str):
    if given:
        return given
    for c in (project / ".venv" / "bin" / "python", project / ".venv" / "Scripts" / "python.exe",
              project / "venv" / "bin" / "python", project / "venv" / "Scripts" / "python.exe"):
        if c.exists():
            return str(c)
    for n in ("python3", "python"):
        p = shutil.which(n)
        if p and "qgis" not in p.lower():
            return p
    return None


def clean_env():
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run(cmd, cwd, feedback, stages=()):
    """Run cmd, stream its output to the Processing log, honour Cancel."""
    feedback.pushCommandInfo(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, cwd=cwd, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, creationflags=flags)
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-15:]
        feedback.pushConsoleInfo(line)
        for marker, pct in stages:
            if line.lstrip().startswith(marker):
                feedback.setProgress(pct)
        if feedback.isCanceled():
            proc.terminate()
            raise QgsProcessingException("Canceled")
    proc.stdout.close()
    if proc.wait() != 0:
        raise QgsProcessingException("redistrict.py failed:\n" + "\n".join(tail))


def check_python(python, project, feedback):
    if not python:
        raise QgsProcessingException(
            "No Python with geopandas found. In the project folder run:\n"
            "  python3 -m venv .venv\n  .venv/bin/pip install -r requirements.txt\n"
            "or pick an interpreter under 'Python interpreter'.")
    r = subprocess.run([python, "-c", "import geopandas, networkx, scipy, pyogrio"], env=clean_env(),
                       capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise QgsProcessingException(
            f"{python} can't import geopandas/networkx/scipy/pyogrio.\n"
            f"Install them with: \"{python}\" -m pip install -r \"{project / 'requirements.txt'}\"\n\n{r.stderr[-800:]}")
    feedback.pushInfo(f"Python: {python}")


class RedrawCouncils(QgsProcessingAlgorithm):
    PROJECT, PYTHON, PLAN = "PROJECT", "PYTHON", "PLAN"
    DISTRICTS, BALANCE, TOLERANCE, COVERAGE, UNITS = "DISTRICTS", "BALANCE", "TOLERANCE", "COVERAGE", "UNITS"
    KEEP, STICK, BARRIERS, BARRIER_LINES, STRENGTH = "KEEP", "STICK", "BARRIERS", "BARRIER_LINES", "STRENGTH"
    SMALL, SMOOTH, ROUTING = "KEEP_SMALL_WHOLE", "SMOOTH", "BARRIER_ROUTING"
    SEED, FETCH, LOAD, OUTPUT, GPKG = "SEED", "FETCH", "LOAD", "OUTPUT", "GPKG"

    def tr(self, s):
        return QCoreApplication.translate("RedrawCouncils", s)

    def createInstance(self):
        return RedrawCouncils()

    def name(self):
        return "redraw_councils"

    def displayName(self):
        return self.tr("Redraw council districts")

    def group(self):
        return self.tr("LA equal councils")

    def groupId(self):
        return "la_equal_councils"

    def shortHelpString(self):
        return self.tr(
            "Redraws Los Angeles into districts that each hold an equal share of a 2020 Census count, "
            "by running redistrict.py.\n\n"
            "Anything left on 'From plan file' (or blank) comes from the plan file, or from the defaults "
            "if there is none. The defaults follow California's FAIR MAPS Act: 99 districts of equal residents "
            "within ±1%, the whole city, and lines drawn along "
            "freeways, rail, the LA River and the Mulholland ridge where they can be. Today's council lines "
            "aren't a constraint: the new districts replace them.\n\n"
            "Tolerance: the largest allowed difference from an equal share, in percent. The slack lets lines "
            "follow barriers; about 0.5% is the tightest census blocks allow.\n"
            "Keep together (optional): today's councils or LA Times neighborhoods. With it, a group smaller than "
            "one district is only split if the tolerance can't be met otherwise, and Stickiness sets how hard to "
            "keep bigger ones in few pieces.\n"
            "Barriers: lines residents recognize, which district lines follow where they can. Leave blank for the "
            "plan's list. 'Your own barrier lines' adds any line layer you draw.\n"
            "Route around barriers: also measure distance along streets so crossings cost more (slower, stronger).\n\n"
            "Python interpreter: needs geopandas, networkx and scipy. Leave blank to use .venv in the project folder.")

    def initAlgorithm(self, config=None):
        s = QgsSettings()
        P = self.addParameter
        P(QgsProcessingParameterFile(self.PROJECT, self.tr("Project folder (contains redistrict.py)"),
                                     behavior=QgsProcessingParameterFile.Folder,
                                     defaultValue=s.value(f"{SETTINGS}/project", "")))
        P(QgsProcessingParameterFile(self.PLAN, self.tr("Plan file (.toml)"), extension="toml", optional=True))
        P(QgsProcessingParameterNumber(self.DISTRICTS, self.tr("Number of districts"),
                                       QgsProcessingParameterNumber.Integer, optional=True, minValue=2, maxValue=2000))
        P(QgsProcessingParameterEnum(self.BALANCE, self.tr("Give every district an equal share of"),
                                     [b[1] for b in BALANCE], defaultValue=0))
        P(QgsProcessingParameterNumber(self.TOLERANCE, self.tr("Tolerance, ± percent"),
                                       QgsProcessingParameterNumber.Double, optional=True, minValue=0.05, maxValue=40))
        P(QgsProcessingParameterEnum(self.COVERAGE, self.tr("Area"), [c[1] for c in COVERAGE], defaultValue=0))
        P(QgsProcessingParameterEnum(self.KEEP, self.tr("Keep together"), [k[1] for k in KEEP], defaultValue=0))
        P(QgsProcessingParameterEnum(self.SMALL, self.tr("Keep groups smaller than a district whole"), TRISTATE,
                                     defaultValue=0))
        P(QgsProcessingParameterNumber(self.STICK, self.tr("Stickiness (0 = ignore, 3 = strong)"),
                                       QgsProcessingParameterNumber.Double, optional=True, minValue=0, maxValue=20))
        P(QgsProcessingParameterEnum(self.BARRIERS, self.tr("Barriers (blank = from plan file)"), [b[1] for b in BARRIERS],
                                     allowMultiple=True, optional=True))
        P(QgsProcessingParameterEnum(self.SMOOTH, self.tr("Slide lines onto barriers"), TRISTATE,
                                     defaultValue=0))
        P(QgsProcessingParameterEnum(self.ROUTING, self.tr("Route around barriers"), TRISTATE, defaultValue=0))
        P(QgsProcessingParameterVectorLayer(self.BARRIER_LINES, self.tr("Your own barrier lines"),
                                            [QgsProcessing.TypeVectorLine, QgsProcessing.TypeVectorPolygon], optional=True))
        P(QgsProcessingParameterNumber(self.STRENGTH, self.tr("Routing: a crossing costs N× the distance"),
                                       QgsProcessingParameterNumber.Double, optional=True, minValue=1, maxValue=50))
        P(QgsProcessingParameterEnum(self.UNITS, self.tr("Building blocks"), [u[1] for u in UNITS], defaultValue=0))
        P(QgsProcessingParameterNumber(self.SEED, self.tr("Random seed (when districts ≠ number of councils)"),
                                       QgsProcessingParameterNumber.Integer, optional=True, minValue=0))
        P(QgsProcessingParameterFile(self.PYTHON, self.tr("Python interpreter (blank = project .venv)"),
                                     optional=True, defaultValue=s.value(f"{SETTINGS}/python", "") or None))
        P(QgsProcessingParameterBoolean(self.FETCH, self.tr("Download missing data first"), defaultValue=False))
        P(QgsProcessingParameterBoolean(self.LOAD, self.tr("Add the result to the map"), defaultValue=True))
        P(QgsProcessingParameterFolderDestination(self.OUTPUT, self.tr("Output folder")))
        self.addOutput(QgsProcessingOutputFile(self.GPKG, self.tr("GeoPackage")))

    def processAlgorithm(self, parameters, context, feedback):
        project = Path(self.parameterAsFile(parameters, self.PROJECT, context))
        script = project / "redistrict.py"
        if not script.exists():
            raise QgsProcessingException(f"No redistrict.py in {project}")
        python = find_python(project, self.parameterAsFile(parameters, self.PYTHON, context))
        check_python(python, project, feedback)
        s = QgsSettings()
        s.setValue(f"{SETTINGS}/project", str(project))
        if self.parameterAsFile(parameters, self.PYTHON, context):
            s.setValue(f"{SETTINGS}/python", python)

        if self.parameterAsBool(parameters, self.FETCH, context):
            feedback.pushInfo("Downloading inputs (the first time is about 600 MB) ...")
            run([python, str(script), "fetch"], str(project), feedback)

        out = Path(self.parameterAsString(parameters, self.OUTPUT, context))
        cmd = [python, str(script), "build", "--out", str(out)]
        plan = self.parameterAsFile(parameters, self.PLAN, context)
        if plan:
            cmd += ["--config", plan]

        def num(key, flag, scale=1.0, cast=float):
            if parameters.get(key) not in (None, ""):
                cmd.extend([flag, str(cast(self.parameterAsDouble(parameters, key, context) * scale))])

        def choice(key, options, flag):
            v = options[self.parameterAsEnum(parameters, key, context)][0]
            if v:
                cmd.extend([flag, v])

        num(self.DISTRICTS, "--districts", cast=lambda x: int(round(x)))
        choice(self.BALANCE, BALANCE, "--balance-on")
        num(self.TOLERANCE, "--tolerance", scale=0.01)
        choice(self.COVERAGE, COVERAGE, "--coverage")
        choice(self.KEEP, KEEP, "--keep-whole")
        num(self.STICK, "--stick")
        num(self.STRENGTH, "--barrier-strength")
        choice(self.UNITS, UNITS, "--units")
        num(self.SEED, "--random-seed", cast=lambda x: int(round(x)))
        for key, flag in [(self.SMALL, "keep-small-whole"), (self.SMOOTH, "smooth"), (self.ROUTING, "barrier-routing")]:
            v = self.parameterAsEnum(parameters, key, context)
            if v:
                cmd.append(f"--{flag}" if v == 1 else f"--no-{flag}")
        barriers = [BARRIERS[i][0] for i in self.parameterAsEnums(parameters, self.BARRIERS, context)]
        if barriers:
            cmd += ["--barriers", ",".join(barriers)]
        if parameters.get(self.BARRIER_LINES):
            path = self.parameterAsCompatibleSourceLayerPath(
                parameters, self.BARRIER_LINES, context, ["gpkg", "shp", "geojson"], "gpkg", feedback)
            if path:
                cmd += ["--add-barriers", path.split("|")[0]]

        stages = [("lloyd 0", 10), ("lloyd 5", 25), ("balanced assignment", 40), ("polish pass", 50),
                  ("chain", 65), ("splitting", 75), ("smoothing", 90), ("wrote", 98)]
        run(cmd, str(project), feedback, stages)
        gpkg = out / "la_nc_equal_pop.gpkg"
        if not gpkg.exists():
            raise QgsProcessingException(f"{gpkg} was not written")
        summary = json.loads((out / "summary.json").read_text())
        feedback.pushInfo(
            f"{summary['districts']} districts, {summary['balance_on']} target {summary['target']:,}, "
            f"largest deviation ±{summary['max_dev_pct']:.2f}%")
        self._result = (gpkg, summary) if self.parameterAsBool(parameters, self.LOAD, context) else None
        return {self.OUTPUT: str(out), self.GPKG: str(gpkg)}

    def postProcessAlgorithm(self, context, feedback):
        res = getattr(self, "_result", None)
        if not res:
            return {}
        gpkg, s = res
        project = context.project() or QgsProject.instance()
        label = f"{s['districts']} districts · {s['balance_on']} ±{s['max_dev_pct']:.2f}% · {gpkg.parent.name}"
        root = project.layerTreeRoot()
        group = root.insertGroup(0, label)
        for layer_name, title, visible in [("districts_equal_pop", "Redrawn districts", True),
                                           ("nc_current", "Today's councils", True),
                                           ("units", "Census blocks (edit district_id here)", False)]:
            lyr = QgsVectorLayer(f"{gpkg}|layername={layer_name}", title, "ogr")
            if not lyr.isValid():
                feedback.reportError(f"Could not load {layer_name} from {gpkg}")
                continue
            project.addMapLayer(lyr, False)
            node = group.addLayer(lyr)
            node.setItemVisibilityChecked(visible)
            feedback.pushInfo(f"Added layer: {title}")
        return {}
