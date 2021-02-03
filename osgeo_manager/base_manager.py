# -*- coding: utf-8 -*-
try:
    import ogr
except ImportError:
    from osgeo import ogr
import os
import pipes
import subprocess
import time

from geonode.geoserver.helpers import ogc_server_settings
from geonode.layers.models import Layer

from .constants import DOWNLOADS_DIR_PATH, POSTGIS_OPTIONS
from .exceptions import OSGEOLayerException, SourceException
from .layers import OSGEOLayer
from .log import get_logger
from .mixins import OSGEOManagerMixin
from .os_utils import get_new_dir
from .styles import StyleManager
from .utils import get_sld_body

logger = get_logger(__name__)


class OSGEOManager(OSGEOManagerMixin):
    def __init__(self, package_path):
        self.path = package_path
        self.get_source()

    def get_source(self):
        with self.open_source(self.path) as source:
            self.source = source
        return self.source

    def check_schema_geonode(self, layername, glayername, ignore_case=False):
        gpkg_layer = self.get_layer_by_name(layername)
        glayer = Layer.objects.get(alternate=glayername)
        if not gpkg_layer:
            raise SourceException("Cannot find this layer in Source")
        geonode_manager = OSGEOManager(get_connection(), is_postgis=True)
        glayer = geonode_manager.get_layer_by_name(glayername.split(":").pop())
        if not glayer:
            raise OSGEOLayerException(
                "Layer {} Cannot be found in Source".format(glayername))
        check = OSGEOManager.compare_schema(gpkg_layer, glayer, ignore_case)
        return check

    def layer_exists(self, layername):
        return OSGEOManager.source_layer_exists(self.source, layername)

    def get_layers(self):
        return self.get_source_layers(self.source)

    def get_layernames(self):
        return tuple(layer.name for layer in self.get_layers())

    def get_layer_by_name(self, layername):
        if self.layer_exists(layername):
            return OSGEOLayer(
                self.source.GetLayerByName(layername), self.source)
        return None

    def read_schema(self):
        return self.read_source_schema(self.source)

    def get_features(self):
        return self.get_layers_features(self.get_layers())

    def _cmd_lyr_postgis(self,
                         gpkg_path,
                         connectionString,
                         layername,
                         options=POSTGIS_OPTIONS._asdict()):

        overwrite = options.get('overwrite', POSTGIS_OPTIONS.overwrite)
        skipfailures = options.get('skipfailures',
                                   POSTGIS_OPTIONS.skipfailures)
        append_layer = options.get('append', POSTGIS_OPTIONS.append)
        update_layer = options.get('update', POSTGIS_OPTIONS.update)
        command = """ogr2ogr {} {} {} -f "PostgreSQL" PG:"{}" {} {}  {} """\
            .format("-overwrite" if overwrite else "",
                    "-update" if update_layer else "",
                    "-append" if append_layer else "",
                    connectionString,
                    gpkg_path, "-skipfailures" if skipfailures else "",
                    pipes.quote(layername))
        return command

    def execute(self, cmd):
        p = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        return out, err

    def layer_to_postgis(self,
                         layername,
                         connectionString,
                         overwrite=True,
                         temporary=False,
                         launder=False,
                         name=None):
        with self.open_source(connectionString) as source:
            layer = self.source.GetLayerByName(layername)
            assert layer
            layer = OSGEOLayer(layer, source)
            return layer.copy_to_source(
                source,
                overwrite=overwrite,
                temporary=temporary,
                launder=launder,
                name=name)

    def layer_to_postgis_cmd(self, layername, connectionString, options=None):
        cmd = self._cmd_lyr_postgis(
            self.path,
            connectionString,
            layername,
            options=options if options else POSTGIS_OPTIONS._asdict())
        out, err = self.execute(cmd)
        if not err:
            logger.warning("{} Added Successfully".format(layername))

    @staticmethod
    def postgis_as_gpkg(connectionString, dest_path, layernames=None):
        if not dest_path.endswith(".gpkg"):
            dest_path += ".gpkg"
        with OSGEOManager.open_source(connectionString) as postgis_source:
            ds = ogr.GetDriverByName('GPKG').CreateDataSource(dest_path)
            layers = OSGEOManager.get_source_layers(postgis_source) \
                if not layernames \
                else [layer for layer in
                      OSGEOManager.get_source_layers(postgis_source)
                      if layer and layer.name in layernames]
            for lyr in layers:
                ds.CopyLayer(lyr.gpkg_layer, lyr.name)
        return dest_path

    @staticmethod
    def backup_portal(dest_path=None):
        final_path = None
        if not dest_path:
            dest_path = get_new_dir(base_dir=DOWNLOADS_DIR_PATH)
        file_suff = time.strftime("%Y_%m_%d-%H_%M_%S")
        package_dir = os.path.join(dest_path, "backup_%s.gpkg" % (file_suff))
        connection_string = get_connection()
        try:
            if not os.path.isdir(dest_path) or not os.access(
                    dest_path, os.W_OK):
                raise Exception(
                    'maybe destination is not writable or not a directory')
            with OSGEOManager.open_source(connection_string) as ds:
                if ds:
                    all_layers = Layer.objects.all()
                    layer_styles = []
                    table_names = []
                    for layer in all_layers:
                        typename = str(layer.alternate)
                        table_name = typename.split(":").pop()
                        if OSGEOManager.source_layer_exists(ds, table_name):
                            table_names.append(table_name)
                            gattr = str(
                                layer.attribute_set.filter(
                                    attribute_type__contains='gml').first()
                                .attribute)
                            layer_style = layer.default_style
                            sld_url = layer_style.sld_url
                            style_name = str(layer_style.name)
                            layer_styles.append((table_name, gattr, style_name,
                                                 get_sld_body(sld_url)))
                    OSGEOManager.postgis_as_gpkg(
                        connection_string, package_dir, layernames=table_names)
                    stm = StyleManager(package_dir)
                    stm.create_table()
                    for style in layer_styles:
                        stm.add_style(*style, default=True)
            final_path = dest_path

        except Exception as e:
            logger.error(e)
        finally:
            return final_path


def get_connection(database_name=None, schema=None):
    db_settings = ogc_server_settings.datastore_db
    db_name = database_name if database_name else db_settings.get('NAME')
    user = db_settings.get('USER')
    password = db_settings.get('PASSWORD')
    host = db_settings.get('HOST', 'localhost')
    port = db_settings.get('PORT', 5432)
    return OSGEOManager.build_connection_string(db_name, schema, user, password, port, host)
