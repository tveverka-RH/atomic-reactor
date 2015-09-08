"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import docker
from flexmock import flexmock
import pytest

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PostBuildPluginsRunner
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from atomic_reactor.util import ImageName
from tests.constants import DOCKERFILE_GIT
from tests.docker_mock import mock_docker


TEST_IMAGE = "fedora:latest"
SOURCE = {"provider": "git", "uri": DOCKERFILE_GIT}


class X(object):
    pass


PACKAGE_LIST = ['python-docker-py,1.3.1,1.fc24,noarch,(none),191456,7c1f60d8cde73e97a45e0c489f4a3b26,1438058212',
                'fedora-repos-rawhide,24,0.1,noarch,(none),2149,d41df1e059544d906363605d47477e60,1436940126']
PACKAGE_LIST_WITH_AUTOGENERATED = PACKAGE_LIST + ['gpg-pubkey,qwe123,zxcasd123,(none),(none),0,(none),1370645731']
PACKAGE_LIST_WITH_AUTOGENERATED_B = [x.encode("utf-8") for x in PACKAGE_LIST_WITH_AUTOGENERATED]


def mock_logs(cid, **kwargs):
    return b"\n".join(PACKAGE_LIST_WITH_AUTOGENERATED_B)


@pytest.mark.parametrize("ignore_autogenerated", [
    {"ignore": True, "package_list": PACKAGE_LIST},
    {"ignore": False, "package_list": PACKAGE_LIST_WITH_AUTOGENERATED},
])
def test_rpmqa_plugin(ignore_autogenerated):
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='fedora', tag='21'))
    setattr(workflow.builder, "source", X())
    setattr(workflow.builder.source, 'dockerfile_path', "/non/existent")
    setattr(workflow.builder.source, 'path', "/non/existent")
    mock_docker()
    flexmock(docker.Client, logs=mock_logs)
    runner = PostBuildPluginsRunner(
        tasker,
        workflow,
        [{"name": PostBuildRPMqaPlugin.key,
          "args": {
              'image_id': TEST_IMAGE,
              "ignore_autogenerated_gpg_keys": ignore_autogenerated["ignore"]}}
    ])
    results = runner.run()
    assert results[PostBuildRPMqaPlugin.key] == ignore_autogenerated["package_list"]


def mock_logs_raise(cid, **kwargs):
    raise RuntimeError


def test_rpmqa_plugin_exception():
    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(SOURCE, "test-image")
    setattr(workflow, 'builder', X())
    setattr(workflow.builder, 'image_id', "asd123")
    setattr(workflow.builder, 'base_image', ImageName(repo='fedora', tag='21'))
    setattr(workflow.builder, "source", X())
    setattr(workflow.builder.source, 'dockerfile_path', "/non/existent")
    setattr(workflow.builder.source, 'path', "/non/existent")
    mock_docker()
    flexmock(docker.Client, logs=mock_logs_raise)
    runner = PostBuildPluginsRunner(tasker, workflow,
                                    [{"name": PostBuildRPMqaPlugin.key,
                                      "args": {'image_id': TEST_IMAGE}}])
    results = runner.run()

    assert results is not None
    assert results[PostBuildRPMqaPlugin.key] is not None
    assert isinstance(results[PostBuildRPMqaPlugin.key], Exception)
