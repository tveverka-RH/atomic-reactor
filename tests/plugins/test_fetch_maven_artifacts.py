"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import json
from pathlib import Path

from flexmock import flexmock

import koji
import pytest
import responses
import yaml

from atomic_reactor.constants import (REPO_FETCH_ARTIFACTS_KOJI,
                                      REPO_FETCH_ARTIFACTS_PNC,
                                      REPO_FETCH_ARTIFACTS_URL, DOCKERFILE_FILENAME)
from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_fetch_maven_artifacts import FetchMavenArtifactsPlugin
from osbs.utils import ImageName
from textwrap import dedent

from tests.mock_env import MockEnv

KOJI_HUB = 'https://koji-hub.com'
KOJI_ROOT = 'https://koji-root.com'
PNC_ROOT = 'https://pnc-root.com'

FILER_ROOT_DOMAIN = 'filer.com'
FILER_ROOT = 'https://' + FILER_ROOT_DOMAIN

DEFAULT_KOJI_BUILD_ID = 472397

DEFAULT_KOJI_BUILD = {
    'build_id': DEFAULT_KOJI_BUILD_ID,
    'id': DEFAULT_KOJI_BUILD_ID,
    'name': 'com.sun.xml.bind.mvn-jaxb-parent',
    'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
    'release': '1',
    'version': '2.2.11.4',
}

ARCHIVE_JAXB_SUN_POM = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '697317209103338c7c841e327bb6e7b0',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.pom',
    'group_id': 'com.sun.xml.bind',
    'id': 1269850,
    'size': 15320,
    'version': '2.2.11-4'
}

ARCHIVE_JAXB_SUN_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '06bae6472e3d1635f0c3b79bd314fdf3',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.jar',
    'group_id': 'com.sun.xml.bind',
    'id': 1269849,
    'size': 252461,
    'version': '2.2.11-4'
}

ARCHIVE_JAXB_JAVADOC_SUN_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '3643ba275364b29117f2bc5f0bcf18d9',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4-javadoc.jar',
    'group_id': 'com.sun.xml.bind',
    'id': 1269848,
    'size': 819956,
    'version': '2.2.11-4'
}

ARCHIVE_JAXB_GLASSFISH_POM = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': 'cc7b7a4d1c33d83fba9adf95226af570',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.pom',
    'group_id': 'org.glassfish.jaxb',
    'id': 1269791,
    'size': 3092,
    'version': '2.2.11-4'
}

ARCHIVE_JAXB_GLASSFISH_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '2ba4912b1a3c699b09ec99e19820fb09',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.jar',
    'group_id': 'org.glassfish.jaxb',
    'id': 1269790,
    'size': 156400,
    'version': '2.2.11-4'
}

ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '69bc6de0a57dd10c7370573a8e76f0b2',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4-javadoc.jar',
    'group_id': 'org.glassfish.jaxb',
    'id': 1269789,
    'size': 524417,
    'version': '2.2.11-4'
}

ARCHIVE_SHA1 = {
    'artifact_id': 'jaxb-sha1',
    'build_id': 472397,
    'checksum': '66bd6b88ba636993ad0fd522cc1254c9ff5f5a1c',
    'checksum_type': koji.CHECKSUM_TYPES['sha1'],
    'filename': 'jaxb-core-2.2.11-4-sha1.jar',
    'group_id': 'org.glassfish.jaxb.sha1',
    'id': 1269792,
    'size': 524417,
    'version': '2.2.11-4'
}

ARCHIVE_SHA256 = {
    'artifact_id': 'jaxb-sha256',
    'build_id': 472397,
    'checksum': 'ca52bcbda16954c9e83e4c0049277ac77f014ecc16a94ed92bc3203fa13aac7d',
    'checksum_type': koji.CHECKSUM_TYPES['sha256'],
    'filename': 'jaxb-core-2.2.11-4-sha256.jar',
    'group_id': 'org.glassfish.jaxb.sha256',
    'id': 1269792,
    'size': 524417,
    'version': '2.2.11-4'
}

# To avoid having to actually download archives during testing,
# the checksum value is based on the mocked download response,
# which is simply the "filename" and "group_id" values.
DEFAULT_ARCHIVES = [
    ARCHIVE_JAXB_SUN_POM,
    ARCHIVE_JAXB_SUN_JAR,
    ARCHIVE_JAXB_JAVADOC_SUN_JAR,
    ARCHIVE_JAXB_GLASSFISH_POM,
    ARCHIVE_JAXB_GLASSFISH_JAR,
    ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR,
    ARCHIVE_SHA1,
    ARCHIVE_SHA256,
]

REMOTE_FILE_SPAM = {
    'url': FILER_ROOT + '/spam/spam.jar',
    'source-url': FILER_ROOT + '/spam/spam-sources.tar',
    'md5': 'ec61f019a3d0826c04ab20c55462aa24',
    'source-md5': 'b4dbaf349d175aa5bbd5c5d076c00393',
}

REMOTE_FILE_BACON = {
    'url': FILER_ROOT + '/bacon/bacon.jar',
    'source-url': FILER_ROOT + '/bacon/bacon-sources.tar',
    'md5': 'b4dbaf349d175aa5bbd5c5d076c00393',
    'source-md5': 'b1605c846e03035a6538873e993847e5',
}

REMOTE_FILE_WITH_TARGET = {
    'url': FILER_ROOT + '/eggs/eggs.jar',
    'source-url': FILER_ROOT + '/eggs/eggs-sources.tar',
    'md5': 'b1605c846e03035a6538873e993847e5',
    'source-md5': 'ec61f019a3d0826c04ab20c55462aa24',
    'target': 'sgge.jar'
}

REMOTE_FILE_SHA1 = {
    'url': FILER_ROOT + '/ham/ham.jar',
    'source-url': FILER_ROOT + '/ham/ham-sources.tar',
    'sha1': 'c4f8d66d78f5ed17299ae88fed9f8a8c6f3c592a',
    'source-sha1': '0eb3dc253aeda45e272f07cf6e77fcc8bcf6628a',
}

REMOTE_FILE_SHA256 = {
    'url': FILER_ROOT + '/sausage/sausage.jar',
    'source-url': FILER_ROOT + '/sausage/sausage-sources.tar',
    'sha256': '0da8e7df6c45b1006b10e4d0df5e1a8d5c4dc17c2c9c0ab53c5714dadb705d1c',
    'source-sha256': '05892a95a8257a6c51a5ee4ba122e14e9719d7ead3b1d44e7fbea604da2fc8d1'
}

REMOTE_FILE_MULTI_HASH = {
    'url': FILER_ROOT + '/biscuit/biscuit.jar',
    'source-url': FILER_ROOT + '/biscuit/biscuit-sources.tar',
    'sha256': '05892a95a8257a6c51a5ee4ba122e14e9719d7ead3b1d44e7fbea604da2fc8d1',
    'source-sha256': '0da8e7df6c45b1006b10e4d0df5e1a8d5c4dc17c2c9c0ab53c5714dadb705d1c',
    'sha1': '0eb3dc253aeda45e272f07cf6e77fcc8bcf6628a',
    'source-sha1': 'c4f8d66d78f5ed17299ae88fed9f8a8c6f3c592a',
    'md5': '24e4dec8666658ec7141738dbde951c5',
    'source-md5': 'b1605c846e03035a6538873e993847e5',
}

# To avoid having to actually download archives during testing,
# the md5 value is based on the mocked download response,
# which is simply the url.
DEFAULT_REMOTE_FILES = [REMOTE_FILE_SPAM, REMOTE_FILE_BACON, REMOTE_FILE_WITH_TARGET,
                        REMOTE_FILE_SHA1, REMOTE_FILE_SHA256, REMOTE_FILE_MULTI_HASH]

ARTIFACT_MD5 = {
    'build_id': '12',
    'artifacts': [
        {
            'id': '122',
            'target': 'md5.jar'
        }
    ]
}

ARTIFACT_SHA1 = {
    'build_id': '12',
    'artifacts': [
        {
            'id': '123',
            'target': 'sha1.jar'
        }
    ]
}

ARTIFACT_SHA256 = {
    'build_id': '12',
    'artifacts': [
        {
            'id': '124',
            'target': 'sha256.jar'
        }
    ]
}

ARTIFACT_MULTI_HASH = {
    'build_id': '12',
    'artifacts': [
        {
            'id': '125',
            'target': 'multi-hash.jar'
        }
    ]
}

RESPONSE_MD5 = {
    'id': '122',
    'publicUrl': FILER_ROOT + '/md5.jar',
    'md5': '900150983cd24fb0d6963f7d28e17f72'
}

RESPONSE_SHA1 = {
    'id': '123',
    'publicUrl': FILER_ROOT + '/sha1.jar',
    'sha1': 'a9993e364706816aba3e25717850c26c9cd0d89d'
}

RESPONSE_SHA256 = {
    'id': '124',
    'publicUrl': FILER_ROOT + '/sha256.jar',
    'sha256': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
}

RESPONSE_MULTI_HASH = {
    'id': '125',
    'publicUrl': FILER_ROOT + '/multi-hash.jar',
    'sha256': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    'sha1': 'a9993e364706816aba3e25717850c26c9cd0d89d',
    'md5': '900150983cd24fb0d6963f7d28e17f72'
}

DEFAULT_PNC_ARTIFACTS = {'builds': [ARTIFACT_MD5, ARTIFACT_SHA1, ARTIFACT_SHA256,
                                    ARTIFACT_MULTI_HASH]}

DEFAULT_PNC_RESPONSES = {
    RESPONSE_MD5['id']: RESPONSE_MD5,
    RESPONSE_SHA1['id']: RESPONSE_SHA1,
    RESPONSE_SHA256['id']: RESPONSE_SHA256,
    RESPONSE_MULTI_HASH['id']: RESPONSE_MULTI_HASH
}


class MockSource(object):
    def __init__(self, source_dir: Path):
        self.dockerfile_path = str(source_dir / DOCKERFILE_FILENAME)
        self.path = str(source_dir)

    def get_build_file_path(self):
        return self.dockerfile_path, self.path


class X(object):
    image_id = "xxx"
    base_image = ImageName.parse("fedora/jboss")
    set_base_image = flexmock()


class MockedPathInfo(object):
    def __init__(self, topdir=None):
        self.topdir = topdir

    def mavenbuild(self, build):
        return '{topdir}/packages/{name}/{version}/{release}/maven'.format(topdir=self.topdir,
                                                                           **build)

    def mavenfile(self, maveninfo):
        return '{group_id}/{artifact_id}/{version}/{filename}'.format(**maveninfo)


def mock_env(workflow, source_dir: Path, r_c_m=None, domains_override=None):
    if not r_c_m:
        r_c_m = {
            'version': 1,
            'koji': {
                'hub_url': KOJI_HUB,
                'root_url': KOJI_ROOT,
                'auth': {}
            },
            'pnc': {
                'base_api_url': PNC_ROOT,
                'get_artifact_path': 'artifacts/{}',
            },
        }

    if domains_override:
        r_c_m.setdefault('artifacts_allowed_domains', domains_override)

    env = (MockEnv(workflow)
           .for_plugin('prebuild', FetchMavenArtifactsPlugin.key)
           .make_orchestrator()
           .set_reactor_config(r_c_m))

    env.workflow.source = MockSource(source_dir)

    return env


def mock_koji_session(koji_proxyuser=None, koji_ssl_certs_dir=None,
                      koji_krb_principal=None, koji_krb_keytab=None,
                      build_info=None, archives=None):
    if not build_info:
        build_info = DEFAULT_KOJI_BUILD
    if not archives:
        archives = DEFAULT_ARCHIVES

    flexmock(koji, PathInfo=MockedPathInfo)

    session = flexmock()

    (flexmock(koji)
        .should_receive('ClientSession')
        .once()
        .and_return(session))

    def mock_get_build(nvr):
        if nvr == DEFAULT_KOJI_BUILD['nvr']:
            return DEFAULT_KOJI_BUILD
        else:
            return None
    (session
        .should_receive('getBuild')
        .replace_with(mock_get_build))

    (session
        .should_receive('listArchives')
        .and_return(archives))

    (session
        .should_receive('krb_login')
        .and_return(True))
    return session


def mock_fetch_artifacts_by_nvr(source_dir: Path, contents=None):
    if contents is None:
        contents = dedent("""\
            - nvr: com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1
            """)

    source_dir.joinpath(REPO_FETCH_ARTIFACTS_KOJI).write_text(contents, "utf-8")


def mock_fetch_artifacts_from_pnc(source_dir: Path, contents=None):
    if contents is None:
        contents = yaml.safe_dump(DEFAULT_PNC_ARTIFACTS)

    source_dir.joinpath(REPO_FETCH_ARTIFACTS_PNC).write_text(contents, "utf-8")


def mock_fetch_artifacts_by_url(source_dir: Path, contents=None):
    if contents is None:
        contents = yaml.safe_dump(DEFAULT_REMOTE_FILES)

    source_dir.joinpath(REPO_FETCH_ARTIFACTS_URL).write_text(contents, "utf-8")


def mock_nvr_downloads(build_info=None, archives=None, overrides=None):
    if not build_info:
        build_info = DEFAULT_KOJI_BUILD
    if not archives:
        archives = DEFAULT_ARCHIVES
    if not overrides:
        overrides = {}

    pi = koji.PathInfo(topdir=KOJI_ROOT)

    for archive in archives:
        url = pi.mavenbuild(build_info) + '/' + pi.mavenfile(archive)
        # Use any overrides for this archive ID
        archive_overrides = overrides.get(archive['id'], {})
        status = archive_overrides.get('status', 200)
        body = archive_overrides.get('body', archive['filename'] + archive['group_id'])
        responses.add(responses.GET, url, body=body, status=status)


def mock_url_downloads(remote_files=None, overrides=None):
    if not remote_files:
        remote_files = DEFAULT_REMOTE_FILES
    if not overrides:
        overrides = {}

    for remote_file in remote_files:
        url = remote_file['url']
        # Use any overrides for this url
        remote_file_overrides = overrides.get(url, {})
        body = remote_file_overrides.get('body', url)
        status = remote_file_overrides.get('status', 200)
        responses.add(responses.GET, url, body=body, status=status)


def mock_pnc_downloads(contents=None, pnc_responses=None, overrides=None):
    if not contents:
        contents = DEFAULT_PNC_ARTIFACTS
    if not pnc_responses:
        pnc_responses = DEFAULT_PNC_RESPONSES
    if not overrides:
        overrides = {}

    builds = contents['builds']
    # Use any overrides for these builds
    pnc_artifacts_overrides = overrides.get('builds', {})
    for build in builds:
        for artifact in build['artifacts']:
            api_url = PNC_ROOT + '/artifacts/{}'.format(artifact['id'])
            body = pnc_artifacts_overrides.get('body', b'abc')
            status = pnc_artifacts_overrides.get('status', 200)
            responses.add(responses.GET, api_url, body=json.dumps(pnc_responses[artifact['id']]),
                          status=status)
            responses.add(responses.GET, pnc_responses[artifact['id']]['publicUrl'], body=body,
                          status=status)


@responses.activate  # noqa
def test_fetch_maven_artifacts(workflow, source_dir):
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(source_dir)
    mock_fetch_artifacts_by_url(source_dir)
    mock_fetch_artifacts_from_pnc(source_dir)
    mock_nvr_downloads()
    mock_url_downloads()
    mock_pnc_downloads()

    results = mock_env(workflow, source_dir).create_runner().run()

    plugin_result = results[FetchMavenArtifactsPlugin.key]

    assert len(plugin_result['download_queue']) == (len(DEFAULT_ARCHIVES) + len(
        DEFAULT_REMOTE_FILES) + len(DEFAULT_PNC_ARTIFACTS['builds']))
    assert len(plugin_result['pnc_artifact_ids']) == len(DEFAULT_PNC_ARTIFACTS['builds'])
    for download in plugin_result['download_queue']:
        assert source_dir.joinpath(
            FetchMavenArtifactsPlugin.DOWNLOAD_DIR, download.dest
        ).exists()


@pytest.mark.parametrize(('nvr_requests', 'expected'), (  # noqa
    ([], []),  # Empty file
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'filename': '*'}]
        }
    ], DEFAULT_ARCHIVES),
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'filename': '*javadoc*'}]
        }
    ], [ARCHIVE_JAXB_JAVADOC_SUN_JAR, ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR]),
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'filename': '*javadoc*', 'group_id': 'org.glassfish.jaxb'}]
        }
    ], [ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR]),
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'group_id': 'org.glassfish.jaxb'}]
        }
    ], [ARCHIVE_JAXB_GLASSFISH_JAR, ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR, ARCHIVE_JAXB_GLASSFISH_POM]),
))
@responses.activate
def test_fetch_maven_artifacts_nvr_filtering(workflow, source_dir, nvr_requests, expected):
    """Test filtering of archives in a Koji build."""
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(source_dir, contents=yaml.safe_dump(nvr_requests))
    mock_nvr_downloads(archives=expected)

    results = mock_env(workflow, source_dir).create_runner().run()

    plugin_result = results[FetchMavenArtifactsPlugin.key]

    assert len(plugin_result['download_queue']) == len(expected)
    for download in plugin_result['download_queue']:
        assert len(download.checksums.values()) == 1
    assert (set(list(download.checksums.values())[0] for download in plugin_result[
        'download_queue']) == set(expectation['checksum'] for expectation in expected))
    for download in plugin_result['download_queue']:
        assert source_dir.joinpath(
            FetchMavenArtifactsPlugin.DOWNLOAD_DIR, download.dest
        ).exists()


@pytest.mark.parametrize(('nvr_requests', 'error_msg'), (  # noqa
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'filename': '*gboss'}]
        }
    ], '*gboss'),
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'group_id': 'glassfish.org'}]
        }
    ], 'glassfish.org'),
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'filename': '*', 'group_id': 'glassfish.org'}]
        }
    ], 'glassfish.org'),
))
@responses.activate
def test_fetch_maven_artifacts_nvr_no_match(workflow, source_dir,
                                            nvr_requests, error_msg):
    """Err when a requested archive is not found in Koji build."""
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(source_dir, contents=yaml.safe_dump(nvr_requests))
    mock_nvr_downloads()

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow, source_dir).create_runner().run()

    assert 'failed to find archives' in str(e.value)
    assert error_msg in str(e.value)


@responses.activate  # noqa
def test_fetch_maven_artifacts_nvr_bad_checksum(workflow, source_dir):
    """Err when downloaded archive from Koji build has unexpected checksum."""
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(source_dir)
    mock_nvr_downloads(overrides={1269850: {'body': 'corrupted-file'}})

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow, source_dir).create_runner().run()

    assert 'does not match expected checksum' in str(e.value)


@responses.activate  # noqa
def test_fetch_maven_artifacts_nvr_bad_url(workflow, source_dir):
    """Err on download errors for artifact from Koji build."""
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(source_dir)
    mock_nvr_downloads(overrides={1269850: {'status': 404}})

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow, source_dir).create_runner().run()

    assert '404 Client Error' in str(e.value)


@responses.activate  # noqa
def test_fetch_maven_artifacts_nvr_bad_nvr(workflow, source_dir):
    """Err when given nvr is not a valid build in Koji."""
    mock_koji_session()
    contents = dedent("""\
        - nvr: where-is-this-build-3.0-2
        """)
    mock_fetch_artifacts_by_nvr(source_dir, contents=contents)
    mock_nvr_downloads()

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow, source_dir).create_runner().run()

    assert 'Build where-is-this-build-3.0-2 not found' in str(e.value)


@pytest.mark.parametrize('contents', (  # noqa
    dedent("""\
        - nvo: invalid attribute
        """),

    dedent("""\
        nvr: not a list
        """),

    dedent("""\
        - nvr: foo-bar-22-9
          archives: not a list
        """),

    dedent("""\
        - nvr: foo-bar-22-9
          archives:
            - filenamo: invalid attribute
        """),

))
@responses.activate
def test_fetch_maven_artifacts_nvr_schema_error(workflow, source_dir, contents):
    """Err on invalid format for fetch-artifacts-koji.yaml"""
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(source_dir, contents=contents)
    mock_nvr_downloads()

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow, source_dir).create_runner().run()

    assert 'OsbsValidationException' in str(e.value)


@pytest.mark.parametrize('contents', (  # noqa
    dedent("""\
        metadata:
            anything: information
        builds:
          - buid_id: invalid attribute
        """),

    dedent("""\
        metadata:
            anything: information
        builds:
          build_id: not a list
        """),

    dedent("""\
        metadata:
            anything: information
        builds:
          - build_id: 12345
            artifacts: not a list
        """),

    dedent("""\
        metadata:
            anything: information
        builds:
          - build_id: 12345
            artifacts:
              - ids: invalid attribute
        """),

    dedent("""\
        metadata:
          create_by: author
        builds:
          - build_id: 12345
            artifacts:
              - id: invalid value
        """),

))
@responses.activate
def test_fetch_maven_artifacts_pnc_schema_error(workflow, source_dir, contents):
    """Err on invalid format for fetch-artifacts-pnc.yaml"""
    mock_koji_session()
    mock_fetch_artifacts_from_pnc(source_dir, contents=contents)

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow, source_dir).create_runner().run()

    assert 'OsbsValidationException' in str(e.value)


@responses.activate
def test_fetch_maven_artifacts_no_pnc_config(workflow, source_dir):
    r_c_m = {
        'version': 1,
        'koji': {
            'hub_url': KOJI_HUB,
            'root_url': KOJI_ROOT,
            'auth': {}
        }
    }

    with pytest.raises(PluginFailedException) as exc:
        mock_koji_session()
        mock_fetch_artifacts_from_pnc(source_dir)
        mock_pnc_downloads()
        mock_env(workflow, source_dir, r_c_m=r_c_m).create_runner().run()

    msg = 'No PNC configuration found in reactor config map'

    assert msg in str(exc.value)


@pytest.mark.parametrize(('contents', 'expected'), (  # noqa
    ([], []),
    ([REMOTE_FILE_WITH_TARGET], [REMOTE_FILE_WITH_TARGET]),
))
@responses.activate
def test_fetch_maven_artifacts_url_with_target(workflow, source_dir,
                                               contents, expected):
    """Remote file is downloaded into specified filename."""
    mock_koji_session()
    remote_files = contents
    mock_fetch_artifacts_by_url(source_dir, contents=yaml.safe_dump(remote_files))
    mock_url_downloads(remote_files)

    results = mock_env(workflow, source_dir).create_runner().run()
    plugin_result = results[FetchMavenArtifactsPlugin.key]

    assert len(plugin_result['download_queue']) == len(expected)

    if not expected:
        return

    download = plugin_result['download_queue'][0]
    assert source_dir.joinpath(
        FetchMavenArtifactsPlugin.DOWNLOAD_DIR, download.dest
    ).exists()
    assert download.dest == REMOTE_FILE_WITH_TARGET['target']
    assert not REMOTE_FILE_WITH_TARGET['url'].endswith(download.dest)


@responses.activate  # noqa
def test_fetch_maven_artifacts_url_bad_checksum(workflow, source_dir):
    """Err when downloaded remote file has unexpected checksum."""
    mock_koji_session()
    mock_fetch_artifacts_by_url(source_dir)
    mock_url_downloads(overrides={REMOTE_FILE_SPAM['url']: {'body': 'corrupted-file'}})

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow, source_dir).create_runner().run()

    assert 'does not match expected checksum' in str(e.value)


@responses.activate  # noqa
def test_fetch_maven_artifacts_url_bad_url(workflow, source_dir):
    """Err on download errors for remote file."""
    mock_koji_session()
    mock_fetch_artifacts_by_url(source_dir)
    mock_url_downloads(overrides={REMOTE_FILE_SPAM['url']: {'status': 404}})

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow, source_dir).create_runner().run()

    assert '404 Client Error' in str(e.value)


@pytest.mark.parametrize('contents', (  # noqa
    dedent("""\
        - uru: invalid attribute
        """),

    dedent("""\
        url: not a list
        """),

    dedent("""\
        - url: missing hashing
        """),

    dedent("""\
        - url: missing source hashing
          source-url: source
          md5: cac3a36cfefd5baced859ac3cd9e2329
        """),

    dedent("""\
        - url: invalid md5 checksum size
          md5: a1234
        """),

    dedent("""\
        - url: invalid sha1 checksum size
          sha1: a1234
        """),

    dedent("""\
        - url: invalid sha256 checksum size
          sha256: a1234
        """),
))
@responses.activate
def test_fetch_maven_artifacts_url_schema_error(workflow, source_dir, contents):
    """Err on invalid format for fetch-artifacts-url.yaml"""
    mock_koji_session()
    mock_fetch_artifacts_by_url(source_dir, contents=contents)
    mock_url_downloads()

    with pytest.raises(PluginFailedException) as e:
        mock_env(workflow, source_dir).create_runner().run()

    assert 'OsbsValidationException' in str(e.value)


@pytest.mark.parametrize(('domains', 'raises'), (  # noqa
    ([], False),
    ([FILER_ROOT_DOMAIN], False),
    ([FILER_ROOT_DOMAIN.upper()], False),
    ([FILER_ROOT_DOMAIN, 'spam.com'], False),
    ([
        FILER_ROOT_DOMAIN + '/spam/',
        FILER_ROOT_DOMAIN + '/bacon/',
        FILER_ROOT_DOMAIN + '/eggs/',
        FILER_ROOT_DOMAIN + '/ham/',
        FILER_ROOT_DOMAIN + '/sausage/',
        FILER_ROOT_DOMAIN + '/biscuit/',
    ], False),
    ([FILER_ROOT_DOMAIN + '/spam/'], True),
    (['spam.com'], True),
    (['spam.com', 'bacon.bz'], True),
))
@responses.activate
def test_fetch_maven_artifacts_url_allowed_domains(workflow, source_dir, domains, raises):
    """Validate URL domain is allowed when fetching remote file."""
    mock_koji_session()
    mock_fetch_artifacts_by_url(source_dir)
    mock_url_downloads()

    runner = mock_env(workflow, source_dir, domains_override=domains).create_runner()

    if raises:
        with pytest.raises(PluginFailedException) as e:
            runner.run()
        assert 'is not in list of allowed domains' in str(e.value)

    else:
        results = runner.run()
        plugin_result = results[FetchMavenArtifactsPlugin.key]
        for download in plugin_result['download_queue']:
            assert source_dir.joinpath(
                FetchMavenArtifactsPlugin.DOWNLOAD_DIR, download.dest
            ).exists()


@responses.activate  # noqa
def test_fetch_maven_artifacts_commented_out_files(workflow, source_dir):
    mock_koji_session()
    contents = dedent("""\
        # This file

        # is completely
        # and absolutely
        # commented out!
        """)
    mock_fetch_artifacts_by_nvr(source_dir, contents=contents)
    mock_fetch_artifacts_by_url(source_dir, contents=contents)
    mock_nvr_downloads()
    mock_url_downloads()

    results = mock_env(workflow, source_dir).create_runner().run()
    plugin_result = results[FetchMavenArtifactsPlugin.key]

    assert len(plugin_result['download_queue']) == 0
    assert not source_dir.joinpath(FetchMavenArtifactsPlugin.DOWNLOAD_DIR).exists()
