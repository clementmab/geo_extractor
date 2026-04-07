from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import QDialog, QFileDialog, QMessageBox
from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProject, QgsVectorLayer,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem
)
import os
import json
import requests
import processing

FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), 'geo_extractor_dialog_base.ui')
)

OSM_TYPES = {
    "Routes":                ("way",  "highway"),
    "Bâtiments":             ("way",  "building"),
    "Hydrographie (lignes)": ("way",  "waterway"),
    "Hydrographie (areas)":  ("way",  "natural=water"),
    "Végétation / Landuse":  ("way",  "landuse"),
    "Forêts":                ("way",  "natural=wood"),
    "Lieux / Points":        ("node", "place"),
    "Écoles":                ("node", "amenity=school"),
    "Hôpitaux":              ("node", "amenity=hospital"),
    "Pharmacies":            ("node", "amenity=pharmacy"),
}

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]

class GeoExtractorDialog(QDialog, FORM_CLASS):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setupUi(self)

        self.comboBox.clear()
        for label in OSM_TYPES.keys():
            self.comboBox.addItem(label)

        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        self.outputFolder.setText(desktop)

        self.progressBar.setValue(0)
        self.progressBar.setVisible(False)

        self.browseButton.clicked.connect(self.select_shapefile)
        self.folderButton.clicked.connect(self.select_folder)
        self.extractButton.clicked.connect(self.extract_osm)

    # ── UI ───────────────────────────────────────────────────────────
    def select_shapefile(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choisir un shapefile", "", "Shapefiles (*.shp)"
        )
        if path:
            self.inputShapefile.setText(path)

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier de sauvegarde"
        )
        if folder:
            self.outputFolder.setText(folder)

    def set_progress(self, value, message=""):
        self.progressBar.setVisible(True)
        self.progressBar.setValue(value)
        if message:
            self.progressBar.setFormat(f"{message}  %p%")
        # Rafraîchit l'écran pour éviter que QGIS gèle
        QCoreApplication.processEvents()

    def set_ui_enabled(self, enabled):
        self.extractButton.setEnabled(enabled)
        self.browseButton.setEnabled(enabled)
        self.folderButton.setEnabled(enabled)
        self.comboBox.setEnabled(enabled)

    # ── Reprojection WGS84 ───────────────────────────────────────────
    def get_wgs84_extent(self, layer):
        crs_src  = layer.crs()
        crs_dest = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(
            crs_src, crs_dest, QgsProject.instance()
        )
        return transform.transformBoundingBox(layer.extent())

    # ── Découpage en blocs ───────────────────────────────────────────
    def split_bbox(self, extent, step=0.2):
        xmin, xmax = extent.xMinimum(), extent.xMaximum()
        ymin, ymax = extent.yMinimum(), extent.yMaximum()
        bboxes = []
        x = xmin
        while x < xmax:
            y = ymin
            while y < ymax:
                bboxes.append((
                    round(y, 6),
                    round(x, 6),
                    round(min(y + step, ymax), 6),
                    round(min(x + step, xmax), 6),
                ))
                y += step
            x += step
        return bboxes

    # ── Requête Overpass ─────────────────────────────────────────────
    def build_query(self, osm_type, tag_value, bbox):
        if "=" in tag_value:
            key, val = tag_value.split("=")
            filter_str = f'["{key}"="{val}"]'
        else:
            filter_str = f'["{tag_value}"]'
        ymin, xmin, ymax, xmax = bbox
        return (
            f'[out:json][timeout:25];\n'
            f'{osm_type}{filter_str}({ymin},{xmin},{ymax},{xmax});\n'
            f'out geom;\n'
        )

    def fetch_osm(self, query):
        for server in OVERPASS_SERVERS:
            try:
                r = requests.post(
                    server,
                    data={"data": query},
                    timeout=30
                )
                if r.status_code == 200:
                    data = r.json()
                    if "elements" in data:
                        return data
            except Exception:
                continue
        return None

    # ── Conversion GeoJSON ───────────────────────────────────────────
    def osm_json_to_geojson(self, osm_data, seen_ids):
        features = []
        for element in osm_data.get("elements", []):

            uid = f"{element['type']}-{element['id']}"
            if uid in seen_ids:
                continue
            seen_ids.add(uid)

            geom  = None
            props = element.get("tags", {})

            if element.get("type") == "way" and "geometry" in element:
                coords = [
                    [pt["lon"], pt["lat"]]
                    for pt in element["geometry"]
                ]
                if len(coords) >= 4 and coords[0] == coords[-1]:
                    geom = {"type": "Polygon",    "coordinates": [coords]}
                else:
                    geom = {"type": "LineString", "coordinates": coords}

            elif element.get("type") == "node" and "lat" in element:
                geom = {
                    "type": "Point",
                    "coordinates": [element["lon"], element["lat"]]
                }

            if geom:
                features.append({
                    "type":       "Feature",
                    "geometry":   geom,
                    "properties": props,
                })
        return features

    # ── EXTRACTION PRINCIPALE ────────────────────────────────────────
    def extract_osm(self):

        # --- Vérifications ---
        path = self.inputShapefile.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "Erreur", "Choisis un shapefile valide !")
            return

        out_folder = self.outputFolder.text().strip()
        if not os.path.isdir(out_folder):
            QMessageBox.warning(self, "Erreur", "Dossier de sauvegarde invalide !")
            return

        layer = QgsVectorLayer(path, "Zone", "ogr")
        if not layer.isValid():
            QMessageBox.warning(self, "Erreur", "Impossible de lire le shapefile !")
            return

        # --- Reprojection ---
        extent   = self.get_wgs84_extent(layer)
        nb_blocs = len(self.split_bbox(extent, step=0.2))

        # ✅ CORRECTION PyQt6 : StandardButton à la place de Yes/No
        if nb_blocs > 10:
            boite = QMessageBox(self)
            boite.setWindowTitle("Grande zone détectée")
            boite.setText(
                f"Ta zone nécessite {nb_blocs} requêtes API.\n"
                f"Cela peut prendre plusieurs minutes.\n\n"
                f"Continuer ?"
            )
            boite.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            boite.setDefaultButton(QMessageBox.StandardButton.No)
            if boite.exec() == QMessageBox.StandardButton.No:
                return

        label            = self.comboBox.currentText()
        osm_type, tag_value = OSM_TYPES[label]
        bboxes           = self.split_bbox(extent, step=0.2)
        total            = len(bboxes)

        self.set_ui_enabled(False)
        all_features = []
        seen_ids     = set()
        echecs       = 0

        try:
            # --- Boucle de téléchargement ---
            for i, bbox in enumerate(bboxes):
                pct = int((i / total) * 70)
                self.set_progress(pct, f"Bloc {i+1}/{total}")

                query = self.build_query(osm_type, tag_value, bbox)
                data  = self.fetch_osm(query)

                if data:
                    features = self.osm_json_to_geojson(data, seen_ids)
                    all_features.extend(features)
                else:
                    echecs += 1

            if not all_features:
                QMessageBox.warning(
                    self, "Aucune donnée",
                    f"Aucun résultat pour '{label}'.\n"
                    f"Vérifie que ton shapefile est en WGS84 (EPSG:4326).\n"
                    f"Requêtes échouées : {echecs}/{total}"
                )
                return

            # --- Sauvegarde GeoJSON brut ---
            self.set_progress(75, "Sauvegarde...")
            safe_name    = label.replace(" ", "_").replace("/", "_")
            out_geojson  = os.path.join(out_folder, f"OSM_{safe_name}.geojson")
            geojson_data = {"type": "FeatureCollection", "features": all_features}

            with open(out_geojson, "w", encoding="utf-8") as f:
                json.dump(geojson_data, f, ensure_ascii=False)

            osm_layer = QgsVectorLayer(out_geojson, f"OSM {label}", "ogr")
            if not osm_layer.isValid():
                QMessageBox.warning(self, "Erreur", "GeoJSON illisible après sauvegarde.")
                return

            # --- Clip → GeoPackage ---
            self.set_progress(85, "Découpage...")
            out_clip = os.path.join(out_folder, f"CLIP_{safe_name}.gpkg")

            processing.run("native:clip", {
                "INPUT":   osm_layer,
                "OVERLAY": layer,
                "OUTPUT":  out_clip,
            })

            # --- Chargement résultat ---
            self.set_progress(95, "Chargement...")
            clipped = QgsVectorLayer(out_clip, f"{label} — clippé", "ogr")

            if not clipped.isValid() or clipped.featureCount() == 0:
                QMessageBox.warning(
                    self, "Clip vide",
                    "Le découpage n'a produit aucun objet.\n"
                    "La couche brute est chargée à la place."
                )
                QgsProject.instance().addMapLayer(osm_layer)
            else:
                QgsProject.instance().addMapLayer(clipped)

            self.set_progress(100, "Terminé !")

            QMessageBox.information(
                self, "Succès",
                f"Extraction terminée !\n\n"
                f"• Objets récupérés : {len(all_features)}\n"
                f"• Requêtes échouées : {echecs}/{total}\n\n"
                f"Fichiers dans :\n{out_folder}\n\n"
                f"  → OSM_{safe_name}.geojson\n"
                f"  → CLIP_{safe_name}.gpkg"
            )

        except Exception as e:
            QMessageBox.critical(
                self, "Erreur inattendue",
                f"Une erreur s'est produite :\n{str(e)}"
            )

        finally:
            self.set_ui_enabled(True)