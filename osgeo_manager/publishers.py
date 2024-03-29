# -*- coding: utf-8 -*-
import json
import os
import sys
import time
import uuid
from decimal import Decimal
from io import BytesIO

import lxml
import requests
from django.conf import settings
from django.contrib.gis.geos import Polygon
from django.utils.translation import ugettext as _
from requests.auth import HTTPBasicAuth

from geonode.geoserver.helpers import (cascading_delete, get_store, gs_catalog,
                                       ogc_server_settings,
                                       set_attributes_from_geoserver)
from geonode.layers.models import Layer
from geonode.people.models import Profile
from geonode.security.views import _perms_info_json

from .constants import DEFAULT_WORKSPACE, ICON_REL_PATH, SLUGIFIER
from .decorators import validate_config
from .utils import urljoin

try:
    from celery.utils.log import get_task_logger as get_logger
except ImportError:
    from .log import get_logger
logger = get_logger(__name__)


class GeoserverPublisher(object):
    def __init__(
            self,
            geoserver_url=ogc_server_settings.LOCATION,
            workspace=DEFAULT_WORKSPACE,
            datastore=ogc_server_settings.datastore_db['NAME'],
            geoserver_user={
                'username': ogc_server_settings.credentials[0],
                'password': ogc_server_settings.credentials[1]
            }):
        self.base_url = geoserver_url
        self.workspace = workspace
        self.datastore = datastore
        self.username = geoserver_user.get('username',
                                           ogc_server_settings.credentials[0])
        self.password = geoserver_user.get('password',
                                           ogc_server_settings.credentials[1])

    @property
    def featureTypes_url(self):
        return urljoin(self.base_url, "rest/workspaces/", self.workspace,
                       "datastores/", self.datastore, "featuretypes")

    @property
    def gwc_url(self):
        return urljoin(self.base_url, "gwc/rest/")

    def get_gwc_layer_url(self, layername):
        return urljoin(self.gwc_url, "layers", layername)

    def publish_postgis_layer(self, tablename, layername):
        req = requests.post(
            self.featureTypes_url,
            headers={'Content-Type': "application/json"},
            auth=HTTPBasicAuth(self.username, self.password),
            json={"featureType": {
                "name": layername,
                "nativeName": tablename
            }}, allow_redirects=True, verify=False)
        logger.error("url: {}, status:{}".format(
            self.featureTypes_url, req.status_code))
        logger.error(req.text)
        if req.status_code == 201:
            return True
        return False

    def delete_layer(self, layername):
        try:
            cascading_delete(gs_catalog, "{}:{}".format(
                self.workspace, layername))
        except Exception as e:
            logger.error(e)

    def upload_file(self, file, rel_path=ICON_REL_PATH):
        url = urljoin(self.base_url, "rest/", "resource", rel_path,
                      os.path.basename(file.name))
        req = requests.put(
            url,
            data=file.read(),
            headers={'Content-Type': 'application/octet-stream'},
            auth=HTTPBasicAuth(self.username, self.password))
        message = "URL:{} STATUS:".format(url, req.status_code)
        logger.error(message)
        if req.status_code == 201:
            return True
        return False

    def get_new_style_name(self, sld_name):
        sld_name = SLUGIFIER(sld_name)
        style = gs_catalog.get_style(
            sld_name, workspace=DEFAULT_WORKSPACE)
        if not style:
            return sld_name
        else:
            timestr = time.strftime("%Y%m%d_%H%M%S")
            return "{}_{}".format(sld_name, timestr)

    def convert_sld_attributes(self, sld_path):
        tree = lxml.etree.parse(sld_path)
        root = tree.getroot()
        nsmap = {k: v for k, v in root.nsmap.items() if k}
        properties = tree.xpath('.//ogc:PropertyName', namespaces=nsmap)
        for prop in properties:
            value = SLUGIFIER(str(prop.text))
            prop.text = value
        properties = tree.xpath('.//sld:PropertyName', namespaces=nsmap)
        for prop in properties:
            value = SLUGIFIER(str(prop.text))
            prop.text = value
        return lxml.etree.tostring(tree)

    def create_style(self, name, sld_path, overwrite=True, raw=True):
        name = self.get_new_style_name(name)
        sld_body = self.convert_sld_attributes(sld_path)
        gs_catalog.create_style(
            name,
            sld_body,
            overwrite=overwrite,
            raw=True,
            workspace=DEFAULT_WORKSPACE)
        style = gs_catalog.get_style(
            name, workspace=DEFAULT_WORKSPACE)
        return style

    def set_default_style(self, layername, style):
        saved = False
        try:
            layer = gs_catalog.get_layer(layername)
            layer.default_style = style
            gs_catalog.save(layer)
            saved = True
        except Exception as e:
            logger.error(e)
        return saved

    def remove_cached(self, typename):
        import geonode.geoserver.helpers as helpers
        try:
            logger.warning("Clearing Layer Cache")
            helpers._invalidate_geowebcache_layer(typename)
            # the following line for compatiblilty 2.8rc11 and 2.8
            try:
                helpers._stylefilterparams_geowebcache_layer(typename)
            except BaseException:
                pass
            logger.warning("Layer Cache Cleared")
        except BaseException as e:
            logger.error(e)


class GeonodePublisher(object):
    def __init__(self,
                 storename=ogc_server_settings.datastore_db['NAME'],
                 workspace=DEFAULT_WORKSPACE,
                 owner=Profile.objects.filter(is_superuser=True).first()):
        self.store = get_store(gs_catalog, storename, workspace)
        self.storename = storename
        self.workspace = workspace
        self.owner = owner

    def publish(self, config=None):
        config = validate_config(config_obj=config)
        resource = gs_catalog.get_resource(
            config.name, store=self.store, workspace=self.workspace)
        if not resource:
            raise Exception("Cannot Find Layer In Geoserver")
        name = resource.name
        the_store = resource.store
        workspace = the_store.workspace
        layer = None
        try:
            logger.warning("=========> Creating the Layer in Django")
            layer, created = Layer.objects.get_or_create(
                name=name,
                workspace=workspace.name,

                defaults={
                    "store": the_store.name,
                    "storeType": the_store.resource_type,
                    "alternate":
                    "%s:%s" % (workspace.name, resource.name),
                    "title": (resource.title or 'No title provided'),
                    "abstract":
                    (resource.abstract or _('No abstract provided')),
                    "owner": config.get_user(),
                    "uuid": str(uuid.uuid4()),
                    "bbox_polygon": Polygon.from_bbox((Decimal(resource.native_bbox[0]),
                                                       Decimal(resource.native_bbox[2]),
                                                       Decimal(resource.native_bbox[1]),
                                                       Decimal(resource.native_bbox[3]))),
                    "srid": resource.projection
                })
            logger.warning("=========> Settting permissions")
            # sync permissions in GeoFence
            perm_spec = json.loads(_perms_info_json(layer))
            layer.set_permissions(perm_spec)
            logger.warning("=========> Settting Attributes")
            # recalculate the layer statistics
            set_attributes_from_geoserver(layer, overwrite=True)
            layer.save()
            logger.warning("=========> Fixing Metadata Links")
            # Fix metadata links if the ip has changed
            if layer.link_set.metadata().count() > 0:
                if not created and settings.SITEURL \
                        not in layer.link_set.metadata()[0].url:
                    layer.link_set.metadata().delete()
                    layer.save()
                    metadata_links = []
                    for link in layer.link_set.metadata():
                        metadata_links.append((link.mime, link.name, link.url))
                    resource.metadata_links = metadata_links
                    gs_catalog.save(resource)

        except Exception as e:
            logger.error(e)
            exception_type, error, traceback = sys.exc_info()
        else:
            if layer:
                if config.permissions:
                    layer.set_permissions(config.permissions)
                else:
                    layer.set_default_permissions()
            return layer
