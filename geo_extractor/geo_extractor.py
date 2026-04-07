from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
import os
from .geo_extractor_dialog import GeoExtractorDialog

class GeoExtractor:
    def __init__(self, iface):
        self.iface = iface

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), 'icon.png')
        self.action = QAction(
            QIcon(icon_path),
            "GeoExtractor",
            self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu("GeoExtractor", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        self.iface.removePluginMenu("GeoExtractor", self.action)
        self.iface.removeToolBarIcon(self.action)

    def run(self):
        self.dlg = GeoExtractorDialog(self.iface)
        self.dlg.show()
        self.dlg.exec()   # ✅ QGIS 4 / PyQt6