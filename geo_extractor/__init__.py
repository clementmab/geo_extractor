def classFactory(iface):
    from .geo_extractor import GeoExtractor
    return GeoExtractor(iface)