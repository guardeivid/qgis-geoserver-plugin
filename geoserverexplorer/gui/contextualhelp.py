# -*- coding: utf-8 -*-
#
# (c) 2016 Boundless, http://boundlessgeo.com
# This code is licensed under the GPL 2.0 license.
#
"""
Contextual help components for use in dialogs, etc.
"""

import os
from qgis.PyQt import QtGui, QtCore, QtWidgets


# noinspection PyAttributeOutsideInit, PyPep8Naming
class InfoIcon(QtWidgets.QLabel):
    def __init__(self, tip, parent=None):
        QtWidgets.QLabel.__init__(self, parent)
        self.tiptxt = tip
        self.setSizePolicy(QtGui.QSizePolicy.Fixed,
                           QtGui.QSizePolicy.Fixed)
        self.setMaximumSize(QtCore.QSize(16, 16))
        self.setMinimumSize(QtCore.QSize(16, 16))
        infopx = QtGui.QPixmap(
            os.path.dirname(os.path.dirname(__file__)) + "/images/help.png")
        self.setPixmap(infopx)

        self.setMouseTracking(True)

    def mouseMoveEvent(self, event):
        # QtGui.QToolTip.showText(self.mapToGlobal(event.pos()),
        #                         self.tiptxt, self, self.rect())
        QtGui.QToolTip.showText(self.mapToGlobal(event.pos()),
                                self.tiptxt, self)
        event.ignore()


# noinspection PyPep8Naming
def infoIcon(tip, parent=None):
    return InfoIcon(tip, parent)
