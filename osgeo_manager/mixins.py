from contextlib import contextmanager

from .constants import STYLES_TABLE
from .utils import is_postgres_source
try:
    from osgeo import ogr
except ImportError:
    import ogr


class OSGEOManagerMixin(object):
    @staticmethod
    def build_connection_string(DB_Name,
                                DB_Schema,
                                DB_user,
                                DB_Pass,
                                DB_Port=5432, DB_server='localhost'):
        connectionString = "PG: host={} port={} dbname={} active_schema={} user={} password={}"\
            .format(DB_server, DB_Port, DB_Name, DB_Schema, DB_user, DB_Pass)
        return connectionString

    @staticmethod
    @contextmanager
    def open_source(source_path, update_enabled=0):
        source = ogr.Open(source_path, update_enabled)
        yield source
        source.FlushCache()
        source = None

    @staticmethod
    def source_layer_exists(source, layername):
        layer = source.GetLayerByName(layername)
        if layer:
            return True
        return False

    @classmethod
    def read_source_schema(cls, source):
        layers = cls.get_source_layers(source)
        return tuple((layer.name,
                      layer.get_schema() + layer.geometry_fields_schema())
                     for layer in layers)

    @staticmethod
    def get_source_layers(source):
        from .layer_manager import GpkgLayer
        return [
            GpkgLayer(layer, source) for layer in source
            if layer.GetName() != STYLES_TABLE
        ]

    @staticmethod
    def compare_schema(layer1, layer2, ignore_case=False):
        schema1 = layer1.get_full_schema()
        schema2 = layer2.get_full_schema()
        if ignore_case:
            schema1 = [(field[0].lower(), field[1], field[2])
                       for field in layer1.get_full_schema()]
            schema2 = [(field[0].lower(), field[1], field[2])
                       for field in layer2.get_full_schema()]
        schema1.sort(key=lambda field: field[0])
        schema2.sort(key=lambda field: field[0])
        new_fields = [field for field in schema1 if field not in schema2]
        deleted_fields = [field for field in schema2 if field not in schema1]
        deleted_fields.sort(key=lambda field: field[0])
        new_fields.sort(key=lambda field: field[0])
        return {
            "compatible": schema1 == schema2,
            "deleted_fields": deleted_fields,
            "new_fields": new_fields,
        }

    @staticmethod
    def get_layers_features(layers):
        for lyr in layers:
            yield lyr.get_features()
