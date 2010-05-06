# Copyright (C) 2010 by the Free Software Foundation, Inc.
#
# This file is part of GNU Mailman.
#
# GNU Mailman is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# GNU Mailman is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# GNU Mailman.  If not, see <http://www.gnu.org/licenses/>.

"""Tests for config.pck imports."""

from __future__ import absolute_import, unicode_literals

__metaclass__ = type
__all__ = [
    'test_suite',
    ]


import cPickle
import unittest

from mailman.app.lifecycle import create_list, remove_list
from mailman.testing.layers import ConfigLayer
from mailman.utilities.importer import import_config_pck
from pkg_resources import resource_filename



class TestBasicImport(unittest.TestCase):
    layer = ConfigLayer

    def setUp(self):
        self._mlist = create_list('blank@example.com')
        pickle_file = resource_filename('mailman.testing', 'config.pck')
        with open(pickle_file) as fp:
            self._pckdict = cPickle.load(fp)

    def tearDown(self):
        remove_list(self._mlist.fqdn_listname, self._mlist)

    def _import(self):
        import_config_pck(self._mlist, self._pckdict)

    def test_real_name(self):
        # The mlist.real_name gets set.
        self.assertEqual(self._mlist.real_name, 'Blank')
        self._import()
        self.assertEqual(self._mlist.real_name, 'Test')



def test_suite():
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(TestBasicImport))
    return suite
