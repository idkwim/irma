#
# Copyright (c) 2013-2014 QuarksLab.
# This file is part of IRMA project.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License in the top-level directory
# of this distribution and at:
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# No part of the project, including this file, may be copied,
# modified, propagated, or distributed except according to the
# terms contained in the LICENSE file.

import os
import sys
import hashlib

from ConfigParser import SafeConfigParser

from lib.plugins import PluginBase
from lib.plugins import ModuleDependency, FileDependency
from lib.plugin_result import PluginResult


class VirusTotalPlugin(PluginBase):

    ##########################################################################
    # plugin metadata
    ##########################################################################

    _plugin_name_ = "VirusTotal"
    _plugin_author_ = "IRMA (c) Quarkslab"
    _plugin_version_ = "1.0.0"
    _plugin_category_ = "external"
    _plugin_description_ = "Plugin to query VirusTotal API"
    _plugin_dependencies_ = [
        ModuleDependency(
            'virus_total_apis', 
            help='See requirements.txt for needed dependencies'
        ),
        FileDependency(
            os.path.join(os.path.dirname(__file__), 'config.ini')
        )
    ]


    ##########################################################################
    # constructor
    ##########################################################################

    def __init__(self, apikey=None, private=None):
        # load default configuration file
        config = SafeConfigParser()
        config.read(os.path.join(os.path.dirname(__file__), 'config.ini'))

        # override default values if specified
        if apikey is None:
            self.apikey = config.get('VirusTotal', 'apikey')
        else:
            self.apikey = apikey

        if private is None:
            self.private = bool(config.get('VirusTotal', 'private'))
        else:
            self.private = private

        # choose either public or private API for requests
        if private:
            module = sys.modules['virus_total_apis'].PrivateApi
        else:
            module = sys.modules['virus_total_apis'].PublicApi
        self.module = module(self.apikey)

    def get_file_report(self, filename):
        digest = hashlib.md5(filename).hexdigest()
        return self.module.get_file_report(digest)

    ##########################################################################
    # probe interfaces
    ##########################################################################

    def run(self, paths):
        # allocate plugin results place-holders
        plugin_results = PluginResult(type(self).plugin_name)
        # query page
        plugin_results.start_time = None
        results = self.get_file_report(paths)
        plugin_results.end_time = None
        # update results
        plugin_results.result_code = 0 if results else 1
        plugin_results.data = {paths: results}
        return plugin_results.serialize()
