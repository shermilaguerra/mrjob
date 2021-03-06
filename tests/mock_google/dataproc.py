# Copyright 2009-2016 Yelp and Contributors
# Copyright 2017 Yelp
# Copyright 2018 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# see case.py for definition of mock_clusters and mock_gcs_fs
from copy import deepcopy

from google.api_core.exceptions import AlreadyExists
from google.api_core.exceptions import InvalidArgument
from google.api_core.exceptions import NotFound
from google.cloud.dataproc_v1.types import Cluster
from google.cloud.dataproc_v1.types import ClusterStatus
from google.cloud.dataproc_v1.types import Job
from google.cloud.dataproc_v1.types import JobStatus

from mrjob.dataproc import _cluster_state_name
from mrjob.dataproc import _job_state_name


# convert strings (e.g. 'RUNNING') to enum values

def _cluster_state_value(state_name):
    return ClusterStatus.State.Value(state_name)


def _job_state_value(state_name):
    return JobStatus.State.Value(state_name)


class MockGoogleDataprocClient(object):
    """Common code for mock dataproc clients."""

    def __init__(
            self, mock_clusters, mock_jobs, mock_gcs_fs, mock_jobs_succeed,
            channel=None, credentials=None):
        self.channel = channel
        self.credentials = credentials

        # maps (project_id, region, cluster_name) to a
        # google.cloud.dataproc_v1.types.Cluster
        self.mock_clusters = mock_clusters

        # maps (project_id, region, cluster_name, job_name) to a
        # google.cloud.dataproc_v1.types.Job
        self.mock_jobs = mock_jobs

        # if False, mock jobs end in ERROR
        self.mock_jobs_succeed = mock_jobs_succeed

        # see case.py
        self.mock_gcs_fs = mock_gcs_fs

    def _expected_region(self):
        if self.channel:
            target = self.channel._channel.target()
            name = target.split('.')[0]
            if '-' in name:
                return '-'.join(name.split('-')[:-1])

        return 'global'

    def _check_region_matches_endpoint(self, region):
        expected = self._expected_region()

        if region != expected:
            raise InvalidArgument(
                "Region '%s' invalid or not supported by this endpoint;"
                " permitted regions: '[%s]'" % (region, expected))


class MockGoogleDataprocClusterClient(MockGoogleDataprocClient):

    """Mock out google.cloud.dataproc_v1.ClusterControllerClient"""

    def create_cluster(self, project_id, region, cluster):
        self._check_region_matches_endpoint(region)

        # convert dict to object
        if not isinstance(cluster, Cluster):
            cluster = Cluster(**cluster)

        if cluster.project_id:
            if cluster.project_id != project_id:
                raise InvalidArgument(
                    'If provided, CreateClusterRequest.cluster.project_id must'
                    ' match CreateClusterRequest.project_id')
        else:
            cluster.project_id = project_id

        if not cluster.cluster_name:
            raise InvalidArgument('Cluster name is required')

        # initialize cluster status
        cluster.status.state = _cluster_state_value('CREATING')

        cluster_key = (project_id, region, cluster.cluster_name)

        if cluster_key in self.mock_clusters:
            raise AlreadyExists('Already exists: Cluster ' +
                                _cluster_path(*cluster_key))

        self.mock_clusters[cluster_key] = cluster

    def delete_cluster(self, project_id, region, cluster_name):
        self._check_region_matches_endpoint(region)

        cluster_key = (project_id, region, cluster_name)

        cluster = self.mock_clusters.get(cluster_key)

        if not cluster:
            raise NotFound('Not found: Cluster ' + _cluster_path(*cluster_key))

        cluster.status.state = _cluster_state_value('DELETING')

    def get_cluster(self, project_id, region, cluster_name):
        self._check_region_matches_endpoint(region)

        cluster_key = (project_id, region, cluster_name)
        if cluster_key not in self.mock_clusters:
            raise NotFound(
                'Not Found: Cluster ' + _cluster_path(*cluster_key))

        cluster = self.mock_clusters[cluster_key]

        result = deepcopy(cluster)
        self._simulate_progress(project_id, region, cluster_name)
        return result

    def _simulate_progress(self, project_id, region, cluster_name):
        cluster_key = (project_id, region, cluster_name)
        cluster = self.mock_clusters[cluster_key]

        state_name = _cluster_state_name(cluster.status.state)

        if state_name == 'DELETING':
            del self.mock_clusters[cluster_key]
        else:
            # just move from STARTING to RUNNING
            cluster.status.state = _cluster_state_value('RUNNING')


class MockGoogleDataprocJobClient(MockGoogleDataprocClient):

    """Mock out google.cloud.dataproc_v1.JobControllerClient"""

    def submit_job(self, project_id, region, job):
        self._check_region_matches_endpoint(region)

        # convert dict to object
        if not isinstance(job, Job):
            job = Job(**job)

        if not (project_id and job.reference.job_id):
            raise NotImplementedError('generation of job IDs not implemented')
        job_id = job.reference.job_id

        if not job.placement.cluster_name:
            raise InvalidArgument('Cluster name is required')

        # cluster must exist
        cluster_key = (project_id, region, job.placement.cluster_name)
        if cluster_key not in self.mock_clusters:
            raise NotFound('Not Found: Cluster ' + _cluster_path(*cluster_key))

        if not job.hadoop_job:
            raise NotImplementedError('only hadoop jobs are supported')

        if job.reference.project_id:
            if job.reference.project_id != project_id:
                raise InvalidArgument(
                    'If provided, SubmitJobRequest.job.job_reference'
                    '.project_id must match SubmitJobRequest.project_id')
        else:
            job.reference.project_id = project_id

        job.status.state = _job_state_value('SETUP_DONE')

        job_key = (project_id, region, job_id)

        if job_key in self.mock_jobs:
            raise AlreadyExists(
                'Already exists: Job ' + _job_path(*job_key))

        self.mock_jobs[job_key] = job

        return deepcopy(job)

    def get_job(self, project_id, region, job_id):
        self._check_region_matches_endpoint(region)

        job_key = (project_id, region, job_id)

        job = self.mock_jobs.get(job_key)

        if not job:
            raise NotFound('Not found: Job ' + _job_path(*job_key))

        result = deepcopy(job)
        self._simulate_progress(job)
        return result

    def list_jobs(self, project_id, region, page_size=None,
                  cluster_name=None, job_state_matcher=None):
        self._check_region_matches_endpoint(region)

        if page_size:
            raise NotImplementedError('page_size is not mocked')

        for job_key, job in self.mock_jobs.items():
            job_project_id, job_region, job_id = job_key

            if job_project_id != project_id:
                continue

            if job_region != region:
                continue

            if cluster_name and job.placement.cluster_name != cluster_name:
                continue

            if job_state_matcher:
                if job_state_matcher != _STATE_MATCHER_ACTIVE:
                    raise NotImplementedError(
                        'only ACTIVE job state matcher is mocked')

                if (_job_state_name(job.status.state) not in
                        ('PENDING', 'RUNNING', 'CANCEL_PENDING')):
                    continue

            yield deepcopy(job)

    def _simulate_progress(self, mock_job):
        state = _job_state_name(mock_job.status.state)

        if state == 'SETUP_DONE':
            mock_job.status.state = _job_state_value('PENDING')
        elif state == 'PENDING':
            mock_job.status.state = _job_state_value('RUNNING')
        elif state == 'RUNNING':
            if self.mock_jobs_succeed:
                mock_job.status.state = _job_state_value('DONE')
            else:
                mock_job.status.state = _job_state_value('ERROR')


def _cluster_path(project_id, region, cluster_name):
    return 'projects/%s/regions/%s/clusters/%s' % (
        project_id, region, cluster_name)


def _job_path(project_id, region, job_id):
    return 'projects/%s/regions/%s/jobs/%s'
