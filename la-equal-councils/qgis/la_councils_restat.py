"""
QGIS Processing tool: Recalculate districts after editing.

After you change `district_id` on the census blocks layer (and save the
edits), this rebuilds the districts layer and its stats with redistrict.py
restat, then refreshes the layers on the map.

Install: Processing Toolbox > Python icon > Add Script to Toolbox... and pick
this file. It appears under Scripts > LA equal councils.
"""
import os
import shutil
import subprocess
from pathlib import Path

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputFile,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsSettings,
)
from qgis.PyQt.QtCore import QCoreApplication

SETTINGS = "la_equal_councils"
BALANCE = [("", "Same as the build"), ("pop", "Residents"), ("vap", "Adults (18+)"),
           ("households", "Households"), ("housing_units", "Housing units")]
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


def gpkg_of(layer):
    return Path(layer.source().split("|")[0]) if layer else None


class RecalculateCouncils(QgsProcessingAlgorithm):
    UNITS, PROJECT, PYTHON, BALANCE, GPKG = "UNITS", "PROJECT", "PYTHON", "BALANCE", "GPKG"

    def tr(self, s):
        return QCoreApplication.translate("RecalculateCouncils", s)

    def createInstance(self):
        return RecalculateCouncils()

    def name(self):
        return "recalculate_councils"

    def displayName(self):
        return self.tr("Recalculate districts after editing")

    def group(self):
        return self.tr("LA equal councils")

    def groupId(self):
        return "la_equal_councils"

    def shortHelpString(self):
        return self.tr(
            "Pick the census blocks layer you edited (the 'units' layer from a build's GeoPackage). "
            "Save your edits first. The districts layer, GeoJSON, CSV and summary in the same folder are rebuilt "
            "from the blocks' district_id, and n_parts flags any district you split into pieces.")

    def initAlgorithm(self, config=None):
        s = QgsSettings()
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.UNITS, self.tr("Census blocks layer you edited"), [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterEnum(
            self.BALANCE, self.tr("Measure deviation by"), [b[1] for b in BALANCE], defaultValue=0))
        self.addParameter(QgsProcessingParameterFile(
            self.PROJECT, self.tr("Project folder (contains redistrict.py)"),
            behavior=QgsProcessingParameterFile.Folder, defaultValue=s.value(f"{SETTINGS}/project", "")))
        self.addParameter(QgsProcessingParameterFile(
            self.PYTHON, self.tr("Python interpreter (blank = project .venv)"), optional=True,
            defaultValue=s.value(f"{SETTINGS}/python", "") or None))
        self.addOutput(QgsProcessingOutputFile(self.GPKG, self.tr("GeoPackage")))

    def checkParameterValues(self, parameters, context):
        layer = self.parameterAsVectorLayer(parameters, self.UNITS, context)
        if layer is not None:
            if layer.isModified():
                return False, self.tr("Save the edits on the blocks layer first (Layer > Save Layer Edits).")
            if "district_id" not in layer.fields().names():
                return False, self.tr("That layer has no district_id field. Pick the 'units' layer from a build.")
            if not str(gpkg_of(layer)).lower().endswith(".gpkg"):
                return False, self.tr("Pick the units layer inside a build's GeoPackage.")
        return super().checkParameterValues(parameters, context)

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.UNITS, context)
        gpkg = gpkg_of(layer)
        project = Path(self.parameterAsFile(parameters, self.PROJECT, context))
        script = project / "redistrict.py"
        if not script.exists():
            raise QgsProcessingException(f"No redistrict.py in {project}")
        python = find_python(project, self.parameterAsFile(parameters, self.PYTHON, context))
        if not python:
            raise QgsProcessingException("No Python with geopandas found; see the Redraw tool's help.")
        cmd = [python, str(script), "restat", str(gpkg)]
        b = BALANCE[self.parameterAsEnum(parameters, self.BALANCE, context)][0]
        if b:
            cmd += ["--balance-on", b]
        feedback.pushCommandInfo(" ".join(cmd))
        env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
        r = subprocess.run(cmd, cwd=str(project), env=env, capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in (r.stdout + r.stderr).splitlines():
            feedback.pushConsoleInfo(line)
        if r.returncode != 0:
            raise QgsProcessingException("redistrict.py restat failed; see the log above.")
        self._gpkg = gpkg
        return {self.GPKG: str(gpkg)}

    def postProcessAlgorithm(self, context, feedback):
        gpkg = getattr(self, "_gpkg", None)
        if gpkg is None:
            return {}
        project = context.project() or QgsProject.instance()
        n = 0
        for lyr in project.mapLayers().values():
            if hasattr(lyr, "dataProvider") and gpkg_of(lyr) == gpkg:
                lyr.dataProvider().reloadData()
                lyr.triggerRepaint()
                n += 1
        feedback.pushInfo(f"Refreshed {n} layer(s) from {gpkg.name}")
        return {}
