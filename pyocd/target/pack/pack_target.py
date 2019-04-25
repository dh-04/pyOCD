# pyOCD debugger
# Copyright (c) 2017-2019 Arm Limited
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function
import cmsis_pack_manager
import logging
import six
import os

from .cmsis_pack import (CmsisPack, MalformedCmsisPackError)
from ..family import FAMILIES
from .. import TARGET
from ...core.coresight_target import CoreSightTarget
from ...debug.svd.loader import SVDFile
from ...utility.compatibility import FileNotFoundError_

LOG = logging.getLogger(__name__)

class ManagedPacks(object):
    """! @brief Namespace for managed CMSIS-Pack utilities.
    
    By managed, we mean managed by the cmsis-pack-manager package. All the methods on this class
    apply only to those packs managed by cmsis-pack-manager, not any targets from packs specified
    by the user.
    """

    @staticmethod
    def get_installed_packs(cache=None):
        """! @brief Return a list containing CmsisPackRef objects for all installed packs."""
        try:
            cache = cache or cmsis_pack_manager.Cache(True, True)
            results = []
            # packs_for_devices() returns only unique packs.
            for pack in cache.packs_for_devices(cache.index.values()):
                pack_path = os.path.join(cache.data_path, pack.get_pack_name())
                if os.path.isfile(pack_path):
                    results.append(pack)
            return results
        except FileNotFoundError:
            # cmsis-pack-manage can raise this exception if the cache is empty.
            return []

    @staticmethod
    def get_installed_targets():
        """! @brief Return a list of CmsisPackDevice objects for installed pack targets."""
        try:
            cache = cmsis_pack_manager.Cache(True, True)
            results = []
            for pack in ManagedPacks.get_installed_packs(cache=cache):
                pack_path = os.path.join(cache.data_path, pack.get_pack_name())
                pack = CmsisPack(pack_path)
                results += list(pack.devices)
            return sorted(results, key=lambda dev:dev.part_number)
        except FileNotFoundError:
            # cmsis-pack-manager can raise this exception if the cache is empty.
            pass

    @staticmethod
    def populate_target(device_name):
        """! @brief Add targets from cmsis-pack-manager matching the given name.

        Targets are added to the `#TARGET` list. A case-insensitive comparison against the
        device part number is used to find the target to populate. If multiple packs are installed
        that provide the same part numbers, all matching targets will be populated.
        """
        targets = ManagedPacks.get_installed_targets()
        for dev in targets:
            if device_name.lower() == dev.part_number.lower():
                PackTargets.populate_device(dev)

class _PackTargetMethods(object):
    """! @brief Container for methods added to the dynamically generated pack target subclass."""

    def _pack_target__init__(self, session):
        """! @brief Constructor for dynamically created target class."""
        super(self.__class__, self).__init__(session, self._pack_device.memory_map)

        self.vendor = self._pack_device.vendor
        self.part_families = self._pack_device.families
        self.part_number = self._pack_device.part_number

        self._svd_location = SVDFile(filename=self._pack_device.svd)

    def _pack_target_create_init_sequence(self):
        """! @brief Creates an init task to set the default reset type."""
        seq = super(self.__class__,self).create_init_sequence()
        seq.insert_after('create_cores',
            ('set_default_reset_type', self.set_default_reset_type))
        return seq

    def _pack_target_set_default_reset_type(self):
        """! @brief Set's the first core's default reset type to the one specified in the pack."""
        if 0 in self.cores:
            self.cores[0].default_reset_type = self._pack_device.default_reset_type

class PackTargets(object):
    """! @brief Namespace for CMSIS-Pack target generation utilities. """

    @staticmethod
    def _find_family_class(dev):
        """! @brief Search the families list for matching entry."""
        for familyInfo in FAMILIES:
            # Skip if wrong vendor.
            if dev.vendor != familyInfo.vendor:
                continue

            # Scan each level of families
            for familyName in dev.families:
                for regex in familyInfo.matches:
                    # Require the regex to match the entire family name.
                    match = regex.match(familyName)
                    if match and match.span() == (0, len(familyName)):
                        return familyInfo.klass
        else:
            # Default target superclass.
            return CoreSightTarget

    @staticmethod
    def _generate_pack_target(dev):
        """! @brief Generates a new.

        The new target class is added to the `#TARGET` list.

        @param dev A CmsisPackDevice object.
        @return A new subclass of either CoreSightTarget or one of the family classes.
        """
        try:
            # Look up the target family superclass.
            superklass = PackTargets._find_family_class(dev)

            # Replace spaces and dashes with underscores on the new target subclass name.
            subclassName = dev.part_number.replace(' ', '_').replace('-', '_')

            # Create a new subclass for this target.
            targetClass = type(subclassName, (superklass,), {
                        "_pack_device": dev,
                        "__init__": _PackTargetMethods._pack_target__init__,
                        "create_init_sequence": _PackTargetMethods._pack_target_create_init_sequence,
                        "set_default_reset_type": _PackTargetMethods._pack_target_set_default_reset_type,
                    })
            return targetClass
        except (MalformedCmsisPackError, FileNotFoundError_) as err:
            LOG.warning(err)
            return None

    @staticmethod
    def populate_device(dev):
        """! @brief Generates and populates the target defined by a CmsisPackDevice.

        The new target class is added to the `#TARGET` list.

        @param dev A CmsisPackDevice object.
        """
        try:
            tgt = PackTargets._generate_pack_target(dev)
            if tgt is None:
                return
            part = dev.part_number.lower()

            # Make sure there isn't a duplicate target name.
            if part not in TARGET:
                TARGET[part] = tgt
        except (MalformedCmsisPackError, FileNotFoundError_) as err:
            LOG.warning(err)

    @staticmethod
    def populate_targets_from_pack(pack_list):
        """! @brief Adds targets defined in the provided CMSIS-Pack.

        Targets are added to the `#TARGET` list.

        @param pack_list Sequence of strings that are paths to .pack files, file objects,
            ZipFile instances, or CmsisPack instance. May also be a single object of one of
            the accepted types.
        """
        if not isinstance(pack_list, (list, tuple)):
            pack_list = [pack_list]
        for pack_or_path in pack_list:
            if isinstance(pack_or_path, six.string_types):
                LOG.info("Loading CMSIS-Pack: %s", pack_or_path)
            if not isinstane(pack_or_path, CmsisPack):
                pack = CmsisPack(pack_or_path)
            for dev in pack.devices:
                PackTargets.populate_device(dev)
