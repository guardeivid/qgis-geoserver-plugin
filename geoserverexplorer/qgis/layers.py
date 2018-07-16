# -*- coding: utf-8 -*-
#
# (c) 2016 Boundless, http://boundlessgeo.com
# This code is licensed under the GPL 2.0 license.
#
from qgis.core import *
from geoserverexplorer import config

ALL_TYPES = -1

class WrongLayerNameException(BaseException) :
    pass

def resolveLayer(name):
    layers = getAllLayers()
    for layer in layers:
        if layer.name() == name:
            return layer
    raise WrongLayerNameException()

def getPublishableLayers():
    layers = getAllLayers()
    return [layer for layer in layers if layer.dataProvider().name() != "wms"]

def getAllLayers():
    return QgsProject.instance().mapLayers().values()

def getAllLayersAsDict():
    return {layer.source(): layer for layer in getAllLayers()}

def getPublishableLayersAsDict():
    return {layer.source(): layer for layer in getPublishableLayers()}

def getGroups():
    groups = {}
    root = QgsProject.instance().layerTreeRoot()
    for child in root.children():
        if isinstance(child, QgsLayerTreeGroup):
            layers = []
            for subchild in child.children():
                if isinstance(subchild, QgsLayerTreeLayer):
                    layers.append(subchild.layer())
            groups[child.name()] = layers
        '''elif isinstance(child, QgsLayerTreeLayer):
            layer = child.layer()
            if layer.type() not in skipType:
                item = TreeLayerItem(layer, self.layersTree)
                item.setCheckState(0, Qt.Checked if layer in visibleLayers else Qt.Unchecked)
                item.toggleChildren()
                self.layersTree.addTopLevelItem(item)'''
    return groups



    rels = config.iface.legendInterface().groupLayerRelationship()
    for rel in rels:
        groupName = rel[0]
        if groupName != '':
            groupLayers = rel[1]
            groups[groupName] = [QgsProject.instance().mapLayer(layerid) for layerid in groupLayers]
    return groups


