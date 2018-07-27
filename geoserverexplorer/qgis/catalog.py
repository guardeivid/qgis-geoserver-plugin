# -*- coding: utf-8 -*-
#
# (c) 2016 Boundless, http://boundlessgeo.com
# This code is licensed under the GPL 2.0 license.
#
from builtins import str
from builtins import object
import os
from qgis.core import *
from qgis.PyQt import QtCore
from geoserverexplorer.qgis import layers, exporter, utils
from geoserver.catalog import ConflictingDataError, UploadError, FailedRequestError
from geoserverexplorer.qgis.sldadapter import adaptGsToQgs,\
    getGsCompatibleSld
from geoserverexplorer.qgis import uri as uri_utils
from geoserverexplorer.geoserver.pki import PKICatalog
from geoserverexplorer.geoserver.auth import AuthCatalog
from geoserverexplorer.geoserver.basecatalog import BaseCatalog
from geoserverexplorer.geoserver import pem
from geoserverexplorer.geoserver.util import groupsWithLayer, removeLayerFromGroups, \
    addLayerToGroups
from geoserverexplorer.gui.gsnameutils import xmlNameFixUp, xmlNameIsValid
import requests
from geoserverexplorer.qgis.utils import addTrackedLayer
from qgiscommons2.settings import pluginSetting
from qgiscommons2.files import tempFilename

try:
    from processing.modeler.ModelerAlgorithm import ModelerAlgorithm
    from processing.script.ScriptAlgorithm import ScriptAlgorithm
    from processing.core.parameters import *
    from processing.core.outputs import *

    from processing.gui.AlgorithmExecutor import *
    from processing.gui.SilentProgress import SilentProgress
    from processing.tools.dataobjects import getObjectFromUri as load
    from processing.modeler.ModelerUtils import ModelerUtils
    processingOk = True
except Exception as e:
    processingOk = False

def createGeoServerCatalog(service_url = "http://localhost:8080/geoserver/rest",
                           username="admin",
                           password="geoserver",
                           authid=None,
                           authtype=None,
                           disable_ssl_certificate_validation=False):
    # if not authid use basic auth
    if not authid or not authtype:
        catalog = BaseCatalog(service_url, username, password, disable_ssl_certificate_validation)
    else:
        cache_time = pluginSetting("AuthCatalogXMLCacheTime")
        catalog = AuthCatalog(service_url, authid, cache_time)

    return CatalogWrapper(catalog)


class CatalogWrapper(object):
    '''
    This class is a wrapper for a catalog object, with convenience methods to use it with QGIS layers
    '''

    def __init__(self, catalog):
        self.catalog = catalog

    def clean(self):
        self.cleanUnusedStyles()
        self.cleanUnusedResources()

    def cleanUnusedStyles(self):
        '''cleans styles that are not used by any layer'''
        usedStyles = set()
        styles = self.catalog.get_styles()
        layers = self.catalog.get_layers()
        groups = self.catalog.get_layergroups()
        for layer in layers:
            if layer.default_style is not None:
                usedStyles.add(layer.default_style.name)
            usedStyles.update([s.name for s in layer.styles if s is not None])
        for group in groups:
            usedStyles.update([s for s in group.styles if s is not None])
        toDelete = [s for s in styles if s.name not in usedStyles]
        for style in toDelete:
            style.catalog.delete(style, purge = True)

    def cleanUnusedResources(self):
        '''cleans resources that are not published through any layer in the catalog'''
        usedResources = set()
        resources = self.catalog.get_resources()
        layers = self.catalog.get_layers()
        for layer in layers:
            usedResources.add(layer.resource.name)

        toDelete = [r for r in resources if r.name not in usedResources]
        for resource in toDelete:
            resource.catalog.delete(resource)

        for store in self.catalog.get_stores():
            if len(store.get_resources()) == 0:
                self.catalog.delete(store)

    def consolidateStyles(self):
        '''
        Deletes styles that are redundant and just keeps one copy of them
        in the catalog, configuring the corresponding layers to use that copy
        '''
        used = {}
        allstyles = self.catalog.get_styles()
        for style in allstyles:
            sld = style.sld_body.decode().replace("<sld:Name>%s</sld:Name>" % style.name, "")
            if sld in list(used.keys()):
                used[sld].append(style)
            else:
                used[sld] = [style]

        for sld, styles in used.items():
            if len(styles) == 1:
                continue
            #find the layers that use any of the secondary styles in the list, and make them use the first one
            styleNames = [s.name for s in styles[1:]]
            layers = self.catalog.get_layers()
            for layer in layers:
                changed = False
                if layer.default_style.name in styleNames:
                    layer.default_style = styles[0]
                    changed = True
                alternateStyles = layer.styles
                newAlternateStyles = set()
                for alternateStyle in alternateStyles:
                    if alternateStyle.name in styleNames:
                        newAlternateStyles.add(styles[0])
                    else:
                        newAlternateStyles.add(alternateStyle)
                newAlternateStyles = list(newAlternateStyles)
                if newAlternateStyles != alternateStyles:
                    layer.styles = newAlternateStyles
                    changed = True
                if changed:
                    self.catalog.save(layer)


    def publishStyle(self, layer, overwrite = True, name = None):
        '''
        Publishes the style of a given layer style in the specified catalog. If the overwrite parameter is True,
        it will overwrite a style with that name in case it exists
        '''

        if isinstance(layer, str):
            layer = layers.resolveLayer(layer)
        sld, icons = getGsCompatibleSld(layer)
        print(sld)
        if sld is not None:
            name = name if name is not None else layer.name()
            name = name.replace(" ", "_")
            self.uploadIcons(icons)
            self.catalog.create_style(name, sld, overwrite)
        return sld


    def uploadIcons(self, icons):
        for icon in icons:
            url = self.catalog.service_url + "rest/resource/styles/" + icon[1]
            if isinstance(self.catalog, PKICatalog):
                r = requests.put(url, data=icon[2], cert=(self.catalog.cert, self.catalog.key), verify=self.catalog.ca_cert)
            else:
                r = requests.put(url, data=icon[2], auth=(self.catalog.username, self.catalog.password))
            r.raise_for_status()


    def getDataFromLayer(self, layer):
        '''
        Returns the data corresponding to a given layer, ready to be passed to the
        method in the Catalog class for uploading to the server.
        If needed, it performs an export to ensure that the file format is supported
        by the upload API to be used for import. In that case, the data returned
        will point to the exported copy of the data, not the original data source
        '''
        if layer.type() == layer.RasterLayer:
            data = exporter.exportRasterLayer(layer)
        else:
            filename = exporter.exportVectorLayer(layer)
            basename, extension = os.path.splitext(filename)
            data = {
                'shp': basename + '.shp',
                'shx': basename + '.shx',
                'dbf': basename + '.dbf',
                'prj': basename + '.prj'
            }
        return data


    def _publishPostgisLayer(self, layer, workspace, overwrite, name, storename=None):
        uri = QgsDataSourceURI(layer.dataProvider().dataSourceUri())

        conname = self.getConnectionNameFromLayer(layer)
        storename = xmlNameFixUp(storename or conname)

        if not xmlNameIsValid(storename):
            raise Exception("Database connection name is invalid XML and can "
                            "not be auto-fixed: {0} -> {1}"
                            .format(conname, storename))

        user = uri.username()
        passwd = uri.password()
        if not uri or not passwd:
            connInfo = uri.connectionInfo()
            (success, user, passwd) = QgsCredentials.instance().get(connInfo, None, None)
            if success:
                QgsCredentials.instance().put(connInfo, user, passwd)
            else:
                raise Exception("Couldn't connect to database")

        store = createPGFeatureStore(self.catalog,
                                     storename,
                                     workspace = workspace,
                                     overwrite = overwrite,
                                     host = uri.host(),
                                     database = uri.database(),
                                     schema = uri.schema(),
                                     port = uri.port(),
                                     user = user,
                                     passwd = passwd)
        if store is not None:
            grpswlyr = []
            if overwrite:
                # TODO: How do we honor *unchecked* user setting of
                #   "Delete resource when deleting layer" here?
                #   Is it an issue, if overwrite is expected?

                # We will soon have two layers with slightly different names,
                # a temp based upon table.name, the other possibly existing
                # layer with the same custom name, which may belong to group(s).
                # If so, remove existing layer from any layer group, before
                # continuing on with layer delete and renaming of new feature
                # type layer to custom name, then add new resultant layer back
                # to any layer groups the existing layer belonged to. Phew!

                flyr = self.catalog.get_layer(name)
                if flyr is not None:
                    grpswlyr = groupsWithLayer(self.catalog, flyr)
                    if grpswlyr:
                        removeLayerFromGroups(self.catalog, flyr, grpswlyr)
                    self.catalog.delete(flyr)
                # TODO: What about when the layer name is the same, but the
                #   underlying db connection/store has changed? Not an issue?
                #   The layer is deleted, which is correct, but the original
                #   db store and feature type will not be changed. A conflict?
                frsc = store.get_resources(name=name)
                if frsc is not None:
                    self.catalog.delete(frsc)

            # for dbs the name has to be the table name, initially
            ftype = self.catalog.publish_featuretype(uri.table(), store,
                                                     layer.crs().authid())

            # once table-based feature type created, switch name to user-chosen
            if ftype.name != name:
                ftype.dirty["name"] = name
                ftype.dirty["title"] = name
            self.catalog.save(ftype)

            # now re-add to any previously assigned-to layer groups
            if overwrite and grpswlyr:
                ftypes = self.catalog.get_resources(name)
                if ftypes:
                    ftype = ftypes[0]
                    addLayerToGroups(self.catalog, ftype, grpswlyr,
                                     workspace=workspace)


    def _uploadRest(self, layer, workspace, overwrite, name):
        if layer.type() == layer.RasterLayer:
            path = self.getDataFromLayer(layer)
            self.catalog.create_coveragestore(name,
                                      path,
                                      workspace=workspace,
                                      overwrite=overwrite)
        elif layer.type() == layer.VectorLayer:
            path = self.getDataFromLayer(layer)
            self.catalog.create_featurestore(name,
                              path,
                              workspace=workspace,
                              overwrite=overwrite)


    def upload(self, layer, workspace=None, overwrite=True, name=None):
        '''uploads the specified layer'''

        if isinstance(layer, str):
            layer = layers.resolveLayer(layer)

        name = name or layer.name()
        title = name
        name = name.replace(" ", "_")

        if layer.type() not in (layer.RasterLayer, layer.VectorLayer):
            msg = layer.name() + ' is not a valid raster or vector layer'
            raise Exception(msg)

        provider = layer.dataProvider()
        try:
            if provider.name() == 'postgres':
                self._publishPostgisLayer(layer, workspace, overwrite, name)
            else:
                self._uploadRest(layer, workspace, overwrite, name)
        except UploadError as e:
            msg = ('Could not save the layer %s, there was an upload '
                   'error: %s' % (layer.name(), str(e)))
            e.args = (msg,)
            raise
        except ConflictingDataError as e:
            # A datastore of this name already exists
            msg = ('GeoServer reported a conflict creating a store with name %s: '
                   '"%s". This should never happen because a brand new name '
                   'should have been generated. But since it happened, '
                   'try renaming the file or deleting the store in '
                   'GeoServer.' % (layer.name(), str(e)))
            e.args = (msg,)
            raise e


        # Verify the resource was created
        resources = self.catalog.get_resources(name)
        if resources:
            resource = resources[0]
            assert resource.name == name
        else:
            msg = ('could not create layer %s.' % name)
            raise Exception(msg)

        if title != name:
            resource.dirty["title"] = title
            self.catalog.save(resource)
        if resource.latlon_bbox is None:
            box = resource.native_bbox[:4]
            minx, maxx, miny, maxy = [float(a) for a in box]
            if -180 <= minx <= 180 and -180 <= maxx <= 180 and \
                    -90 <= miny <= 90 and -90 <= maxy <= 90:
                resource.latlon_bbox = resource.native_bbox
                resource.projection = "EPSG:4326"
                self.catalog.save(resource)
            else:
                msg = ('Could not set projection for layer '
                       '[%s]. the layer has been created, but its projection should be set manually.')
                raise Exception(msg % layer.name())

    def getConnectionNameFromLayer(self, layer):
        connName = "postgis_store"
        uri = QgsDataSourceURI(layer.dataProvider().dataSourceUri())
        host = uri.host()
        database = uri.database()
        port = uri.port()
        settings = QtCore.QSettings()
        settings.beginGroup(u'/PostgreSQL/connections')
        for name in settings.childGroups():
            settings.beginGroup(name)
            host2 = str(settings.value('host'))
            database2 = str(settings.value('database'))
            port2 = str(settings.value('port'))
            settings.endGroup()
            if port == port2 and database == database2 and host == host2:
                connName = name + "_" + str(uri.schema())
        settings.endGroup()
        return connName

    def publishGroup(self, name, destName = None, workspace = None, overwrite = False, overwriteLayers = False):

        '''
        Publishes a group in the given catalog

        name: the name of the QGIS group to publish. It will also be used as the GeoServer layergroup name

        workspace: The workspace to add the group to

        overwrite: if True, it will overwrite a previous group with the specified name, if it exists

        overwriteLayers: if False, in case a layer in the group is not found in the specified workspace, the corresponding layer
        from the current QGIS project will be published, but all layers of the group that can be found in the GeoServer
        workspace will not be published. If True, all layers in the group are published, even if layers with the same name
        exist in the workspace
        '''
        groups = layers.getGroups()
        if name not in groups:
            raise Exception("The specified group does not exist")

        destName = destName if destName is not None else name
        gsgroup = self.catalog.get_layergroups(destName)[0]
        if gsgroup is not None and not overwrite:
            return

        group = groups[name]
        bounds = None

        def addToBounds(bbox, bounds):
            if bounds is not None:
                bounds = [min(bounds[0], bbox.xMinimum()),
                            max(bounds[1], bbox.xMaximum()),
                            min(bounds[2], bbox.yMinimum()),
                            max(bounds[3], bbox.yMaximum())]
            else:
                bounds = [bbox.xMinimum(), bbox.xMaximum(),
                          bbox.yMinimum(), bbox.yMaximum()]
            return bounds

        for layer in group:
            gslayer = self.catalog.get_layer(layer.name())
            if gslayer is None or overwriteLayers:
                self.publishLayer(layer, workspace, True)
            transform = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem("EPSG:4326"))
            bounds = addToBounds(transform.transformBoundingBox(layer.extent()), bounds)

        names = [layer.name() for layer in group]

        bounds = (str(bounds[0]), str(bounds[1]), str(bounds[2]), str(bounds[3]), "EPSG:4326")
        layergroup = self.catalog.create_layergroup(destName, names, names, bounds)

        self.catalog.save(layergroup)


    def publishLayer (self, layer, workspace=None, overwrite=True, name=None, style=None):
        '''
        Publishes a QGIS layer.
        It creates the corresponding store and the layer itself.
        If a pre-upload hook is set, its runs it and publishes the resulting layer

        layer: the layer to publish, whether as a QgsMapLayer object or its name in the QGIS TOC.

        workspace: the workspace to publish to. USes the default workspace if not passed
        or None

        name: the name for the published layer. Uses the QGIS layer name if not passed
        or None

        style: the style to use from the ones in the catalog. Will upload the QGIS style if
        not passed or None

        '''

        if isinstance(layer, str):
            layer = layers.resolveLayer(layer)

        addTrackedLayer(layer, self.catalog.service_url)

        name = xmlNameFixUp(name) if name is not None \
            else xmlNameFixUp(layer.name())

        gslayer = self.catalog.get_layer(name)
        if gslayer is not None and not overwrite:
            return

        sld = self.publishStyle(layer, overwrite, name) if style is None else None

        self.upload(layer, workspace, overwrite, name)

        if sld is not None or style is not None:
            #assign style to created store
            publishing = self.catalog.get_layer(name)
            publishing.default_style = style or self.catalog.get_styles(name)[0]
            self.catalog.save(publishing)

    def addLayerToProject(self, name, destName = None):
        '''
        Adds a new layer to the current project based on a layer in a GeoServer catalog
        It will create a new layer with a WFS or WCS connection, pointing to the specified GeoServer
        layer. In the case of a vector layer, it will also fetch its associated style and set it
        as the current style for the created QGIS layer
        '''
        layer = self.catalog.get_layer(name)
        if layer is None:
            raise Exception ("A layer with the name '" + name + "' was not found in the catalog")

        resource = layer.resource
        uri = uri_utils.layerUri(layer)
        QgsNetworkAccessManager.instance().cache().clear()

        if resource.resource_type == "featureType":
            qgslayer = QgsVectorLayer(uri, destName or resource.title, "WFS")
            if not qgslayer.isValid():
                raise Exception ("Layer at %s is not a valid layer" % uri)
            ok = True
            try:
                sld = layer.default_style.sld_body.decode()
                sld = adaptGsToQgs(str(sld))
                sldfile = tempFilename("sld")
                with open(sldfile, 'w') as f:
                    f.write(sld)
                msg, ok = qgslayer.loadSldStyle(sldfile)
            except Exception as e:
                ok = False
            QgsProject.instance().addMapLayers([qgslayer])
            addTrackedLayer(qgslayer, self.catalog.service_url)
            if not ok:
                raise Exception ("Layer was added, but style could not be set (maybe GeoServer layer is missing default style)")
        elif resource.resource_type == "coverage":
            qgslayer = QgsRasterLayer(uri, destName or resource.title, "wcs" )
            if not qgslayer.isValid():
                raise Exception ("Layer at %s is not a valid layer" % uri)
            QgsProject.instance().addMapLayers([qgslayer])
            addTrackedLayer(qgslayer, self.catalog.service_url)
        elif resource.resource_type == "wmsLayer":
            qgslayer = QgsRasterLayer(uri, destName or resource.title, "wms")
            if not qgslayer.isValid():
                raise Exception ("Layer at %s is not a valid layer" % uri)
            QgsProject.instance().addMapLayers([qgslayer])
            addTrackedLayer(qgslayer, self.catalog.service_url)
        else:
            raise Exception("Cannot add layer. Unsupported layer type.")

    def addGroupToProject(self, name):
        group = self.catalog.get_layergroups(name)[0]
        if group is None:
            raise Exception ("A group with the name '" + name + "' was not found in the catalog")

        uri = uri_utils.groupUri(group)

        qgslayer = QgsRasterLayer(uri, name, "wms")
        if not qgslayer.isValid():
            raise Exception ("Layer at %s is not a valid layer" % uri)
        QgsProject.instance().addMapLayers([qgslayer])


def createPGFeatureStore(catalog, name, workspace=None, overwrite=False,
    host="localhost", port=5432, database="db", schema="public", user="postgres", passwd=""):
    try:
        store = catalog.get_store(name, workspace)
    except FailedRequestError:
        store = None

    if store is None:
        store = catalog.create_datastore(name, workspace)
        store.connection_parameters.update(
        host=host, port=str(port), database=database, user=user, schema=schema,
        passwd=passwd, dbtype="postgis")
        catalog.save(store)
        return store
    elif overwrite:
        # if existing store is the same we are trying to add, just return it
        params = store.connection_parameters
        if (str(params['port']) == str(port)
            and params['database'] == database
            and params['host'] == host
            and params['user'] == user):
            return store
        else:
            msg = "store named '" + str(name) + "' already exist"
            if workspace is not None:
                msg += " in '" + str(workspace) + "'"
            msg += ' and has different connection parameters.'
            raise ConflictingDataError(msg)
    else:
        return None
